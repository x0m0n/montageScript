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
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

SCANNER_VERSION = "2026.06.12-2"

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


SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1


def sqlite_int_or_none(value) -> Optional[int]:
    int_value = _int_or_none(value)
    if int_value is None:
        return None
    if SQLITE_INT_MIN <= int_value <= SQLITE_INT_MAX:
        return int_value
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

    if name_l in options.exclude_names:
        return False
    if ext_l in options.exclude_exts:
        return False
    if options.include_exts is not None:
        return ext_l in options.include_exts
    return True


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
            "device_id": sqlite_int_or_none(getattr(st, "st_dev", None)),
            "inode": sqlite_int_or_none(getattr(st, "st_ino", None)),
            "mode_text": stat.filemode(st.st_mode),
            "mode_octal": oct(st.st_mode),
            "readonly": int(not bool(st.st_mode & stat.S_IWUSR)),
            "owner_name": owner,
            "group_name": group,
            "uid": sqlite_int_or_none(uid),
            "gid": sqlite_int_or_none(gid),
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


def parse_tile_dimensions(tile: str) -> tuple[int, int]:
    parts = tile.lower().split("x", 1)
    if len(parts) != 2:
        raise ValueError(f"Tile must be formatted as COLUMNSxROWS, got: {tile!r}")
    try:
        columns = int(parts[0])
        rows = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Tile must contain integer dimensions, got: {tile!r}") from exc
    if columns < 1 or rows < 1:
        raise ValueError(f"Tile dimensions must be positive, got: {tile!r}")
    return columns, rows


def probe_video_duration_seconds(video: Path, options: Options) -> Optional[float]:
    if options.no_ffprobe or not command_available(options.ffprobe):
        return None
    probe = ffprobe_json(video, options.ffprobe, options.timeout)
    duration = _float_or_none(extract_media_summary(probe)["duration_seconds"])
    if duration is not None and duration > 0:
        return duration
    return None


def video_duration_seconds(conn: sqlite3.Connection, source_file_id: int, video: Path, options: Options) -> Optional[float]:
    row = conn.execute(
        "SELECT duration_seconds FROM media_metadata WHERE file_id=?",
        (source_file_id,),
    ).fetchone()
    if row is not None:
        duration = _float_or_none(row[0])
        if duration is not None and duration > 0:
            return duration

    if options.dry_run or options.no_ffprobe or not command_available(options.ffprobe):
        return None

    return probe_video_duration_seconds(video, options)


def video_montage_timestamps(duration_seconds: float, thumbnail_count: int) -> tuple[list[float], float]:
    step_seconds = max(0.000001, round(duration_seconds / thumbnail_count, 6))
    max_timestamp = max(0.0, duration_seconds - 0.000001)
    timestamps = [min(round(i * step_seconds, 6), max_timestamp) for i in range(thumbnail_count)]
    return timestamps, step_seconds


def format_ffmpeg_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    whole_seconds = int(seconds)
    microseconds = int(round((seconds - whole_seconds) * 1_000_000))
    if microseconds == 1_000_000:
        whole_seconds += 1
        microseconds = 0
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{microseconds:06d}"


def ffmpeg_filter_escape(value: str) -> str:
    return (
        value
        .replace("\\", "/")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )


def ffmpeg_drawtext_fontfile() -> Optional[str]:
    if platform.system().lower() != "windows":
        return None
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for font_name in ("arial.ttf", "segoeui.ttf", "calibri.ttf"):
        font_file = windir / "Fonts" / font_name
        if font_file.exists():
            return str(font_file)
    return None


