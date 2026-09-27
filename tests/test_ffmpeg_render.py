"""Local render adapter tests use generated stills and silence, never paid services."""
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from unittest.mock import patch

from bookflow.adapters import ffmpeg_render
from bookflow.adapters.ffmpeg_render import inputs, render
from bookflow.common import load_yaml, sha256_file, write_json, write_yaml
from bookflow.media_fixture import _png
from bookflow.produce import check, run as produce_run
from bookflow.selftest import run as selftest_run


def _title_png(path: Path) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    rows = []
    for y in range(180):
        pixels = b"".join((b"\xff\xff\xff\xdd" if 50 <= x < 270 and 30 <= y < 100
                           else b"\0\0\0\0") for x in range(320))
        rows.append(b"\0" + pixels)
    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", 320, 180, 8, 6, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
    path.write_bytes(image)


def _solid_png(path: Path, color: bytes) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    row = b"\0" + color * 640
    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", 640, 360, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(row * 360)) + chunk(b"IEND", b"")
    path.write_bytes(image)


def _red_mean(video: Path, second: float) -> float:
    result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-ss", str(second),
                             "-i", str(video), "-frames:v", "1", "-pix_fmt", "rgb24",
                             "-f", "rawvideo", "pipe:1"], capture_output=True, check=True)
    pixels = result.stdout
    return sum(pixels[0::3]) / (len(pixels) / 3)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本机 FFmpeg")
class FFmpegRenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "projects/book"
        self.epdir = self.project / "episodes/ep01"
        self.production = self.epdir / "production"
        self.production.mkdir(parents=True)
        write_yaml(self.project / "project.yaml", {
            "book": {"title": "测试书"},
            "render": {"fps": 12, "title_card": {"required": False}},
            "visual_pacing": {"output": {"width": 320, "height": 180}}})

    def prepare(self, *, shots=1):
        seconds = 8 * shots
        with wave.open(str(self.production / "final_mix.wav"), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(b"\0" * (seconds * 48000 * 2))
        (self.production / "subtitles.srt").write_text(
            "1\n00:00:00,500 --> 00:00:02,500\n字幕测试。\n", encoding="utf-8")
        rows = []
        for index in range(shots):
            name = f"still-{index + 1}.png"
            if index == 1:
                _solid_png(self.production / name, b"\xee\x20\x20")
            else:
                _png(self.production / name)
            rows.append({"start": index * 8, "end": (index + 1) * 8, "image": name})
        write_yaml(self.production / "storyboard.yaml", {"shots": rows})

    def test_one_and_two_shot_render_have_audio_video_and_review_frame(self):
        for count in (1, 2):
            with self.subTest(shots=count):
                self.prepare(shots=count)
                result = render(self.epdir, replace_owned=count == 2)
                self.assertAlmostEqual(result["probe"]["duration_sec"], count * 8, delta=0.35)
                self.assertEqual(result["probe"]["subtitle_frame_review"], "awaiting_human")
                self.assertTrue(result["report_frame"].is_file())
                self.assertTrue(result["output"].is_file())
                if count == 2:
                    early = _red_mean(result["output"], 4)
                    middle = _red_mean(result["output"], 8)
                    late = _red_mean(result["output"], 12)
                    self.assertLess(early + 30, middle)
                    self.assertLess(middle + 30, late)

    def test_gap_and_unowned_output_fail_before_replacement(self):
        self.prepare()
        board = self.production / "storyboard.yaml"
        data = load_yaml(board)
        data["shots"][0]["start"] = 1
        write_yaml(board, data)
        with self.assertRaisesRegex(ValueError, "空档"):
            inputs(self.epdir)
        self.assertFalse((self.production / "final.mp4").exists())
        data["shots"][0]["start"] = 0
        write_yaml(board, data)
        target = self.production / "final.mp4"
        target.write_bytes(b"manual video")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            render(self.epdir)
        self.assertEqual(target.read_bytes(), b"manual video")

    def test_input_change_during_render_does_not_commit_video(self):
        self.prepare()
        picture = self.production / "still-1.png"
        original = ffmpeg_render._run

        def change_after_encode(command, *, cwd, timeout):
            result = original(command, cwd=cwd, timeout=timeout)
            if "-filter_complex" in command:
                picture.write_bytes(picture.read_bytes() + b"changed")
            return result

        with patch.object(ffmpeg_render, "_run", side_effect=change_after_encode):
            with self.assertRaisesRegex(ValueError, "输入文件发生变化"):
                render(self.epdir)
        self.assertFalse((self.production / "final.mp4").exists())

    def test_output_created_during_render_is_not_overwritten(self):
        self.prepare()
        target = self.production / "final.mp4"
        original = ffmpeg_render._run

        def create_after_encode(command, *, cwd, timeout):
            result = original(command, cwd=cwd, timeout=timeout)
            if "-filter_complex" in command:
                target.write_bytes(b"manual video")
            return result

        with patch.object(ffmpeg_render, "_run", side_effect=create_after_encode):
            with self.assertRaisesRegex(ValueError, "成片目标发生变化"):
                render(self.epdir)
        self.assertEqual(target.read_bytes(), b"manual video")

    def test_title_card_is_overlaid_on_original_shot_without_extending_duration(self):
        self.prepare()
        plain = render(self.epdir)
        plain_frame = sha256_file(plain["report_frame"])
        config_path = self.project / "project.yaml"
        config = load_yaml(config_path)
        config["render"]["title_card"]["required"] = True
        write_yaml(config_path, config)
        with self.assertRaisesRegex(ValueError, "缺少标题卡基线"):
            inputs(self.epdir)
        _png(self.production / "title.png")
        write_yaml(self.production / "title_card.yaml", {
            "mode": "overlay_on_original_plot_frame", "image": "title.png",
            "start_sec": 1, "end_sec": 3})
        with self.assertRaisesRegex(ValueError, "没有透明通道"):
            inputs(self.epdir)
        _title_png(self.production / "title.png")
        titled = render(self.epdir, replace_owned=True)
        self.assertNotEqual(sha256_file(titled["report_frame"]), plain_frame)
        self.assertAlmostEqual(titled["probe"]["duration_sec"], 8, delta=0.35)

    def test_produce_from_render_requires_real_upstream_and_preserves_charges(self):
        fixture_root = self.root / "fixture"
        report = selftest_run(fixture_root)
        self.assertTrue(report["passed"], report)
        project = fixture_root / "projects/selftest-fixture"
        epdir = project / "episodes/ep01"
        config_path = project / "project.yaml"
        config = load_yaml(config_path)
        config["render"] = {"fps": 12, "title_card": {"required": False}}
        config.setdefault("visual_pacing", {})["output"] = {"width": 320, "height": 180}
        write_yaml(config_path, config)
        manifest_path = epdir / "production/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("test_fixture_only")
        manifest["mode"] = "real"
        manifest["charges"] = [{"id": "old-voice", "stage": "voice", "cost_cny": 0}]
        final = epdir / "final.md"
        production = epdir / "production"
        paths = {"cues": (final, epdir / "final.sentences.json"),
                 "voice": (final, project / "production/voice_cast.yaml"),
                 "sfx": (production / "sound_cues.yaml",),
                 "mix": (production / "voice.wav", production / "sfx.wav"),
                 "subs": (production / "final_mix.wav", production / "timing_actual.json"),
                 "storyboard": (production / "timing_actual.json",),
                 "images": (production / "storyboard.yaml",)}
        for stage, source in paths.items():
            record = manifest["stages"][stage]
            record["inputs"] = [{"path": str(path.relative_to(project)), "sha256": sha256_file(path)}
                                for path in source]
            record["charge_ids"] = []
        write_json(manifest_path, manifest)
        before = check(project, 1, until="images")
        self.assertTrue(all(row["ready"] for row in before["stages"]), before)
        result = produce_run(project, 1, from_stage="render")
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["executed"], ["render"])
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(after["charges"], manifest["charges"])
        self.assertTrue(check(project, 1, until="render")["stages"][-1]["ready"])
        repeated = produce_run(project, 1, from_stage="render")
        self.assertTrue(repeated["passed"], repeated)
        video = epdir / "production/final.mp4"
        video.write_bytes(b"manual edit")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            produce_run(project, 1, from_stage="render")
        self.assertEqual(video.read_bytes(), b"manual edit")


if __name__ == "__main__":
    unittest.main()
