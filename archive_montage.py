#!/usr/bin/env python3
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

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import mimetypes
import os
import platform
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

SCANNER_VERSION = "2026.06.06-1"

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".flv",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".ogv",
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".avif",
}

DEFAULT_EXCLUDE_NAMES = {
    "thumbs.db", ".ds_store",
}

DEFAULT_EXCLUDE_EXTENSIONS = {
    ".db", ".sqlite", ".sqlite3", ".ini", ".xml", ".7z", ".zip",".py",".ps1",".git",".md"
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_from_timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def path_text(path: Path) -> str:
    return str(path)


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def blake2b_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.blake2b()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_native(
    exe: str,
    args: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    """Run a native command with argument-array semantics and UTF-8 text capture."""
    return subprocess.run(
        [exe, *args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def file_birth_time(st: os.stat_result) -> tuple[Optional[float], bool]:
    """Return best available birth/creation time and whether it is true birth time."""
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return float(birth), True
    if platform.system().lower() == "windows":
        # On Windows, st_ctime is creation time.
        return float(st.st_ctime), True
    return None, False


def metadata_changed_time(st: os.stat_result) -> Optional[float]:
    if platform.system().lower() == "windows":
        # Windows st_ctime is creation time, not POSIX metadata-change time.
        return None
    return float(st.st_ctime)


def get_owner_group(path: Path) -> tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    owner = group = None
    uid = gid = None
    try:
        st = path.lstat()
        uid = getattr(st, "st_uid", None)
        gid = getattr(st, "st_gid", None)
        if os.name != "nt":
            try:
                import pwd
                owner = pwd.getpwuid(uid).pw_name if uid is not None else None
            except Exception:
                owner = None
            try:
                import grp
                group = grp.getgrgid(gid).gr_name if gid is not None else None
            except Exception:
                group = None
    except OSError:
        pass
    return owner, group, uid, gid


def ffprobe_json(path: Path, ffprobe_exe: str, timeout: int) -> Optional[dict]:
    result = run_native(
        ffprobe_exe,
        [
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=timeout,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"_parse_error": True, "stdout": result.stdout, "stderr": result.stderr}


def magick_identify_json(path: Path, magick_exe: str, timeout: int) -> Optional[dict | str]:
    # ImageMagick v7 usually supports JSON via identify -format %j.
    result = run_native(
        magick_exe,
        ["identify", "-format", "%j", str(path)],
        timeout=timeout,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout


def extract_media_summary(probe: Optional[dict]) -> dict:
    summary = {
        "duration_seconds": None,
        "bit_rate": None,
        "format_name": None,
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
        "frame_rate": None,
        "stream_count": None,
    }
    if not probe:
        return summary

    fmt = probe.get("format") or {}
    summary["duration_seconds"] = _float_or_none(fmt.get("duration"))
    summary["bit_rate"] = _int_or_none(fmt.get("bit_rate"))
    summary["format_name"] = fmt.get("format_name")

    streams = probe.get("streams") or []
    summary["stream_count"] = len(streams)
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and summary["video_codec"] is None:
            summary["video_codec"] = stream.get("codec_name")
            summary["width"] = _int_or_none(stream.get("width"))
            summary["height"] = _int_or_none(stream.get("height"))
            summary["frame_rate"] = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        elif codec_type == "audio" and summary["audio_codec"] is None:
            summary["audio_codec"] = stream.get("codec_name")
    return summary


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY,
            scan_started_utc TEXT NOT NULL,
            scan_finished_utc TEXT,
            root_path TEXT NOT NULL,
            scanner_version TEXT NOT NULL,
            hostname TEXT,
            username TEXT,
            platform TEXT,
            python_version TEXT,
            ffmpeg_available INTEGER,
            ffprobe_available INTEGER,
            magick_available INTEGER
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            full_path TEXT NOT NULL,
            relative_path TEXT,
            parent_path TEXT,
            name TEXT NOT NULL,
            stem TEXT,
            extension TEXT,
            casefold_path TEXT,
            size_bytes INTEGER,
            is_file INTEGER,
            is_dir INTEGER,
            is_symlink INTEGER,
            link_target TEXT,
            device_id INTEGER,
            inode INTEGER,
            mode_text TEXT,
            mode_octal TEXT,
            readonly INTEGER,
            owner_name TEXT,
            group_name TEXT,
            uid INTEGER,
            gid INTEGER,
            created_time_utc TEXT,
            birth_time_available INTEGER,
            modified_time_utc TEXT,
            accessed_time_utc TEXT,
            metadata_changed_time_utc TEXT,
            ctime_raw_utc TEXT,
            mime_guess TEXT,
            sha256 TEXT,
            blake2b TEXT,
            scan_time_utc TEXT NOT NULL,
            stat_json TEXT,
            error TEXT,
            FOREIGN KEY(scan_id) REFERENCES scans(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_files_scan_path ON files(scan_id, full_path);
        CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
        CREATE INDEX IF NOT EXISTS idx_files_relative_path ON files(relative_path);

        CREATE TABLE IF NOT EXISTS media_metadata (
            file_id INTEGER PRIMARY KEY,
            duration_seconds REAL,
            bit_rate INTEGER,
            format_name TEXT,
            video_codec TEXT,
            audio_codec TEXT,
            width INTEGER,
            height INTEGER,
            frame_rate TEXT,
            stream_count INTEGER,
            ffprobe_json TEXT,
            magick_identify_json TEXT,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS generated_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            source_file_id INTEGER,
            artifact_type TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            command TEXT,
            exit_code INTEGER,
            stdout TEXT,
            stderr TEXT,
            created_time_utc TEXT NOT NULL,
            FOREIGN KEY(scan_id) REFERENCES scans(id),
            FOREIGN KEY(source_file_id) REFERENCES files(id)
        );
        """
    )
    conn.commit()


@dataclass
class Options:
    root: Path
    db: Path
    output_dir: Path
    scan_only: bool
    montage_only: bool
    no_hash: bool
    blake2b: bool
    no_ffprobe: bool
    no_magick_identify: bool
    no_video_montage: bool
    no_image_montage: bool
    recursive: bool
    per_directory_montage: bool
    video_fps: str
    video_scale_width: int
    video_tile: str
    image_tile: str
    image_geometry: str
    image_page_size: int
    ffmpeg: str
    ffprobe: str
    magick: str
    timeout: int
    include_exts: Optional[set[str]]
    exclude_exts: set[str]
    exclude_names: set[str]
    dry_run: bool


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def should_include(path: Path, options: Options) -> bool:
    try:
        resolved = path.resolve()
        db_resolved = options.db.resolve()
        db_sidecars = {db_resolved.with_name(db_resolved.name + suffix) for suffix in ("-wal", "-shm", "-journal")}
        if resolved == db_resolved or resolved in db_sidecars or is_relative_to(resolved, options.output_dir):
            return False
    except OSError:
        pass
    name_l = path.name.lower()
    ext_l = path.suffix.lower()

    if options.include_exts is None:
        if name_l in options.exclude_names:
            return False
        if ext_l in options.exclude_exts:
            return False
    else:
        if ext_l in options.include_exts:
            return True
        elif name_l in options.exclude_names:
            return False
        elif ext_l in options.exclude_exts:
            return False
    #return True


def iter_paths(root: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        yield from root.rglob("*")
    else:
        yield from root.iterdir()


def insert_file_record(conn: sqlite3.Connection, scan_id: str, root: Path, path: Path, options: Options) -> int:
    scan_time = utc_now_iso()
    try:
        st = path.lstat()
        birth_ts, birth_available = file_birth_time(st)
        meta_ts = metadata_changed_time(st)
        owner, group, uid, gid = get_owner_group(path)
        is_file = path.is_file()
        is_dir = path.is_dir()
        is_symlink = path.is_symlink()
        link_target = None
        if is_symlink:
            try:
                link_target = os.readlink(path)
            except OSError:
                link_target = None

        sha256 = None
        blake2b = None

        if is_file and not options.no_hash:
            size_bytes = st.st_size if is_file else None
            #print(size_bytes)
            modified_time_utc = iso_from_timestamp(st.st_mtime)
            #print(modified_time_utc)
            existing = conn.execute(
                """
                SELECT id
                FROM files
                WHERE size_bytes = ?
                AND modified_time_utc = ?
                LIMIT 1
            """,(size_bytes, modified_time_utc,)).fetchone()
            #print(existing)
            if (existing):
                print(
                    f"Skipping duplicate size_byte and modified_time_utc: {path} "
                    f"(matches {existing[0]})",
                    flush=True,
                )
                return 0  # special value indicating skipped
            else:
                sha256 = sha256_file(path)
                existing = conn.execute(
                    """
                    SELECT id, full_path
                    FROM files
                    WHERE sha256 = ?
                    LIMIT 1
                    """,
                    (sha256,)
                ).fetchone()
                if existing:
                    print(
                        f"Skipping duplicate SHA256: {path} "
                        f"(matches {existing[0]})",
                        flush=True,
                    )
                    return 0  # special value indicating skipped

                if options.blake2b:
                    blake2b = blake2b_file(path)

        stat_payload = {
            key: getattr(st, key)
            for key in dir(st)
            if key.startswith("st_") and isinstance(getattr(st, key), (int, float, str, type(None)))
        }

        values = {
            "scan_id": scan_id,
            "full_path": path_text(path),
            "relative_path": safe_relative(path, root),
            "parent_path": path_text(path.parent),
            "name": path.name,
            "stem": path.stem,
            "extension": path.suffix.lower(),
            "casefold_path": path_text(path).casefold(),
            "size_bytes": st.st_size if is_file else None,
            "is_file": int(is_file),
            "is_dir": int(is_dir),
            "is_symlink": int(is_symlink),
            "link_target": link_target,
            "device_id": getattr(st, "st_dev", None),
            "inode": getattr(st, "st_ino", None),
            "mode_text": stat.filemode(st.st_mode),
            "mode_octal": oct(st.st_mode),
            "readonly": int(not bool(st.st_mode & stat.S_IWUSR)),
            "owner_name": owner,
            "group_name": group,
            "uid": uid,
            "gid": gid,
            "created_time_utc": iso_from_timestamp(birth_ts),
            "birth_time_available": int(birth_available),
            "modified_time_utc": iso_from_timestamp(st.st_mtime),
            "accessed_time_utc": iso_from_timestamp(st.st_atime),
            "metadata_changed_time_utc": iso_from_timestamp(meta_ts),
            "ctime_raw_utc": iso_from_timestamp(st.st_ctime),
            "mime_guess": mimetypes.guess_type(path.name)[0],
            "sha256": sha256,
            "blake2b": blake2b,
            "scan_time_utc": scan_time,
            "stat_json": json.dumps(stat_payload, ensure_ascii=False, sort_keys=True),
            "error": None,
        }
    except Exception as exc:
        values = {
            "scan_id": scan_id,
            "full_path": path_text(path),
            "relative_path": safe_relative(path, root),
            "parent_path": path_text(path.parent),
            "name": path.name,
            "stem": path.stem,
            "extension": path.suffix.lower(),
            "casefold_path": path_text(path).casefold(),
            "size_bytes": None,
            "is_file": None,
            "is_dir": None,
            "is_symlink": None,
            "link_target": None,
            "device_id": None,
            "inode": None,
            "mode_text": None,
            "mode_octal": None,
            "readonly": None,
            "owner_name": None,
            "group_name": None,
            "uid": None,
            "gid": None,
            "created_time_utc": None,
            "birth_time_available": 0,
            "modified_time_utc": None,
            "accessed_time_utc": None,
            "metadata_changed_time_utc": None,
            "ctime_raw_utc": None,
            "mime_guess": None,
            "sha256": None,
            "blake2b": None,
            "scan_time_utc": scan_time,
            "stat_json": None,
            "error": repr(exc),
        }

    cols = list(values.keys())
    placeholders = ",".join([":" + c for c in cols])
    conn.execute(
        f"INSERT INTO files ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def insert_media_metadata(conn: sqlite3.Connection, file_id: int, path: Path, options: Options) -> None:
    ext = path.suffix.lower()
    probe = None
    identify = None
    if ext in VIDEO_EXTENSIONS and not options.no_ffprobe and command_available(options.ffprobe):
        probe = ffprobe_json(path, options.ffprobe, options.timeout)
    if ext in IMAGE_EXTENSIONS and not options.no_magick_identify and command_available(options.magick):
        identify = magick_identify_json(path, options.magick, options.timeout)

    if probe is None and identify is None:
        return

    summary = extract_media_summary(probe)
    conn.execute(
        """
        INSERT OR REPLACE INTO media_metadata (
            file_id, duration_seconds, bit_rate, format_name, video_codec, audio_codec,
            width, height, frame_rate, stream_count, ffprobe_json, magick_identify_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            summary["duration_seconds"],
            summary["bit_rate"],
            summary["format_name"],
            summary["video_codec"],
            summary["audio_codec"],
            summary["width"],
            summary["height"],
            summary["frame_rate"],
            summary["stream_count"],
            json.dumps(probe, ensure_ascii=False, sort_keys=True) if probe is not None else None,
            json.dumps(identify, ensure_ascii=False, sort_keys=True) if identify is not None else None,
        ),
    )


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
    out_pattern = out_dir / f"{video.name}.%03d.jpg"
    vf = f"fps={options.video_fps},scale={options.video_scale_width}:-1,tile={options.video_tile}"
    args = ["-hide_banner", "-n", "-i", str(video), "-vf", vf, str(out_pattern)]
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


def chunks(items: Sequence[Path], size: int) -> Iterable[Sequence[Path]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def generate_image_montages(conn: sqlite3.Connection, scan_id: str, root: Path, image_files: Sequence[Path], options: Options) -> None:
    if options.no_image_montage or options.scan_only:
        return
    if not image_files or not command_available(options.magick):
        return

    by_dir: dict[Path, list[Path]] = {}
    if options.per_directory_montage:
        for p in image_files:
            by_dir.setdefault(p.parent, []).append(p)
    else:
        by_dir[root] = list(image_files)

    for source_dir, files in by_dir.items():
        out_dir = artifact_dir_for(source_dir, root, options.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for page_num, page_files in enumerate(chunks(files, options.image_page_size), start=1):
            out_file = out_dir / f"montage-{page_num:03d}.jpg"
            args: list[str] = ["montage"]
            for image in page_files:
                # ImageMagick geometry suffix is intentionally a separate path-like token.
                args.append(f"{image}[{options.image_geometry}]")
            args.extend([
                "-auto-orient",
                "-mode", "concatenate",
                "-set", "label", "%f",
                "-tile", options.image_tile,
                "-background", "#AB82FF",
                str(out_file),
            ])
            if options.dry_run:
                result = subprocess.CompletedProcess([options.magick, *args], 0, "DRY RUN", "")
            else:
                result = run_native(options.magick, args, timeout=options.timeout)
            conn.execute(
                """
                INSERT INTO generated_artifacts
                (scan_id, source_file_id, artifact_type, artifact_path, command, exit_code, stdout, stderr, created_time_utc)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    "image_montage",
                    str(out_file),
                    json.dumps([options.magick, *args], ensure_ascii=False),
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    utc_now_iso(),
                ),
            )


def parse_ext_list(value: Optional[str]) -> Optional[set[str]]:
    if not value:
        return None
    result = set()
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        result.add(item)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform archival scanner and montage generator.")
    parser.add_argument("--root", required=True, type=Path, help="Root folder to scan/process.")
    parser.add_argument("--db", type=Path, help="SQLite database path. Default: <root>/archive_inventory.sqlite")
    parser.add_argument("--output-dir", type=Path, help="Montage output folder. Default: <root>/Montages")
    parser.add_argument("--scan-only", action="store_true", help="Record SQLite metadata only; do not generate montages.")
    parser.add_argument("--montage-only", action="store_true", help="Generate montages only; still records minimal scan rows for artifact linkage.")
    parser.add_argument("--no-hash", action="store_true", help="Skip SHA256 hashing.")
    parser.add_argument("--blake2b", action="store_true", help="Also compute BLAKE2b checksums.")
    parser.add_argument("--no-ffprobe", action="store_true", help="Skip ffprobe media metadata.")
    parser.add_argument("--no-magick-identify", action="store_true", help="Skip ImageMagick identify metadata.")
    parser.add_argument("--no-video-montage", action="store_true", help="Do not generate video contact sheets.")
    parser.add_argument("--no-image-montage", action="store_true", help="Do not generate image contact sheets.")
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True, help="Walk recursively. Default: true.")
    parser.add_argument("--per-directory-montage", action=argparse.BooleanOptionalAction, default=True, help="Create image montages per source directory. Default: true.")
    parser.add_argument("--video-fps", default="0.1", help="FFmpeg fps filter value for video thumbnails. Default: 0.1.")
    parser.add_argument("--video-scale-width", type=int, default=400, help="Video thumbnail width. Default: 400.")
    parser.add_argument("--video-tile", default="15x15", help="FFmpeg tile layout. Default: 15x15.")
    parser.add_argument("--image-tile", default="15x15", help="ImageMagick montage tile layout. Default: 15x15.")
    parser.add_argument("--image-geometry", default="300x300>", help="Image resize geometry before montage. Default: 300x300>.")
    parser.add_argument("--image-page-size", type=int, default=225, help="Images per montage page. Default: 225.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable name/path.")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable name/path.")
    parser.add_argument("--magick", default="magick", help="ImageMagick executable name/path.")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-command timeout in seconds. Default: 1800.")
    parser.add_argument("--include-exts", default=",".join(sorted(VIDEO_EXTENSIONS | IMAGE_EXTENSIONS)), help="Comma-separated extension allowlist, e.g. .mp4,.jpg")
    parser.add_argument("--exclude-exts", default=",".join(sorted(DEFAULT_EXCLUDE_EXTENSIONS)), help="Comma-separated extension blocklist.")
    parser.add_argument("--exclude-names", default=",".join(sorted(DEFAULT_EXCLUDE_NAMES)), help="Comma-separated filename blocklist.")
    parser.add_argument("--dry-run", action="store_true", help="Do not run ffmpeg/magick montage commands; record intended commands.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Root is not a directory: {root}", file=sys.stderr)
        return 2

    db = (args.db or (root / "archive_inventory.sqlite")).expanduser().resolve()
    output_dir = (args.output_dir or (root / "Montages")).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    options = Options(
        root=root,
        db=db,
        output_dir=output_dir,
        scan_only=args.scan_only,
        montage_only=args.montage_only,
        no_hash=args.no_hash,
        blake2b=args.blake2b,
        no_ffprobe=args.no_ffprobe,
        no_magick_identify=args.no_magick_identify,
        no_video_montage=args.no_video_montage,
        no_image_montage=args.no_image_montage,
        recursive=args.recursive,
        per_directory_montage=args.per_directory_montage,
        video_fps=args.video_fps,
        video_scale_width=args.video_scale_width,
        video_tile=args.video_tile,
        image_tile=args.image_tile,
        image_geometry=args.image_geometry,
        image_page_size=args.image_page_size,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        magick=args.magick,
        timeout=args.timeout,
        include_exts=parse_ext_list(args.include_exts),
        exclude_exts=parse_ext_list(args.exclude_exts) or set(),
        exclude_names={x.strip().lower() for x in args.exclude_names.split(",") if x.strip()},
        dry_run=args.dry_run,
    )

    scan_id = str(uuid.uuid4())
    started = utc_now_iso()
    video_files: list[tuple[int, Path]] = []
    image_files: list[Path] = []
    count = 0

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        init_db(conn)
        conn.execute(
            """
            INSERT INTO scans
            (id, scan_started_utc, root_path, scanner_version, hostname, username, platform,
             python_version, ffmpeg_available, ffprobe_available, magick_available)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                started,
                str(root),
                SCANNER_VERSION,
                socket.gethostname(),
                getpass.getuser(),
                platform.platform(),
                sys.version,
                int(command_available(options.ffmpeg)),
                int(command_available(options.ffprobe)),
                int(command_available(options.magick)),
            ),
        )
        conn.commit()

        for path in iter_paths(root, options.recursive):
            if not should_include(path, options):
                continue
            if not path.exists() and not path.is_symlink():
                continue
            file_id = insert_file_record(conn, scan_id, root, path, options)
            if file_id == 0:
                continue
            count += 1
            if path.is_file():
                ext = path.suffix.lower()
                if not options.montage_only:
                    insert_media_metadata(conn, file_id, path, options)
                if ext in VIDEO_EXTENSIONS:
                    video_files.append((file_id, path))
                elif ext in IMAGE_EXTENSIONS and not path.name.lower().startswith("montage-"):
                    image_files.append(path)
            if count % 100 == 0:
                conn.commit()
                print(f"Recorded {count} paths...", flush=True)

        conn.commit()

        for file_id, video in video_files:
            print(f"Video montage: {video}", flush=True)
            generate_video_montage(conn, scan_id, file_id, video, root, options)
            conn.commit()

        print(f"Image montage source files: {len(image_files)}", flush=True)
        generate_image_montages(conn, scan_id, root, image_files, options)
        conn.commit()

        conn.execute("UPDATE scans SET scan_finished_utc=? WHERE id=?", (utc_now_iso(), scan_id))
        conn.commit()

    print(f"Done. Recorded {count} paths.")
    print(f"SQLite DB: {db}")
    print(f"Montages: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
