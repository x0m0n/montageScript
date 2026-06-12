#!/usr/bin/env python3
"""
archive_montage_web.py

Dependency-free local web interface for archive_montage.py.

Features:
  - Accepts archive_montage.py parameters from a browser form.
  - Starts archive_montage.py as a subprocess.
  - Streams live stdout/stderr/status updates using Server-Sent Events.
  - Provides a stop button for the running job.
  - Designed for localhost use on Windows, Linux, and macOS.

Usage:
  python archive_montage_web.py --script ./archive_montage.py --host 127.0.0.1 --port 8765

Then open:
  http://127.0.0.1:8765/
"""

from __future__ import annotations

import argparse
import html
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

APP_VERSION = "2026.06.08-2"
MAX_LOG_LINES = 5000


@dataclass
class JobState:
    job_id: Optional[str] = None
    command: list[str] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    return_code: Optional[int] = None
    running: bool = False
    process: Optional[subprocess.Popen[str]] = None
    log_lines: list[dict] = field(default_factory=list)
    event_counter: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def add_event(self, stream: str, text: str) -> dict:
        with self.lock:
            self.event_counter += 1
            event = {
                "id": self.event_counter,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stream": stream,
                "text": text.rstrip("\r\n"),
            }
            self.log_lines.append(event)
            if len(self.log_lines) > MAX_LOG_LINES:
                del self.log_lines[: len(self.log_lines) - MAX_LOG_LINES]
            return event

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "job_id": self.job_id,
                "command": self.command,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "return_code": self.return_code,
                "running": self.running,
                "log_lines": list(self.log_lines),
                "event_counter": self.event_counter,
            }


STATE = JobState()
SERVER_CONFIG: dict = {}


def parse_bool_form(form: dict[str, list[str]], name: str) -> bool:
    return form.get(name, [""])[0].lower() in {"1", "true", "yes", "on"}


def text_value(form: dict[str, list[str]], name: str) -> str:
    return form.get(name, [""])[0].strip()


def add_optional_value(args: list[str], form: dict[str, list[str]], flag: str, name: str) -> None:
    value = text_value(form, name)
    if value:
        args.extend([flag, value])


def build_archive_command(form: dict[str, list[str]]) -> tuple[list[str], Optional[str]]:
    script = Path(SERVER_CONFIG["script"]).expanduser().resolve()
    if not script.is_file():
        return [], f"archive_montage.py not found: {script}"

    root = text_value(form, "root")
    if not root:
        return [], "Root folder is required."

    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return [], f"Root folder is not a directory: {root}"

    args = [sys.executable, "-u", str(script), "--root", str(root_path)]

    add_optional_value(args, form, "--db", "db")
    add_optional_value(args, form, "--output-dir", "output_dir")
    add_optional_value(args, form, "--video-thumbnail-width", "video_scale_width")
    add_optional_value(args, form, "--video-tile", "video_tile")
    add_optional_value(args, form, "--image-tile", "image_tile")
    add_optional_value(args, form, "--image-geometry", "image_geometry")
    add_optional_value(args, form, "--image-page-size", "image_page_size")
    add_optional_value(args, form, "--ffmpeg", "ffmpeg")
    add_optional_value(args, form, "--ffprobe", "ffprobe")
    add_optional_value(args, form, "--magick", "magick")
    add_optional_value(args, form, "--timeout", "timeout")
    add_optional_value(args, form, "--include-exts", "include_exts")
    add_optional_value(args, form, "--exclude-exts", "exclude_exts")
    add_optional_value(args, form, "--exclude-names", "exclude_names")

    for flag_name, cli_flag in [
        ("scan_only", "--scan-only"),
        ("montage_only", "--montage-only"),
        ("no_hash", "--no-hash"),
        ("blake2b", "--blake2b"),
        ("no_ffprobe", "--no-ffprobe"),
        ("no_magick_identify", "--no-magick-identify"),
        ("no_video_montage", "--no-video-montage"),
        ("no_image_montage", "--no-image-montage"),
        ("dry_run", "--dry-run"),
    ]:
        if parse_bool_form(form, flag_name):
            args.append(cli_flag)

    args.append("--recursive" if parse_bool_form(form, "recursive") else "--no-recursive")
    args.append("--per-directory-montage" if parse_bool_form(form, "per_directory_montage") else "--no-per-directory-montage")

    return args, None


