# archive_montage.py

Python-only, cross-platform archival scanner and montage generator for Windows/Linux/macOS.

It uses Python for Unicode-safe directory walking, file metadata collection, hashing, and SQLite writes. It calls native tools only for media work:

- `ffmpeg` for video thumbnail contact sheets
- `ffprobe` for video/audio technical metadata
- `magick` from ImageMagick for image contact sheets and image metadata
- `7z` from 7-Zip for optional per-video tar archives

## Install prerequisites

Python 3.9+ is required.

Windows:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install ImageMagick.ImageMagick
winget install 7zip.7zip
```

Linux example:

```bash
sudo apt update
sudo apt install python3 ffmpeg imagemagick p7zip-full
```

No Python packages are required beyond the standard library.

## Basic usage

Scan a folder, write SQLite metadata, and generate montages:

```bash
python archive_montage.py --root "/path/to/archive"
```

Windows example:

```powershell
python .\archive_montage.py --root "D:\Archive"
```

Default outputs:

```text
<root>/archive_inventory.sqlite
<root>/Montages/
```

## Useful modes

Scan only, no montage generation:

```bash
python archive_montage.py --root "/path/to/archive" --scan-only
```

Generate only video montages, no image contact sheets:

```bash
python archive_montage.py --root "/path/to/archive" --no-image-montage
```

Generate only image montages, no video contact sheets:

```bash
python archive_montage.py --root "/path/to/archive" --no-video-montage
```

Skip hashing for a faster inventory:

```bash
python archive_montage.py --root "/path/to/archive" --no-hash
```

Dry run montage commands without running `ffmpeg` or `magick`:

```bash
python archive_montage.py --root "/path/to/archive" --dry-run
```

Use custom output locations:

```bash
python archive_montage.py \
  --root "/path/to/archive" \
  --db "/path/to/archive_inventory.sqlite" \
  --output-dir "/path/to/montages"
```

## Video montage settings

Default video sampling:

```text
15x15 tile = 225 thumbnails
thumbnail width 640 pixels, height auto (-1)
sample step = video duration / 225, rounded to microseconds
```

Example custom settings:

```bash
python archive_montage.py \
  --root "/path/to/archive" \
  --video-thumbnail-width 400 \
  --video-tile 10x10
```

Generate one video thumbnail sheet without scanning or writing SQLite metadata:

```bash
python archive_montage.py \
  --video-file "/path/to/video.mp4" \
  --video-output "/path/to/video-sheet.jpg" \
  --video-thumbnail-width 400 \
  --video-tile 10x10
```

## Video archives with 7z

When `--archive-format` is set, videos are archived individually with `7z`.

Supported formats:

```text
tar
tar.gz
tar.xz
tar.bz2
```

During a scan, each video archive is written under the generated output folder:

```text
<output-dir>/<relative-source-dir>/archives/<video-name>.<format>
```

Example scan that creates video montages and per-video `tar.gz` archives:

```bash
python archive_montage.py \
  --root "/path/to/archive" \
  --archive-format tar.gz
```

Archive videos without generating video thumbnail sheets:

```bash
python archive_montage.py \
  --root "/path/to/archive" \
  --no-video-montage \
  --archive-format tar.xz
```

Generate one video thumbnail sheet and archive that same video:

```bash
python archive_montage.py \
  --video-file "/path/to/video.mp4" \
  --archive-format tar.bz2
```

Archive one video only, then delete the source after the archive succeeds:

```bash
python archive_montage.py \
  --video-file "/path/to/video.mp4" \
  --no-video-montage \
  --archive-format tar.gz \
  --delete-source-after-archive
```

Use `--archive-output` with `--video-file` to choose a specific archive path.
Use `--seven-zip` if the 7-Zip executable is named something other than `7z`.

## Image montage settings

Default image montage:

```text
225 images per page
15x15 tile
300x300> geometry
```

Example custom settings:

```bash
python archive_montage.py \
  --root "/path/to/archive" \
  --image-page-size 100 \
  --image-tile 10x10 \
  --image-geometry "250x250>"
```

## SQLite tables

### scans

One row per run. Stores scanner version, root path, host, platform, Python version, and tool availability.

### files

One row per path. Stores:

- full path, relative path, parent path, name, stem, extension
- size
- symlink status and link target
- device ID and inode/file ID where available
- mode/permissions
- readonly flag
- owner/group/uid/gid where available
- created/birth time when available
- modified time
- accessed time
- metadata-changed time on POSIX systems
- raw ctime
- MIME guess
- SHA256 and optional BLAKE2b
- raw `stat` JSON
- any scan error

### media_metadata

One row per media file where metadata was available. Stores summary fields plus raw JSON/text:

- duration
- bitrate
- format name
- video codec
- audio codec
- width/height
- frame rate
- stream count
- raw `ffprobe` JSON
- raw ImageMagick identify JSON/text

### generated_artifacts

One row per generated montage command. Stores:

- artifact type
- artifact path or pattern
- command array as JSON
- exit code
- stdout/stderr
- creation time

## Unicode notes

The script uses Python `pathlib`, `os`, `sqlite3`, and `subprocess` argument arrays. This avoids shell string parsing and is the safest approach for paths containing spaces, quotes, CJK characters, accents, and emoji.

For best Windows display behavior, run it in Windows Terminal with a Unicode-capable font. The database will still store Unicode correctly even if the console cannot render every glyph.