def ffmpeg_drawtext_filter(label: str, width: int) -> str:
    escaped_label = ffmpeg_filter_escape(label)
    fontfile = ffmpeg_drawtext_fontfile()
    fontfile_option = f"fontfile='{ffmpeg_filter_escape(fontfile)}':" if fontfile else ""
    font_size = max(12, min(32, width // 18))
    return (
        "drawtext="
        f"{fontfile_option}"
        f"text='{escaped_label}':"
        f"fontsize={font_size}:"
        "fontcolor=white:"
        "box=1:"
        "boxcolor=black@0.65:"
        "boxborderw=4:"
        "x=6:"
        "y=h-th-6"
    )


def run_native_result(exe: str, args: Sequence[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess[str]:
    try:
        return run_native(exe, args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([exe, *args], 1, "", repr(exc))


def build_ffmpeg_video_sheet_args(
    video: Path,
    out_file: Path,
    thumbnail_width: int,
    tile: str,
    timestamps: Sequence[float],
) -> list[str]:
    args: list[str] = ["-hide_banner", "-y"]
    labels = [format_ffmpeg_timestamp(timestamp) for timestamp in timestamps]
    for label in labels:
        args.extend(["-ss", label, "-i", str(video)])

    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    for index, label in enumerate(labels):
        out_name = f"v{index}"
        filter_parts.append(
            f"[{index}:v]trim=end_frame=1,setpts=PTS-STARTPTS,"
            f"scale={thumbnail_width}:-1,{ffmpeg_drawtext_filter(label, thumbnail_width)}[{out_name}]"
        )
        concat_inputs.append(f"[{out_name}]")
    filter_parts.append(
        f"{''.join(concat_inputs)}concat=n={len(timestamps)}:v=1:a=0,"
        f"tile={tile}[out]"
    )
    args.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "[out]",
        "-frames:v", "1",
        "-update", "1",
        str(out_file),
    ])
    return args


def build_ffmpeg_thumbnail_args(
    video: Path,
    out_file: Path,
    thumbnail_width: int,
    timestamp: float,
    overlay_timestamp: bool,
) -> list[str]:
    label = format_ffmpeg_timestamp(timestamp)
    vf = f"scale={thumbnail_width}:-1"
    if overlay_timestamp:
        vf = f"{vf},{ffmpeg_drawtext_filter(label, thumbnail_width)}"
    return [
        "-hide_banner", "-y",
        "-ss", label,
        "-i", str(video),
        "-map", "0:v:0",
        "-frames:v", "1",
        "-update", "1",
        "-vf", vf,
        str(out_file),
    ]


def build_magick_video_sheet_args(
    thumb_files: Sequence[Path],
    timestamps: Sequence[float],
    tile: str,
    out_file: Path,
    labels_needed: bool,
) -> list[str]:
    args: list[str] = ["montage"]
    if labels_needed:
        args.extend(["-background", "#000000", "-fill", "white", "-pointsize", "14"])
        for thumb, timestamp in zip(thumb_files, timestamps):
            args.extend(["-label", format_ffmpeg_timestamp(timestamp), str(thumb)])
    else:
        args.extend(str(thumb) for thumb in thumb_files)
    args.extend([
        "-mode", "concatenate",
        "-tile", tile,
        "-background", "#000000",
        str(out_file),
    ])
    return args


def generate_video_sheet_with_magick_fallback(
    video: Path,
    out_file: Path,
    thumbnail_width: int,
    tile: str,
    timestamps: Sequence[float],
    options: Options,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    commands: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="archive-montage-video-") as tmp:
        tmp_dir = Path(tmp)
        thumb_files = [tmp_dir / f"thumb-{index + 1:03d}.jpg" for index in range(len(timestamps))]

        result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess([], 0, "", "")
        overlay_timestamp = True
        for attempt in range(2):
            overlay_timestamp = attempt == 0
            failed = False
            for thumb_file, timestamp in zip(thumb_files, timestamps):
                thumb_args = build_ffmpeg_thumbnail_args(video, thumb_file, thumbnail_width, timestamp, overlay_timestamp)
                commands.append([options.ffmpeg, *thumb_args])
                result = run_native_result(options.ffmpeg, thumb_args, timeout=options.timeout)
                if result.returncode != 0:
                    failed = True
                    break
            if not failed:
                break
        else:
            return result, commands

        magick_args = build_magick_video_sheet_args(
            thumb_files,
            timestamps,
            tile,
            out_file,
            labels_needed=not overlay_timestamp,
        )
        commands.append([options.magick, *magick_args])
        result = run_native_result(options.magick, magick_args, timeout=options.timeout)
        return result, commands


def insert_generated_artifact(
    conn: sqlite3.Connection,
    scan_id: str,
    source_file_id: Optional[int],
    artifact_type: str,
    artifact_path: Path,
    command,
    result: subprocess.CompletedProcess[str],
) -> None:
    conn.execute(
        """
        INSERT INTO generated_artifacts
        (scan_id, source_file_id, artifact_type, artifact_path, command, exit_code, stdout, stderr, created_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_id,
            source_file_id,
            artifact_type,
            str(artifact_path),
            json.dumps(command, ensure_ascii=False),
            result.returncode,
            result.stdout,
            result.stderr,
            utc_now_iso(),
        ),
    )


def generate_video_thumbnail_sheet(
    video: Path,
    out_file: Path,
    options: Options,
    duration_seconds: Optional[float] = None,
) -> tuple[subprocess.CompletedProcess[str], object]:
    if not command_available(options.ffmpeg):
        return (
            subprocess.CompletedProcess([], 1, "", f"ffmpeg not found: {options.ffmpeg}"),
            [],
        )

    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        columns, rows = parse_tile_dimensions(options.video_tile)
    except ValueError as exc:
        return subprocess.CompletedProcess([], 2, "", str(exc)), []

    thumbnail_count = columns * rows
    if duration_seconds is None:
        duration_seconds = float(thumbnail_count) if options.dry_run else probe_video_duration_seconds(video, options)
    if duration_seconds is None:
        return (
            subprocess.CompletedProcess(
                [],
                1,
                "",
                "Unable to determine video duration; ffprobe metadata is required for duration-based thumbnails.",
            ),
            [],
        )

    timestamps, step_seconds = video_montage_timestamps(duration_seconds, thumbnail_count)
    args = build_ffmpeg_video_sheet_args(
        video,
        out_file,
        options.video_scale_width,
        options.video_tile,
        timestamps,
    )
    if options.dry_run:
        return subprocess.CompletedProcess([options.ffmpeg, *args], 0, "DRY RUN", ""), [options.ffmpeg, *args]

    result = run_native_result(options.ffmpeg, args, timeout=options.timeout)
    command: object = [options.ffmpeg, *args]
    if result.returncode != 0 and command_available(options.magick):
        fallback_result, fallback_commands = generate_video_sheet_with_magick_fallback(
            video,
            out_file,
            options.video_scale_width,
            options.video_tile,
            timestamps,
            options,
        )
        command = {
            "direct_ffmpeg": [options.ffmpeg, *args],
            "fallback": fallback_commands,
            "step_seconds": step_seconds,
        }
        result = subprocess.CompletedProcess(
            fallback_result.args,
            fallback_result.returncode,
            "\n".join(part for part in (result.stdout, fallback_result.stdout) if part),
            "\n".join(part for part in (result.stderr, fallback_result.stderr) if part),
        )

    return result, command


def generate_video_montage(conn: sqlite3.Connection, scan_id: str, source_file_id: int, video: Path, root: Path, options: Options) -> None:
    if options.no_video_montage or options.scan_only:
        return
    if not command_available(options.ffmpeg):
        return
    out_dir = artifact_dir_for(video.parent, root, options.output_dir)
    out_file = out_dir / f"{video.name}.jpg"
    duration_seconds = video_duration_seconds(conn, source_file_id, video, options)
    result, command = generate_video_thumbnail_sheet(video, out_file, options, duration_seconds)
    insert_generated_artifact(
        conn,
        scan_id,
        source_file_id,
        "video_montage",
        out_file,
        command,
        result,
    )


def chunks(items: Sequence[Path], size: int) -> Iterable[Sequence[Path]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def generate_image_montage_page(
    conn: sqlite3.Connection,
    scan_id: str,
    root: Path,
    source_dir: Path,
    page_files: Sequence[Path],
    page_num: int,
    options: Options,
) -> None:
    if options.no_image_montage or options.scan_only:
        return
    if not page_files or not command_available(options.magick):
        return
    out_dir = artifact_dir_for(source_dir, root, options.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
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


@dataclass
class ImageMontageState:
    buffers: dict[Path, list[Path]] = field(default_factory=dict)
    next_page_numbers: dict[Path, int] = field(default_factory=dict)
    source_count: int = 0

    def add(self, image: Path, root: Path, options: Options) -> Optional[tuple[Path, int, list[Path]]]:
        self.source_count += 1
        if options.no_image_montage or options.scan_only:
            return None
        source_dir = image.parent if options.per_directory_montage else root
        buffer = self.buffers.setdefault(source_dir, [])
        buffer.append(image)
        if len(buffer) >= max(1, options.image_page_size):
            return self._take_page(source_dir)
        return None

    def flush_remaining(self) -> Iterable[tuple[Path, int, list[Path]]]:
        for source_dir in sorted(self.buffers, key=path_text):
            if self.buffers[source_dir]:
                yield self._take_page(source_dir)

    def _take_page(self, source_dir: Path) -> tuple[Path, int, list[Path]]:
        page_num = self.next_page_numbers.get(source_dir, 1)
        self.next_page_numbers[source_dir] = page_num + 1
        page_files = list(self.buffers[source_dir])
        self.buffers[source_dir].clear()
        return source_dir, page_num, page_files


def generate_image_montages(conn: sqlite3.Connection, scan_id: str, root: Path, image_files: Sequence[Path], options: Options) -> None:
    if options.no_image_montage or options.scan_only:
        return
    if not image_files:
        return

    by_dir: dict[Path, list[Path]] = {}
    if options.per_directory_montage:
        for p in image_files:
            by_dir.setdefault(p.parent, []).append(p)
    else:
        by_dir[root] = list(image_files)

    for source_dir, files in by_dir.items():
        for page_num, page_files in enumerate(chunks(files, options.image_page_size), start=1):
            generate_image_montage_page(conn, scan_id, root, source_dir, page_files, page_num, options)


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
    parser.add_argument("--root", type=Path, help="Root folder to scan/process. Required unless --video-file is used.")
    parser.add_argument("--video-file", type=Path, help="Generate one video thumbnail sheet and exit without scanning or writing SQLite metadata.")
    parser.add_argument("--video-output", "--thumbnail-sheet", dest="video_output", type=Path, help="Output JPG path for --video-file. Default: <video-file>.jpg, or <output-dir>/<video-name>.jpg when --output-dir is set.")
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
    parser.add_argument("--video-fps", default="9999", help="Deprecated; video montages now sample by duration and tile count.")
    parser.add_argument("--video-thumbnail-width", "--video-scale-width", dest="video_scale_width", type=int, default=640, help="Video thumbnail width in horizontal pixels; vertical size is auto (-1). Default: 640.")
    parser.add_argument("--video-tile", default="15x15", help="Video tile layout. The columns*rows count is the number of sampled thumbnails. Default: 15x15.")
    parser.add_argument("--image-tile", default="15x15", help="ImageMagick montage tile layout. Default: 15x15.")
    parser.add_argument("--image-geometry", default="300x300>", help="Image resize geometry before montage. Default: 300x300>.")
    parser.add_argument("--image-page-size", type=int, default=225, help="Images per montage page. Default: 225.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable name/path.")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable name/path.")
    parser.add_argument("--magick", default="magick", help="ImageMagick executable name/path.")
    parser.add_argument("--timeout", type=int, default=3600, help="Per-command timeout in seconds. Default: 3600.")
    parser.add_argument("--include-exts", default=None, help="Comma-separated extension allowlist, e.g. .mp4,.jpg. Default: scan all non-excluded files.")
    parser.add_argument("--exclude-exts", default=",".join(sorted(DEFAULT_EXCLUDE_EXTENSIONS)), help="Comma-separated extension blocklist.")
    parser.add_argument("--exclude-names", default=",".join(sorted(DEFAULT_EXCLUDE_NAMES)), help="Comma-separated filename blocklist.")
    parser.add_argument("--dry-run", action="store_true", help="Do not run ffmpeg/magick montage commands; record intended commands.")
    return parser


def options_from_args(args: argparse.Namespace, root: Path, db: Path, output_dir: Path) -> Options:
    return Options(
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


def output_path_for_single_video(args: argparse.Namespace, video: Path) -> Path:
    if args.video_output is not None:
        return args.video_output.expanduser().resolve()
    if args.output_dir is not None:
        return (args.output_dir.expanduser().resolve() / f"{video.name}.jpg").resolve()
    return video.with_name(f"{video.name}.jpg").resolve()


def generate_single_video_sheet(args: argparse.Namespace) -> int:
    video = args.video_file.expanduser().resolve()
    if not video.is_file():
        print(f"Video file is not a file: {video}", file=sys.stderr)
        return 2

    root = args.root.expanduser().resolve() if args.root else video.parent
    db = (args.db or (root / "archive_inventory.sqlite")).expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else video.parent
    out_file = output_path_for_single_video(args, video)
    options = options_from_args(args, root, db, output_dir)

    result, command = generate_video_thumbnail_sheet(video, out_file, options)
    if args.dry_run:
        print(json.dumps(command, ensure_ascii=False, indent=2))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return result.returncode or 1

    print(f"Thumbnail sheet: {out_file}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.video_file is not None:
        return generate_single_video_sheet(args)

    if args.root is None:
        print("Root is required unless --video-file is used.", file=sys.stderr)
        return 2

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Root is not a directory: {root}", file=sys.stderr)
        return 2

    db = (args.db or (root / "archive_inventory.sqlite")).expanduser().resolve()
    output_dir = (args.output_dir or (root / "Montages")).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    options = options_from_args(args, root, db, output_dir)

    scan_id = str(uuid.uuid4())
    started = utc_now_iso()
    image_montages = ImageMontageState()
    count = 0

    with closing(sqlite3.connect(db)) as conn:
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
                    if not options.no_video_montage and not options.scan_only:
                        conn.commit()
                        print(f"Video montage: {path}", flush=True)
                        generate_video_montage(conn, scan_id, file_id, path, root, options)
                        conn.commit()
                elif ext in IMAGE_EXTENSIONS and not path.name.lower().startswith("montage-"):
                    page = image_montages.add(path, root, options)
                    if page is not None:
                        source_dir, page_num, page_files = page
                        conn.commit()
                        print(f"Image montage page {page_num}: {source_dir}", flush=True)
                        generate_image_montage_page(conn, scan_id, root, source_dir, page_files, page_num, options)
                        conn.commit()
            if count % 100 == 0:
                conn.commit()
                print(f"Recorded {count} paths...", flush=True)

        conn.commit()

        print(f"Image montage source files: {image_montages.source_count}", flush=True)
        for source_dir, page_num, page_files in image_montages.flush_remaining():
            conn.commit()
            print(f"Image montage page {page_num}: {source_dir}", flush=True)
            generate_image_montage_page(conn, scan_id, root, source_dir, page_files, page_num, options)
            conn.commit()

        conn.execute("UPDATE scans SET scan_finished_utc=? WHERE id=?", (utc_now_iso(), scan_id))
        conn.commit()

    print(f"Done. Recorded {count} paths.")
    print(f"SQLite DB: {db}")
    print(f"Montages: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