def stream_reader(proc: subprocess.Popen[str], stream_name: str, pipe) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            STATE.add_event(stream_name, line)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def launch_job(command: list[str]) -> tuple[bool, str]:
    with STATE.lock:
        if STATE.running:
            return False, "A job is already running. Stop it or wait for it to finish."
        STATE.job_id = str(uuid.uuid4())
        STATE.command = command
        STATE.started_at = time.time()
        STATE.finished_at = None
        STATE.return_code = None
        STATE.running = True
        STATE.process = None
        STATE.log_lines.clear()
        STATE.event_counter = 0
        STATE.add_event("status", "Starting archive_montage.py")
        STATE.add_event("command", " ".join(command))

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except Exception as exc:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = time.time()
            STATE.return_code = -1
            STATE.add_event("error", f"Failed to start: {exc!r}")
        return False, f"Failed to start: {exc!r}"

    with STATE.lock:
        STATE.process = proc

    def monitor() -> None:
        threads = [
            threading.Thread(target=stream_reader, args=(proc, "stdout", proc.stdout), daemon=True),
            threading.Thread(target=stream_reader, args=(proc, "stderr", proc.stderr), daemon=True),
        ]
        for t in threads:
            t.start()
        rc = proc.wait()
        for t in threads:
            t.join(timeout=2)
        with STATE.lock:
            STATE.return_code = rc
            STATE.finished_at = time.time()
            STATE.running = False
            STATE.process = None
            STATE.add_event("status", f"Finished with exit code {rc}")

    threading.Thread(target=monitor, daemon=True).start()
    return True, "Job started."


