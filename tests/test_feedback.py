import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from bookflow import feedback
from bookflow.common import atomic_write, write_yaml


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "projects/book"
        write_yaml(self.project / "project.yaml", {"book": {"title": "试稿"}})
        self.draft = self.project / "episodes/ep01/draft_v1.md"
        atomic_write(self.draft, "---\nepisode: 1\n---\n## 01 门\n他拿起钥匙。\n门却已经开了。\n〔据 p00001〕\n## 02 人\n屋里没有人。")
        self.output = self.draft.parent / "human_edit/v1"
        result = feedback.create_copy(self.draft, self.output)
        self.docx = Path(result["docx"])
        self.markdown = Path(result["markdown"])
        self.baseline = Path(result["baseline"])

    def test_markdown_edit_is_imported(self):
        edited = self.root / "edited.md"
        text = self.markdown.read_text(encoding="utf-8").replace("他拿起钥匙。", "他攥紧钥匙。")
        edited.write_text(text, encoding="utf-8")
        result = feedback.import_edits(edited, self.baseline)
        self.assertEqual(result["changes"], 1)
        report = json.loads((Path(result["round"]) / "diff.json").read_text())
        self.assertEqual(report["edited_file_sha256"], feedback.sha256_file(edited))
        self.assertTrue((Path(result["round"]) / "edited.md").is_file())

    def edit(self, transform, comments=None):
        path = self.root / "edited.docx"
        with zipfile.ZipFile(self.docx) as source, zipfile.ZipFile(path, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "word/document.xml":
                    root = ET.fromstring(data)
                    transform(root)
                    data = feedback.xml(root)
                target.writestr(item.filename, data)
            if comments:
                target.writestr("word/comments.xml", comments)
        return path

    def body_paragraphs(self, root):
        return [p for p in root.findall("w:body/w:p", feedback.NS)
                if p.find("w:pPr/w:pStyle", feedback.NS).get(feedback.tag("val")) == "Normal"]

    def test_round_trip_is_unchanged_and_reimport_is_idempotent(self):
        result = feedback.import_edits(self.docx, self.baseline)
        self.assertEqual(result["changes"], 0)
        report = json.loads((Path(result["round"]) / "diff.json").read_text())
        self.assertFalse(report["paragraph_layout_changed"])
        self.assertFalse(report["headings_changed"])
        self.assertEqual(feedback.import_edits(self.docx, self.baseline)["status"], "already_imported")
        self.assertFalse((self.draft.parent / "final.md").exists())

    def test_insert_replace_delete_are_located(self):
        def transform(root):
            texts = self.body_paragraphs(root)
            texts[0].find("w:r/w:t", feedback.NS).text = "他攥着钥匙。奇怪，门怎么开了？"
            texts[1].find("w:r/w:t", feedback.NS).text = "屋里没人。"
        result = feedback.import_edits(self.edit(transform), self.baseline)
        report = json.loads((Path(result["round"]) / "diff.json").read_text())
        self.assertTrue(report["text_changed"])
        self.assertTrue(all(c["blocks"] for c in report["changes"]))
        revised = (Path(result["round"]) / "revised.txt").read_text()
        self.assertIn("他攥着钥匙", revised)
        self.assertNotIn("直接修改", revised)

    def test_paragraph_split_is_layout_only(self):
        def transform(root):
            body = root.find("w:body", feedback.NS)
            p = self.body_paragraphs(root)[0]
            p.find("w:r/w:t", feedback.NS).text = "他拿起钥匙。"
            new = ET.Element(feedback.tag("p"))
            feedback.el(feedback.el(new, "r"), "t", "门却已经开了。")
            body.insert(list(body).index(p) + 1, new)
        result = feedback.import_edits(self.edit(transform), self.baseline)
        data = json.loads((Path(result["round"]) / "diff.json").read_text())
        self.assertFalse(data["text_changed"])
        self.assertTrue(data["paragraph_layout_changed"])

    def test_reorder_is_detected(self):
        def transform(root):
            body = root.find("w:body", feedback.NS)
            p = self.body_paragraphs(root)[0]
            body.remove(p)
            body.insert(len(body) - 1, p)
        self.assertGreater(feedback.import_edits(self.edit(transform), self.baseline)["changes"], 0)

    def test_tracked_changes_and_comment_anchor(self):
        def transform(root):
            p = self.body_paragraphs(root)[0]
            for child in list(p)[1:]:
                p.remove(child)
            feedback.el(feedback.el(feedback.el(p, "del"), "r"), "delText", "旧句。")
            feedback.el(p, "commentRangeStart", id="7")
            feedback.el(feedback.el(feedback.el(p, "ins"), "r"), "t", "新句。")
            feedback.el(p, "commentRangeEnd", id="7")
        comments = f'<w:comments xmlns:w="{feedback.W}"><w:comment w:id="7" w:author="用户"><w:p><w:r><w:t>这里要更口语。</w:t></w:r></w:p></w:comment></w:comments>'
        parsed = feedback.read_docx(self.edit(transform, comments))
        self.assertEqual(parsed["paragraphs"][0], "新句。")
        self.assertTrue(parsed["tracked_changes"])
        self.assertEqual(parsed["comments"]["7"]["anchor"], "新句。")
        self.assertEqual(parsed["comments"]["7"]["text"], "这里要更口语。")

    def test_wrong_document_and_mutated_snapshot_are_rejected(self):
        result = feedback.create_copy(self.draft, self.draft.parent / "human_edit/other")
        with self.assertRaisesRegex(ValueError, "另一轮"):
            feedback.import_edits(Path(result["docx"]), self.baseline)
        atomic_write(self.output / "original.md", "被改动")
        with self.assertRaisesRegex(ValueError, "快照"):
            feedback.import_edits(self.docx, self.baseline)

    def test_baseline_text_tampering_is_rejected(self):
        data = json.loads(self.baseline.read_text())
        data["blocks"][0]["text"] = "虚构原文"
        self.baseline.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "基线段落"):
            feedback.import_edits(self.docx, self.baseline)

    def test_edit_copy_never_overwrites(self):
        with self.assertRaisesRegex(ValueError, "已存在"):
            feedback.create_copy(self.draft, self.output)

    def test_office_renumbered_styles_do_not_become_narration(self):
        path = self.root / "renumbered.docx"
        mapping = {"Title": "a3", "Subtitle": "a4", "Heading1": "1", "Normal": "a"}
        with zipfile.ZipFile(self.docx) as source, zipfile.ZipFile(path, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename in ("word/document.xml", "word/styles.xml"):
                    root = ET.fromstring(data)
                    for node in root.iter():
                        attr = feedback.tag("styleId") if node.tag == feedback.tag("style") else feedback.tag("val")
                        if node.tag in (feedback.tag("style"), feedback.tag("pStyle")) and node.get(attr) in mapping:
                            node.set(attr, mapping[node.get(attr)])
                    data = feedback.xml(root)
                target.writestr(item.filename, data)
        parsed = feedback.read_docx(path)
        self.assertEqual(parsed["headings"], ["01 门", "02 人"])
        self.assertEqual(parsed["paragraphs"], ["他拿起钥匙。门却已经开了。", "屋里没有人。"])

    def test_context_requires_evidence_and_separates_candidates(self):
        def transform(root):
            self.body_paragraphs(root)[0].find("w:r/w:t", feedback.NS).text = "门怎么开了？"
        result = feedback.import_edits(self.edit(transform), self.baseline)
        report_path = Path(result["round"]) / "diff.json"
        rule = {"id": "voice-001", "kind": "expression", "scope": "workspace", "status": "candidate",
                "instruction": "用具体疑问句", "rationale": "单次真实改动候选",
                "evidence": [{"report": str(report_path.relative_to(self.root)), "change": "c001"}]}
        write_yaml(self.root / "style/personal.yaml", {"rules": [rule]})
        with patch.object(feedback, "ROOT", self.root):
            context = feedback.feedback_context(self.project)
            self.assertEqual(len(context["candidate_rules"]), 1)
            self.assertEqual(context["active_rules"], [])
            self.assertEqual(context["errors"], [])
            rule["status"] = "active"
            write_yaml(self.root / "style/personal.yaml", {"rules": [rule]})
            self.assertEqual(len(feedback.feedback_context(self.project)["active_rules"]), 1)
            rule["kind"] = "fact"
            write_yaml(self.root / "style/personal.yaml", {"rules": [rule]})
            self.assertTrue(feedback.feedback_context(self.project)["errors"])
            rule.update(kind="expression", evidence=[])
            write_yaml(self.root / "style/personal.yaml", {"rules": [rule]})
            self.assertTrue(feedback.feedback_context(self.project)["errors"])

    def instruction_evidence(self, scope="workspace", report=None, report_path=None):
        report_path = report_path or self.project / "feedback/rounds/direct/user_instruction.json"
        report = report if report is not None else {
            "source": "user_message", "recorded_at": "2026-09-20",
            "instructions": [{"id": "U01", "text": "按小说节奏展开，重点关注人物的矛盾与无奈。"}],
        }
        atomic_write(report_path, json.dumps(report, ensure_ascii=False))
        rule = {"id": "narrative-001", "kind": "expression", "scope": scope, "status": "active",
                "instruction": "按人物处境展开叙事", "rationale": "用户明确指定剧情类作品的方向",
                "evidence": [{"type": "user_instruction", "report": str(report_path.relative_to(self.root)),
                              "instruction": "U01", "sha256": feedback.sha256_file(report_path)}]}
        rule_path = self.root / "style/personal.yaml" if scope == "workspace" else self.project / "feedback/rules.yaml"
        write_yaml(rule_path, {"rules": [rule]})
        return report_path, rule_path, rule

    def test_context_accepts_direct_instruction_without_an_edited_draft(self):
        with patch.object(feedback, "ROOT", self.root):
            for scope in ["workspace", "book"]:
                with self.subTest(scope=scope):
                    report_path, rule_path, rule = self.instruction_evidence(scope=scope)
                    context = feedback.feedback_context(self.project)
                    self.assertEqual(context["active_rules"], [rule])
                    self.assertEqual(context["errors"], [])
                    self.assertFalse((report_path.parent / "edited.md").exists())
                    self.assertFalse((self.project / "approvals").exists())
                    rule_path.unlink()

    def test_context_rejects_changed_direct_instruction(self):
        report_path, _, _ = self.instruction_evidence()
        report = json.loads(report_path.read_text())
        report["instructions"][0]["text"] = "被改动的指令"
        atomic_write(report_path, json.dumps(report, ensure_ascii=False))
        with patch.object(feedback, "ROOT", self.root):
            context = feedback.feedback_context(self.project)
        self.assertEqual(context["active_rules"], [])
        self.assertTrue(any("用户指令" in error for error in context["errors"]))

    def test_context_rejects_invalid_direct_instruction_records(self):
        cases = [
            {"source": "assistant", "instructions": [{"id": "U01", "text": "用户风格"}]},
            {"source": "user_message", "instructions": [{"id": "U02", "text": "用户风格"}]},
            {"source": "user_message", "instructions": [{"id": "U01", "text": " \n "}]},
            {"source": "user_message", "instructions": [{"id": "U01", "text": None}]},
            {"source": "user_message", "instructions": [{"id": "U01", "text": "原话"}] * 2},
            {"source": "user_message", "instructions": "无效结构"},
            [],
        ]
        with patch.object(feedback, "ROOT", self.root):
            for report in cases:
                with self.subTest(report=report):
                    self.instruction_evidence(report=report)
                    context = feedback.feedback_context(self.project)
                    self.assertEqual(context["active_rules"], [])
                    self.assertTrue(context["errors"])

    def test_context_rejects_direct_instruction_outside_allowed_scope(self):
        cases = [
            ("workspace", self.root / "elsewhere/user_instruction.json", False),
            ("book", self.root / "projects/other/feedback/rounds/direct/user_instruction.json", False),
            ("book", self.project / "analysis/user_instruction.json", False),
            ("workspace", self.project / "feedback/rounds/direct/user_instruction.json", True),
        ]
        with patch.object(feedback, "ROOT", self.root):
            for scope, report_path, absolute in cases:
                with self.subTest(scope=scope, path=report_path, absolute=absolute):
                    _, rule_path, rule = self.instruction_evidence(scope=scope, report_path=report_path)
                    if absolute:
                        rule["evidence"][0]["report"] = str(report_path)
                        write_yaml(rule_path, {"rules": [rule]})
                    context = feedback.feedback_context(self.project)
                    self.assertEqual(context["active_rules"], [])
                    self.assertTrue(context["errors"])
                    rule_path.unlink()

    def test_direct_instruction_cannot_make_a_workspace_fact_rule(self):
        _, rule_path, rule = self.instruction_evidence()
        rule["kind"] = "fact"
        write_yaml(rule_path, {"rules": [rule]})
        with patch.object(feedback, "ROOT", self.root):
            context = feedback.feedback_context(self.project)
        self.assertEqual(context["active_rules"], [])
        self.assertTrue(any("事实规则只属于本书" in error for error in context["errors"]))


if __name__ == "__main__":
    unittest.main()
