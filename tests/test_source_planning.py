from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bookflow.common import read_chapters, read_paragraphs, write_yaml
from bookflow.planning import _term_candidates, check_plan, coverage
from bookflow.source import ingest


class SourcePlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        write_yaml(self.project / "project.yaml", {"depth": {"required_layers": [2], "series_layers": [3]}})
        self.original = self.root / "book.txt"
        self.original.write_text("第一章 来客\n\n旧怀表躺在桌上。\n\n王福拿起旧怀表。\n\n第二章 清晨\n\n旧怀表停了。\n\n旧怀表留下证据。", encoding="utf-8")

    def snapshot_source(self):
        return {str(path.relative_to(self.project)): path.read_bytes()
                for path in (self.project / "source").rglob("*") if path.is_file()}

    def test_german_recurring_clue_is_visible_across_chapters(self):
        paragraphs = {
            "p00001": {"chapter": "ch01", "text": "PROMETHEUS und Prometheus waren hier."},
            "p00002": {"chapter": "ch02", "text": "prometheus und PROMETHEUS waren dort."},
        }
        result = _term_candidates(paragraphs, "尚未整理该系统。", 3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["term"].casefold(), "prometheus")
        self.assertEqual(result[0]["count"], 4)
        self.assertEqual(result[0]["chapters"], ["ch01", "ch02"])

    def test_documented_german_term_and_single_chapter_repetition_are_excluded(self):
        paragraphs = {
            "p00001": {"chapter": "ch01", "text": "Schlüssel " * 4 + "PROMETHEUS " * 2},
            "p00002": {"chapter": "ch02", "text": "prometheus " * 2},
        }
        self.assertEqual(_term_candidates(paragraphs, "已分析 Prometheus 的用途。", 3), [])

    def test_german_function_words_do_not_become_clues(self):
        paragraphs = {f"p0000{i}": {"chapter": f"ch0{i}", "text": "Aber hatte waren nicht wieder weil denn immer seinem " * 4} for i in (1, 2)}
        self.assertEqual(_term_candidates(paragraphs, "", 3), [])

    def valid_plan(self):
        return {"episodes": [{"ep": 1, "title_working": "旧怀表的秘密", "covers": ["ch01", "ch02"],
                              "genre_mode": "suspense", "target_chars": 3000, "core_question": "怀表为什么停了",
                              "takeaways": [{"id": "K01", "layer": 2, "text": "停表暴露了时间", "evidence": ["p00003"]},
                                            {"id": "K02", "layer": 3, "text": "作者用怀表串联线索", "evidence": ["p00001-p00004"]}],
                              "reveal": [], "withhold": []}], "skipped": []}

    def test_failed_or_different_import_preserves_existing_generation(self):
        first = ingest(self.project, [self.original])
        before = self.snapshot_source()
        with self.assertRaises(FileNotFoundError):
            ingest(self.project, [self.original, self.root / "missing.txt"])
        self.assertEqual(before, self.snapshot_source())
        different = self.root / "different.txt"
        different.write_text("这是一份不同的原文。", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "新建项目"):
            ingest(self.project, [different])
        self.assertEqual(before, self.snapshot_source())
        repeated = ingest(self.project, [self.original])
        self.assertEqual("unchanged", repeated["status"])
        self.assertEqual(first["generation"], repeated["generation"])
        self.assertEqual(before, self.snapshot_source())
        self.assertTrue(self.original.exists())

    def test_failed_pointer_write_does_not_select_partial_import(self):
        from bookflow import source
        real_write = source.write_json

        def fail_pointer(path, data):
            if Path(path).name == "current.json":
                raise OSError("simulated disk error")
            return real_write(path, data)

        with patch.object(source, "write_json", side_effect=fail_pointer):
            with self.assertRaises(OSError):
                ingest(self.project, [self.original])
        self.assertFalse((self.project / "source" / "current.json").exists())
        result = ingest(self.project, [self.original])
        self.assertEqual("imported", result["status"])
        self.assertEqual(4, len(read_paragraphs(self.project)))

    def test_concurrent_commit_is_rejected_before_selecting_a_generation(self):
        from bookflow.source import _import_lock

        with _import_lock(self.project / "source"):
            with self.assertRaisesRegex(ValueError, "正在提交"):
                ingest(self.project, [self.original])
        self.assertFalse((self.project / "source" / "current.json").exists())
        self.assertEqual("imported", ingest(self.project, [self.original])["status"])

    def test_heading_next_to_body_is_not_merged_into_paragraph(self):
        self.original.write_text("# 前言\n这是序言正文。\n\n# 第一章\n这是第一章正文。\n\n这是下一段。", encoding="utf-8")
        ingest(self.project, [self.original])
        chapters = read_chapters(self.project)
        self.assertEqual(["前言", "第一章"], [chapter["title"] for chapter in chapters])
        self.assertEqual(3, len(read_paragraphs(self.project)))

    def test_epub_uses_reading_order_instead_of_zip_member_order(self):
        epub = self.root / "book.epub"
        with zipfile.ZipFile(epub, "w") as archive:
            archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
            archive.writestr("OPS/book.opf", '<package><manifest><item id="a" href="one.xhtml"/><item id="b" href="two.xhtml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            archive.writestr("OPS/two.xhtml", '<html><body><h1>第二章</h1><p>后来的事。</p></body></html>')
            archive.writestr("OPS/one.xhtml", '<html><head><title>忽略元数据</title></head><body><h1>第一章</h1><p>先发生的事。</p></body></html>')
        ingest(self.project, [epub])
        paragraphs = list(read_paragraphs(self.project).values())
        self.assertEqual(["先发生的事。", "后来的事。"], [p["text"] for p in paragraphs])
        self.assertEqual(["第一章", "第二章"], [c["title"] for c in read_chapters(self.project)])

    def test_docx_preserves_styled_heading_and_body(self):
        docx = self.root / "book.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>一个章节</w:t></w:r></w:p><w:p><w:r><w:t>这是原文。</w:t></w:r></w:p></w:body></w:document>')
        ingest(self.project, [docx])
        self.assertEqual("一个章节", read_chapters(self.project)[0]["title"])
        self.assertEqual("这是原文。", read_paragraphs(self.project)["p00001"]["text"])

    def test_rescan_preserves_human_review_and_keeps_candidate_visible(self):
        ingest(self.project, [self.original])
        analysis = self.project / "analysis"
        (analysis / "chapter_notes").mkdir(parents=True)
        (analysis / "chapter_notes" / "ch01.md").write_text("来客出现 [p00001]", encoding="utf-8")
        (analysis / "chapter_notes" / "ch02.md").write_text("清晨发生变化 [p00003]", encoding="utf-8")
        (analysis / "book_brief.md").write_text("一次调查 [p00001]", encoding="utf-8")
        write_yaml(analysis / "threads.yaml", [{"id": "T01", "setup": ["p00001"], "payoff": "none",
                                                "importance": "high", "note": "作者有意保持开放结局"}])
        review = analysis / "coverage_review.yaml"
        review.write_text("status: needs_work\nissues:\n  - id: C01\n    severity: P0\n    text: 旧怀表遗漏\n", encoding="utf-8")
        before = review.read_bytes()
        first, second = coverage(self.project), coverage(self.project)
        self.assertEqual(before, review.read_bytes())
        self.assertEqual([], first["errors"])
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertTrue(any(candidate["term"] == "旧怀表" for candidate in second["candidates"]))
        self.assertTrue((analysis / "coverage_machine.json").exists())

    def test_valid_plan_passes_but_fabricated_range_endpoint_is_rejected(self):
        ingest(self.project, [self.original])
        plan_path = self.project / "plan" / "episodes.yaml"
        plan = self.valid_plan()
        write_yaml(plan_path, plan)
        self.assertEqual([], check_plan(self.project)["errors"])
        plan["episodes"][0]["takeaways"][0]["evidence"] = ["p00001-p99999"]
        write_yaml(plan_path, plan)
        errors = check_plan(self.project)["errors"]
        self.assertTrue(any("p99999" in error for error in errors), errors)

    def test_verified_external_evidence_can_support_a_takeaway(self):
        ingest(self.project, [self.original])
        plan = self.valid_plan()
        plan["episodes"][0]["takeaways"].append(
            {"id": "K03", "layer": 4, "text": "一个需要书外材料支撑的观点", "evidence": ["ext:E01"]})
        write_yaml(self.project / "plan" / "episodes.yaml", plan)
        source_path = self.project / "analysis" / "external_sources.yaml"
        entry = {"id": "E01", "title": "研究原文", "url": "https://example.org/research",
                 "accessed_at": "2026-09-15", "status": "verified"}
        write_yaml(source_path, {"sources": [entry]})
        self.assertEqual([], check_plan(self.project)["errors"])
        entry["status"] = "pending"
        write_yaml(source_path, {"sources": [entry]})
        self.assertTrue(any("E01" in error for error in check_plan(self.project)["errors"]))


if __name__ == "__main__":
    unittest.main()
