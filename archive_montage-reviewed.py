"""
archive_montage.py

Cross-platform archival scanner and montage generator.

Primary goals:
  - Walk directories with Unicode-safe Python pathlib/os APIs.
  - Record portable archival file properties to SQLite.
  - Capture technical media metadata using ffprobe and ImageMagick when available.
  - Generate video contact sheets with ffmpeg.
  - Generate image contact sheets with ImageMagick magick montage.

Works on Windows, Linux, and macOS with Python 3.9+.
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import shutil

SCANNER_VERSION = "2026.06.06-1"

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".flv",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".ogv",
}

DEFAULT_EXCLUDE_NAMES = {
    "thumbs.db", ".ds_store",
}

DEFAULT_EXCLUDE_EXTENSIONS = {
    ".db", ".sqlite", ".sqlite3", ".ini", ".xml", ".7z", ".zip",
}

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform archival scanner and montage generator.")
    parser.add_argument("--root", required=True, type=Path, help="Root folder to scan/process.")
    #parser.add_argument("--db", type=Path, help="SQLite database path. Default: <root>/archive_inventory.sqlite")
    #parser.add_argument("--output-dir", type=Path, help="Montage output folder. Default: <root>/Montages")
    parser.add_argument("--scan-only", action="store_true", help="Record SQLite metadata only; do not generate montages.")
    parser.add_argument("--montage-only", action="store_true", help="Generate montages only; still records minimal scan rows for artifact linkage.")
    #parser.add_argument("--no-hash", action="store_true", help="Skip SHA256 hashing.")
    #parser.add_argument("--blake2b", action="store_true", help="Also compute BLAKE2b checksums.")
    #parser.add_argument("--no-ffprobe", action="store_true", help="Skip ffprobe media metadata.")
    #parser.add_argument("--no-magick-identify", action="store_true", help="Skip ImageMagick identify metadata.")
    parser.add_argument("--no-video-montage", action="store_true", help="Do not generate video contact sheets.")
    #parser.add_argument("--no-image-montage", action="store_true", help="Do not generate image contact sheets.")
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True, help="Walk recursively. Default: true.")
    parser.add_argument("--per-directory-montage", action=argparse.BooleanOptionalAction, default=True, help="Create image montages per source directory. Default: true.")
    parser.add_argument("--video-fps", default="1", help="FFmpeg fps filter value for video thumbnails. Default: 1.")
    parser.add_argument("--video-scale-width", type=int, default=200, help="Video thumbnail width. Default: 200.")
    parser.add_argument("--video-tile", default="5x5", help="FFmpeg tile layout. Default: 5x5.")
    parser.add_argument("--image-tile", default="15x15", help="ImageMagick montage tile layout. Default: 15x15.")
    parser.add_argument("--image-geometry", default="300x300>", help="Image resize geometry before montage. Default: 300x300>.")
    parser.add_argument("--image-page-size", type=int, default=225, help="Images per montage page. Default: 225.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable name/path.")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable name/path.")
    parser.add_argument("--magick", default="magick", help="ImageMagick executable name/path.")
    parser.add_argument("--timeout", type=int, default=600, help="Per-command timeout in seconds. Default: 600.")
    parser.add_argument("--include-exts", help="Comma-separated extension allowlist, e.g. .mp4,.jpg")
    parser.add_argument("--exclude-exts", default=",".join(sorted(DEFAULT_EXCLUDE_EXTENSIONS)), help="Comma-separated extension blocklist.")
    parser.add_argument("--exclude-names", default=",".join(sorted(DEFAULT_EXCLUDE_NAMES)), help="Comma-separated filename blocklist.")
    parser.add_argument("--dry-run", action="store_true", help="Do not run ffmpeg/magick montage commands; record intended commands.")
    return parser

def command_available(name: str) -> bool:
    return shutil.which(name) is not None

def path_text(path: Path) -> str:
    return str(path)

def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
    
def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
    
def iter_paths(root: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        yield from root.rglob("*")
    else:
        yield from root.iterdir()

def artifact_dir_for(source_dir: Path, root: Path, output_dir: Path) -> Path:
    rel = safe_relative(source_dir, root)
    safe_rel = Path(rel) if rel not in (".", "") else Path("root")
    return output_dir / safe_rel / "montages"

def generate_video_montage(conn: sqlite3.Connection, scan_id: str, source_file_id: int, video: Path, root: Path, options: Options) -> None:
    if options.no_video_montage or options.scan_only:
        return
    if not command_available(options.ffmpeg):
        return
    out_dir = artifact_dir_for(video.parent, root, options.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = out_dir / f"{video.name}.%02d.jpg"
    vf = f"fps={options.video_fps},scale={options.video_scale_width}:-1,tile={options.video_tile}"
    args = ["-hide_banner", "-y", "-i", str(video), "-vf", vf, str(out_pattern)]
    if options.dry_run:
        result = subprocess.CompletedProcess([options.ffmpeg, *args], 0, "DRY RUN", "")
    else:
        result = run_native(options.ffmpeg, args, timeout=options.timeout)
    conn.execute(
        """
        INSERT INTO generated_artifacts
        (scan_id, source_file_id, artifact_type, artifact_path, command, exit_code, stdout, stderr, created_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_id,
            source_file_id,
            "video_montage",
            str(out_pattern),
            json.dumps([options.ffmpeg, *args], ensure_ascii=False),
            result.returncode,
            result.stdout,
            result.stderr,
            utc_now_iso(),
        ),
    )