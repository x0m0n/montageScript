#!/usr/bin/env python3
"""
Unit tests for archive_montage.py.

Run from the same directory as archive_montage.py:

    python -m unittest -v test_archive_montage.py

These tests avoid requiring ffmpeg or ImageMagick by using --scan-only and by
mocking native command availability where needed. They include Unicode path tests
for cross-platform archival behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

# Ensure the sibling archive_montage.py is importable when tests are run directly.
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import archive_montage as am  # noqa: E402


class ArchiveMontageUnitTests(unittest.TestCase):
    def make_options(self, root: Path, **overrides) -> am.Options:
        """Create a minimal Options object suitable for unit tests."""
        values = dict(
            root=root,
            db=root / "archive_inventory.sqlite",
            output_dir=root / "Montages",
            scan_only=True,
            montage_only=False,
            no_hash=False,
            blake2b=False,
            no_ffprobe=True,
            no_magick_identify=True,
            no_video_montage=True,
            no_image_montage=True,
            recursive=True,
            per_directory_montage=True,
            video_fps="1",
            video_scale_width=200,
            video_tile="5x5",
            image_tile="15x15",
            image_geometry="300x300>",
            image_page_size=225,
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            magick="magick",
            timeout=5,
            include_exts=None,
            exclude_exts=set(am.DEFAULT_EXCLUDE_EXTENSIONS),
            exclude_names=set(am.DEFAULT_EXCLUDE_NAMES),
            dry_run=False,
        )
        values.update(overrides)
        return am.Options(**values)

    def test_parse_ext_list_normalizes_extensions(self):
        self.assertEqual(am.parse_ext_list("jpg, .MP4, , txt"), {".jpg", ".mp4", ".txt"})
        self.assertIsNone(am.parse_ext_list(None))
        self.assertIsNone(am.parse_ext_list(""))

    def test_safe_relative_returns_relative_inside_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "folder" / "file.txt"
            self.assertEqual(am.safe_relative(child, root), os.path.join("folder", "file.txt"))

    def test_path_text_strips_windows_extended_length_prefixes(self):
        self.assertEqual(am.path_text(r"\\?\C:\folder\file.txt"), r"C:\folder\file.txt")
        self.assertEqual(am.path_text(r"\\?\UNC\server\share\file.txt"), r"\\server\share\file.txt")

    @unittest.skipUnless(os.name == "nt", "Windows long-path prefix behavior")
    def test_native_path_prefixes_long_windows_paths_only_at_boundaries(self):
        base = Path(tempfile.gettempdir()).resolve()
        long_path = base / ("a" * 120) / ("b" * 120) / "file.txt"

        native = am.native_path(long_path)

        self.assertTrue(native.startswith(am.WINDOWS_LONG_PATH_PREFIX))
        self.assertEqual(am.path_text(native), str(long_path))

    @unittest.skipUnless(os.name == "nt", "Windows long-path prefix behavior")
    def test_native_command_args_use_extended_paths_for_long_inputs(self):
        base = Path(tempfile.gettempdir()).resolve()
        video = base / ("v" * 120) / ("x" * 120) / "video.mp4"
        out_file = base / ("o" * 120) / ("y" * 120) / "sheet.jpg"

        ffmpeg_args = am.build_ffmpeg_thumbnail_args(video, out_file, 200, 1.0, overlay_timestamp=False)
        magick_args = am.build_magick_video_sheet_args([video], [1.0], "1x1", out_file, labels_needed=True)

        self.assertIn(am.native_path(video), ffmpeg_args)
        self.assertEqual(ffmpeg_args[-1], am.native_path(out_file))
        self.assertIn(am.native_path(video), magick_args)
        self.assertEqual(magick_args[-1], am.native_path(out_file))

    def test_hash_functions_match_hashlib(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "café 😀.txt"
            data = "hello unicode 日本語\n".encode("utf-8")
            path.write_bytes(data)
            self.assertEqual(am.sha256_file(path), hashlib.sha256(data).hexdigest())
            self.assertEqual(am.blake2b_file(path), hashlib.blake2b(data).hexdigest())

    def test_extract_media_summary_from_ffprobe_payload(self):
        probe = {
            "format": {"duration": "12.5", "bit_rate": "123456", "format_name": "mov,mp4"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
        summary = am.extract_media_summary(probe)
        self.assertEqual(summary["duration_seconds"], 12.5)
        self.assertEqual(summary["bit_rate"], 123456)
        self.assertEqual(summary["format_name"], "mov,mp4")
        self.assertEqual(summary["video_codec"], "h264")
        self.assertEqual(summary["audio_codec"], "aac")
        self.assertEqual(summary["width"], 1920)
        self.assertEqual(summary["height"], 1080)
        self.assertEqual(summary["frame_rate"], "30000/1001")
        self.assertEqual(summary["stream_count"], 2)

    def test_video_montage_timestamps_use_tile_count_and_microsecond_step(self):
        columns, rows = am.parse_tile_dimensions("3x2")
        timestamps, step = am.video_montage_timestamps(10.0, columns * rows)

        self.assertEqual(columns * rows, 6)
        self.assertEqual(step, 1.666667)
        self.assertEqual(timestamps, [0.0, 1.666667, 3.333334, 5.000001, 6.666668, 8.333335])
        self.assertEqual(am.format_ffmpeg_timestamp(timestamps[1]), "00:00:01.666667")

    def test_should_include_excludes_database_output_and_blocklists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            options = self.make_options(root)
            options.output_dir.mkdir()
            self.assertFalse(am.should_include(options.db, options))
            self.assertFalse(am.should_include(root / "archive_inventory.sqlite-wal", options))
            self.assertFalse(am.should_include(options.output_dir / "generated.jpg", options))
            self.assertFalse(am.should_include(root / "Thumbs.db", options))
            self.assertFalse(am.should_include(root / "package.zip", options))
            self.assertTrue(am.should_include(root / "keep.mp4", options))
            options.include_exts = {".mp4"}
            self.assertTrue(am.should_include(root / "keep.mp4", options))
            self.assertFalse(am.should_include(root / "notes.txt", options))

    def test_init_db_creates_expected_tables(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            am.init_db(conn)
            table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"scans", "files", "media_metadata", "generated_artifacts"}.issubset(table_names))

    def test_insert_file_record_preserves_unicode_metadata_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "日本語 folder"
            nested.mkdir()
            path = nested / "café 😀.txt"
            payload = "archival text\n".encode("utf-8")
            path.write_bytes(payload)

            options = self.make_options(root)
            with closing(sqlite3.connect(":memory:")) as conn:
                am.init_db(conn)
                conn.execute(
                    "INSERT INTO scans (id, scan_started_utc, root_path, scanner_version) VALUES (?, ?, ?, ?)",
                    ("scan-1", am.utc_now_iso(), str(root), am.SCANNER_VERSION),
                )
                file_id = am.insert_file_record(conn, "scan-1", root, path, options)
                row = conn.execute(
                    "SELECT name, relative_path, extension, size_bytes, sha256, error FROM files WHERE id=?",
                    (file_id,),
                ).fetchone()

            self.assertEqual(row[0], "café 😀.txt")
            self.assertIn("日本語 folder", row[1])
            self.assertEqual(row[2], ".txt")
            self.assertEqual(row[3], len(payload))
            self.assertEqual(row[4], hashlib.sha256(payload).hexdigest())
            self.assertIsNone(row[5])

    def test_insert_file_record_can_skip_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "file.bin"
            path.write_bytes(b"abc")
            options = self.make_options(root, no_hash=True)
            with closing(sqlite3.connect(":memory:")) as conn:
                am.init_db(conn)
                conn.execute(
                    "INSERT INTO scans (id, scan_started_utc, root_path, scanner_version) VALUES (?, ?, ?, ?)",
                    ("scan-1", am.utc_now_iso(), str(root), am.SCANNER_VERSION),
                )
                file_id = am.insert_file_record(conn, "scan-1", root, path, options)
                sha256 = conn.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()[0]
            self.assertIsNone(sha256)

    def test_ffprobe_json_parses_valid_json_and_handles_invalid_json(self):
        good = subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout='{"format":{"duration":"1"}}', stderr="")
        bad_json = subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout="not json", stderr="warning")
        failed = subprocess.CompletedProcess(args=["ffprobe"], returncode=1, stdout="", stderr="error")
        with patch.object(am, "run_native", return_value=good):
            self.assertEqual(am.ffprobe_json(Path("video.mp4"), "ffprobe", 5), {"format": {"duration": "1"}})
        with patch.object(am, "run_native", return_value=bad_json):
            parsed = am.ffprobe_json(Path("video.mp4"), "ffprobe", 5)
            self.assertTrue(parsed["_parse_error"])
            self.assertEqual(parsed["stdout"], "not json")
        with patch.object(am, "run_native", return_value=failed):
            self.assertIsNone(am.ffprobe_json(Path("video.mp4"), "ffprobe", 5))

    def test_generate_video_montage_dry_run_records_command_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "動画.mp4"
            video.write_bytes(b"not a real video")
            options = self.make_options(root, scan_only=False, no_video_montage=False, dry_run=True)
            with closing(sqlite3.connect(":memory:")) as conn:
                am.init_db(conn)
                conn.execute(
                    "INSERT INTO scans (id, scan_started_utc, root_path, scanner_version) VALUES (?, ?, ?, ?)",
                    ("scan-1", am.utc_now_iso(), str(root), am.SCANNER_VERSION),
                )
                source_file_id = am.insert_file_record(conn, "scan-1", root, video, options)
                with patch.object(am, "command_available", return_value=True):
                    am.generate_video_montage(conn, "scan-1", source_file_id, video, root, options)
                row = conn.execute(
                    "SELECT artifact_type, artifact_path, command, exit_code, stdout FROM generated_artifacts"
                ).fetchone()

            self.assertEqual(row[0], "video_montage")
            self.assertIn("動画.mp4.jpg", row[1])
            self.assertEqual(row[3], 0)
            self.assertEqual(row[4], "DRY RUN")
            command = json.loads(row[2])
            self.assertEqual(command.count("-ss"), 25)
            self.assertIn("-filter_complex", command)
            filter_complex = command[command.index("-filter_complex") + 1]
            self.assertIn("scale=200:-1", filter_complex)
            self.assertIn("concat=n=25", filter_complex)
            self.assertIn("tile=5x5", filter_complex)
            self.assertIn("-y", command)
            self.assertNotIn("-n", command)
            self.assertIn("-frames:v", command)

    def test_main_direct_video_file_generates_sheet_without_scan_db(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "single.mp4"
            out_file = root / "sheet.jpg"
            video.write_bytes(b"not a real video")
            observed: dict[str, object] = {}

            def fake_generate(video_arg, out_arg, options, duration_seconds=None, progress=None):
                observed["video"] = video_arg
                observed["out"] = out_arg
                observed["root"] = options.root
                observed["tile"] = options.video_tile
                observed["width"] = options.video_scale_width
                observed["duration"] = duration_seconds
                observed["progress"] = callable(progress)
                return subprocess.CompletedProcess(["ffmpeg"], 0, "", ""), ["ffmpeg"]

            with patch.object(am, "init_db", side_effect=AssertionError("direct mode should not initialize SQLite")):
                with patch.object(am, "generate_video_thumbnail_sheet", side_effect=fake_generate):
                    exit_code = am.main([
                        "--video-file", str(video),
                        "--video-output", str(out_file),
                        "--video-tile", "2x2",
                        "--video-thumbnail-width", "123",
                    ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(observed["video"], video.resolve())
            self.assertEqual(observed["out"], out_file.resolve())
            self.assertEqual(observed["root"], root.resolve())
            self.assertEqual(observed["tile"], "2x2")
            self.assertEqual(observed["width"], 123)
            self.assertIsNone(observed["duration"])
            self.assertTrue(observed["progress"])

    def test_main_generates_video_montages_during_single_scan_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            db = root / "inventory.sqlite"
            out = root / "Montages"
            observed: list[tuple[str, list[str]]] = []

            def fake_generate(conn, scan_id, source_file_id, video, scan_root, options):
                with closing(sqlite3.connect(options.db)) as read_conn:
                    names = [row[0] for row in read_conn.execute("SELECT name FROM files ORDER BY id")]
                observed.append((video.name, names))

            with patch.object(am, "iter_paths", return_value=iter([first, second])):
                with patch.object(am, "generate_video_montage", side_effect=fake_generate):
                    exit_code = am.main([
                        "--root", str(root),
                        "--db", str(db),
                        "--output-dir", str(out),
                        "--include-exts", ".mp4",
                        "--no-image-montage",
                        "--no-ffprobe",
                        "--no-magick-identify",
                        "--no-hash",
                    ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(observed, [
                ("first.mp4", ["first.mp4"]),
                ("second.mp4", ["first.mp4", "second.mp4"]),
            ])

    def test_main_generates_image_pages_during_single_scan_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first.jpg"
            second = root / "second.jpg"
            third = root / "third.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            third.write_bytes(b"third")
            db = root / "inventory.sqlite"
            out = root / "Montages"
            observed: list[tuple[int, list[str], list[str]]] = []

            def fake_generate_page(conn, scan_id, scan_root, source_dir, page_files, page_num, options):
                with closing(sqlite3.connect(options.db)) as read_conn:
                    names = [row[0] for row in read_conn.execute("SELECT name FROM files ORDER BY id")]
                observed.append((page_num, [p.name for p in page_files], names))

            with patch.object(am, "iter_paths", return_value=iter([first, second, third])):
                with patch.object(am, "generate_image_montage_page", side_effect=fake_generate_page):
                    exit_code = am.main([
                        "--root", str(root),
                        "--db", str(db),
                        "--output-dir", str(out),
                        "--include-exts", ".jpg",
                        "--no-video-montage",
                        "--no-ffprobe",
                        "--no-magick-identify",
                        "--no-hash",
                        "--image-page-size", "2",
                    ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(observed, [
                (1, ["first.jpg", "second.jpg"], ["first.jpg", "second.jpg"]),
                (2, ["third.jpg"], ["first.jpg", "second.jpg", "third.jpg"]),
            ])

    def test_main_scan_only_creates_database_with_unicode_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "日本語 folder").mkdir()
            sample = root / "日本語 folder" / "café 😀.txt"
            sample.write_text("hello", encoding="utf-8")
            db = root / "inventory.sqlite"
            out = root / "Montages"

            exit_code = am.main([
                "--root", str(root),
                "--db", str(db),
                "--output-dir", str(out),
                "--scan-only",
                "--no-ffprobe",
                "--no-magick-identify",
            ])

            self.assertEqual(exit_code, 0)
            with closing(sqlite3.connect(db)) as conn:
                names = {row[0] for row in conn.execute("SELECT name FROM files")}
                scans_count = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
            self.assertIn("café 😀.txt", names)
            self.assertEqual(scans_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