def stop_job() -> tuple[bool, str]:
    with STATE.lock:
        proc = STATE.process
        if not STATE.running or proc is None:
            return False, "No job is currently running."
        STATE.add_event("status", "Stop requested.")

    try:
        if os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGTERM)
        return True, "Stop signal sent."
    except Exception as exc:
        return False, f"Failed to stop process: {exc!r}"


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archive Montage Web</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }
    body { margin: 0; padding: 1rem; }
    main { max-width: 1200px; margin: 0 auto; }
    h1 { margin-bottom: .25rem; }
    .hint { color: #666; margin-top: 0; }
    form { display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: .75rem 1rem; align-items: end; }
    label { display: block; font-weight: 650; }
    input[type="text"], input[type="number"] { width: 100%; box-sizing: border-box; padding: .55rem; font: inherit; }
    fieldset { border: 1px solid #9996; border-radius: .5rem; padding: .75rem; }
    fieldset legend { font-weight: 700; }
    .full { grid-column: 1 / -1; }
    .checks { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .35rem .7rem; }
    .actions { display: flex; gap: .5rem; align-items: center; }
    button { font: inherit; padding: .55rem .9rem; cursor: pointer; }
    button.primary { font-weight: 700; }
    #status { padding: .5rem .75rem; border-radius: .5rem; background: #8882; }
    #log { height: 420px; overflow: auto; white-space: pre-wrap; background: #111; color: #eee; padding: .75rem; border-radius: .5rem; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .9rem; }
    .stdout { color: #d8f7d8; }
    .stderr, .error { color: #ffb3b3; }
    .status { color: #9ed0ff; }
    .command { color: #ffd37a; }
    .row-note { font-size: .85rem; color: #777; margin-top: .15rem; }
  </style>
</head>
<body>
<main>
  <h1>Archive Montage Web</h1>
  <p class="hint">Local interface for archive_montage.py. Keep this bound to 127.0.0.1 unless you intentionally secure it behind another layer.</p>

  <form id="runForm">
    <div>
      <label for="root">Root folder *</label>
      <input id="root" name="root" type="text" required placeholder="/path/to/archive or C:\\Archive">
    </div>
    <div>
      <label for="db">SQLite DB path</label>
      <input id="db" name="db" type="text" placeholder="Default: <root>/archive_inventory.sqlite">
    </div>
    <div>
      <label for="output_dir">Montage output folder</label>
      <input id="output_dir" name="output_dir" type="text" placeholder="Default: <root>/Montages">
    </div>
    <div>
      <label for="include_exts">Include extensions</label>
      <input id="include_exts" name="include_exts" type="text" placeholder=".mp4,.jpg,.png">
    </div>
    <div>
      <label for="exclude_exts">Exclude extensions</label>
      <input id="exclude_exts" name="exclude_exts" type="text" placeholder="Use script default if blank">
    </div>
    <div>
      <label for="exclude_names">Exclude names</label>
      <input id="exclude_names" name="exclude_names" type="text" placeholder="Use script default if blank">
    </div>

    <fieldset class="full">
      <legend>Mode and metadata</legend>
      <div class="checks">
        <label><input type="checkbox" name="recursive" checked> Recursive</label>
        <label><input type="checkbox" name="per_directory_montage" checked> Per-directory image montage</label>
        <label><input type="checkbox" name="scan_only"> Scan only</label>
        <label><input type="checkbox" name="montage_only"> Montage only</label>
        <label><input type="checkbox" name="no_hash"> Skip SHA256</label>
        <label><input type="checkbox" name="blake2b"> Also compute BLAKE2b</label>
        <label><input type="checkbox" name="no_ffprobe"> Skip ffprobe metadata</label>
        <label><input type="checkbox" name="no_magick_identify"> Skip ImageMagick identify</label>
        <label><input type="checkbox" name="no_video_montage"> Disable video montage</label>
        <label><input type="checkbox" name="no_image_montage"> Disable image montage</label>
        <label><input type="checkbox" name="dry_run"> Dry run</label>
      </div>
    </fieldset>

    <fieldset class="full">
      <legend>Video montage</legend>
      <div class="checks">
        <label>Thumbnail width <input name="video_scale_width" type="number" value="640"></label>
        <label>Tile <input name="video_tile" type="text" value="15x15"></label>
      </div>
    </fieldset>

    <fieldset class="full">
      <legend>Image montage</legend>
      <div class="checks">
        <label>Tile <input name="image_tile" type="text" value="15x15"></label>
        <label>Geometry <input name="image_geometry" type="text" value="300x300>"></label>
        <label>Page size <input name="image_page_size" type="number" value="225"></label>
      </div>
    </fieldset>

    <fieldset class="full">
      <legend>Executables and timeout</legend>
      <div class="checks">
        <label>ffmpeg <input name="ffmpeg" type="text" value="ffmpeg"></label>
        <label>ffprobe <input name="ffprobe" type="text" value="ffprobe"></label>
        <label>magick <input name="magick" type="text" value="magick"></label>
        <label>Timeout seconds <input name="timeout" type="number" value="600"></label>
      </div>
    </fieldset>

    <div class="actions full">
      <button class="primary" type="submit">Start</button>
      <button id="stopButton" type="button">Stop</button>
      <button id="clearButton" type="button">Clear visible log</button>
      <span id="status">Idle</span>
    </div>
  </form>

  <h2>Live log</h2>
  <div id="log" aria-live="polite"></div>
</main>
<script>
const form = document.getElementById('runForm');
const log = document.getElementById('log');
const statusBox = document.getElementById('status');
const stopButton = document.getElementById('stopButton');
const clearButton = document.getElementById('clearButton');

function addLine(ev) {
  const div = document.createElement('div');
  div.className = ev.stream || 'status';
  div.textContent = `[${ev.ts || ''}] ${ev.stream || ''}: ${ev.text || ''}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function refreshStatus() {
  try {
    const res = await fetch('/status');
    const data = await res.json();
    statusBox.textContent = data.running ? 'Running' : (data.return_code === null ? 'Idle' : `Finished: ${data.return_code}`);
  } catch (e) {
    statusBox.textContent = 'Disconnected';
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const res = await fetch('/start', { method: 'POST', body: new FormData(form) });
  const data = await res.json();
  addLine({ts: new Date().toLocaleString(), stream: data.ok ? 'status' : 'error', text: data.message});
  refreshStatus();
});

stopButton.addEventListener('click', async () => {
  const res = await fetch('/stop', { method: 'POST' });
  const data = await res.json();
  addLine({ts: new Date().toLocaleString(), stream: data.ok ? 'status' : 'error', text: data.message});
  refreshStatus();
});

clearButton.addEventListener('click', () => { log.textContent = ''; });

const source = new EventSource('/events');
source.onmessage = (event) => {
  const ev = JSON.parse(event.data);
  addLine(ev);
  refreshStatus();
};
source.onerror = () => { statusBox.textContent = 'Event stream reconnecting...'; };
refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ArchiveMontageWeb/" + APP_VERSION

    def log_message(self, fmt: str, *args) -> None:
        if SERVER_CONFIG.get("quiet"):
            return
        super().log_message(fmt, *args)

    def send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_text(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path or "/"

        # Serve the UI for the root route and common browser/default-document routes.
        # Some browsers, launchers, or users may request /index.html instead of /.
        if route in {"/", "/index.html", "/index.htm"}:
            page = HTML_PAGE.replace("Archive Montage Web", f"Archive Montage Web v{html.escape(APP_VERSION)}", 1)
            self.send_text(HTTPStatus.OK, page, "text/html; charset=utf-8")
        elif route == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
        elif route == "/status":
            snap = STATE.snapshot()
            self.send_json(HTTPStatus.OK, {k: v for k, v in snap.items() if k != "log_lines"})
        elif route == "/events":
            self.handle_events()
        else:
            # For a local single-page app, falling back to the UI is friendlier than
            # showing a bare 404 when the browser opens a remembered path. API POST
            # routes still return 404 when invalid.
            page = HTML_PAGE.replace("Archive Montage Web", f"Archive Montage Web v{html.escape(APP_VERSION)}", 1)
            self.send_text(HTTPStatus.OK, page, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/start":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            # The built-in cgi module is deprecated. This parser supports the browser FormData payload used here.
            if "multipart/form-data" in content_type:
                form = self.parse_multipart_form(raw, content_type)
            else:
                form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
            command, error = build_archive_command(form)
            if error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
                return
            ok, msg = launch_job(command)
            self.send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
        elif parsed.path == "/stop":
            ok, msg = stop_job()
            self.send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "Not found")

    def parse_multipart_form(self, raw: bytes, content_type: str) -> dict[str, list[str]]:
        import email.parser
        import email.policy

        headers = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(headers + raw)
        form: dict[str, list[str]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            value = payload.decode(charset, errors="replace")
            form.setdefault(name, []).append(value)
        return form

    def handle_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_sent = 0
        try:
            while True:
                snap = STATE.snapshot()
                events = [ev for ev in snap["log_lines"] if ev["id"] > last_sent]
                for ev in events:
                    last_sent = ev["id"]
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"id: {ev['id']}\n".encode("utf-8"))
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local web interface for archive_montage.py.")
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("archive_montage.py"), help="Path to archive_montage.py.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use 127.0.0.1 for local-only access.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    parser.add_argument("--open-browser", action="store_true", help="Open the browser automatically.")
    parser.add_argument("--quiet", action="store_true", help="Reduce HTTP request logging.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    script = args.script.expanduser().resolve()
    SERVER_CONFIG.update({"script": str(script), "quiet": args.quiet})

    if not script.is_file():
        print(f"Warning: archive script not found yet: {script}", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Archive Montage Web v{APP_VERSION}")
    print(f"Using script: {script}")
    print(f"Listening on: {url}")
    print("Press Ctrl+C to stop the web server.")
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping web server.")
        stop_job()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
