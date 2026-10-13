"""Local web UI for Compresso: `compresso ui`.

Stdlib-only (http.server + threading). Serves on 127.0.0.1 and opens the
user's browser. Reuses the exact same pipeline as the CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import which
from urllib.parse import urlparse, parse_qs

from . import __version__
from .images import HEIF_OK
from .engine import run_job as engine_run

STATIC_DIR = Path(__file__).parent / "static"


class Job:
    def __init__(self, opts: dict):
        self.id = uuid.uuid4().hex[:12]
        self.opts = opts
        self.snap = {"state": "running", "total": 0, "done": 0, "entries": [],
                     "error": None, "total_in": 0, "total_out": 0,
                     "compressed": 0, "skipped": 0, "failed": 0, "by_ext": {}}
        self.cancel = threading.Event()

    def snapshot(self) -> dict:
        s = dict(self.snap)
        s["id"] = self.id
        s["entries"] = s.get("entries", [])[-400:]
        return s


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def run_job(job: Job) -> None:
    def progress(summary):
        job.snap = summary
    try:
        summary = engine_run(job.opts, progress=progress, cancel=job.cancel)
        job.snap = summary
    except Exception as e:
        job.snap["state"] = "error"
        job.snap["error"] = str(e)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/env":
            env = {"version": __version__, "heif": HEIF_OK}
            try:
                from .videos import ffmpeg_bin, available_encoders
                import subprocess
                v = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True)
                env["ffmpeg"] = v.stdout.splitlines()[0].split("Copyright")[0].strip()
                encs = available_encoders()
                env["hw"] = sorted(e for e in encs if any(t in e for t in ("nvenc", "qsv", "amf", "vaapi")))
                env["sw"] = sorted(e for e in encs if e in ("libx264", "libsvtav1"))
            except Exception as e:
                env["ffmpeg_error"] = str(e)
            self._send(200, env)
        elif u.path == "/api/progress":
            jid = parse_qs(u.query).get("id", [""])[0]
            job = JOBS.get(jid)
            self._send(200, job.snapshot() if job else {"state": "not_found"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/run":
            n = int(self.headers.get("Content-Length", 0))
            try:
                opts = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            if not opts.get("paths"):
                return self._send(400, {"error": "no paths given"})
            job = Job(opts)
            with JOBS_LOCK:
                JOBS[job.id] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self._send(200, {"id": job.id})
        elif u.path == "/api/cancel":
            jid = parse_qs(u.query).get("id", [""])[0]
            job = JOBS.get(jid)
            if job:
                job.cancel.set()
            self._send(200, {"cancelled": bool(job)})
        elif u.path == "/api/quit":
            self._send(200, {"bye": True})
            _shutdown_server()
        else:
            self._send(404, {"error": "not found"})


def _app_mode_browser() -> list[str] | None:
    """A Chromium/Edge binary that can run a URL as a standalone app window."""
    names = ["msedge", "microsoft-edge", "google-chrome", "chrome",
             "chromium", "chromium-browser"]
    if sys.platform == "win32":
        # Typical Edge/Chrome install locations on Windows.
        pf = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        for sub in (r"Microsoft\Edge\Application\msedge.exe",
                    r"Google\Chrome\Application\chrome.exe"):
            p = pf / sub
            if p.exists():
                return [str(p)]
        pf2 = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"
        if pf2.exists():
            return [str(pf2)]
    for n in names:
        w = which(n)
        if w:
            return [w]
    return None


def open_window(url: str) -> str:
    """Open the UI in a separate OS window. Returns the method used.

    Preference order:
      1. pywebview (if the optional dependency is installed) — a true native
         webview window that shuts the server down when closed.
      2. Edge/Chrome/Chromium in --app mode — a standalone window with no
         browser chrome, common on Windows and most Linux distros.
      3. The default browser (plain tab) as a last resort.
    """
    try:
        import webview  # optional: pip install pywebview

        def on_closed():
            threading.Timer(0.1, _shutdown_server).start()

        webview.create_window("Compresso", url, width=1000, height=800,
                              min_size=(720, 560))
        webview.start(on_closed)
        return "pywebview"
    except ImportError:
        pass
    except Exception as e:  # webview present but no GUI backend (headless box)
        print(f"  pywebview failed ({e}); trying app-window instead.")

    app = _app_mode_browser()
    if app:
        # A dedicated profile dir guarantees a separate process+window (an
        # already-running Chrome would otherwise just open a tab and exit),
        # which is what lets us shut the server down when the window closes.
        profile = Path(tempfile.gettempdir()) / f"compresso-profile-{uuid.uuid4().hex[:8]}"
        proc = subprocess.Popen(
            app + ["--app=" + url, "--window-size=1000,800",
                   "--no-first-run", "--no-default-browser-check",
                   f"--user-data-dir={profile}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=_wait_and_shutdown, args=(proc, profile), daemon=True).start()
        return "app-window"

    webbrowser.open(url)
    return "browser"


_SERVER = None
_STOP = threading.Event()


def _shutdown_server():
    if _SERVER is not None:
        threading.Timer(0.1, _SERVER.shutdown).start()
    _STOP.set()


def _wait_and_shutdown(proc, profile: Path | None = None):
    proc.wait()          # user closed the window
    if profile is not None:
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    _shutdown_server()


def launch(port: int = 0, mode: str = "window") -> None:
    global _SERVER
    _STOP.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler))
    _SERVER = srv
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Compresso {__version__} UI at {url}")

    # Serve in a background thread so the window owns the lifecycle.
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if mode == "browser":
        webbrowser.open(url)
        print("Press Ctrl+C to stop the server.")
        try:
            _STOP.wait()
        except KeyboardInterrupt:
            pass
    else:
        used = open_window(url)
        if used == "browser":
            print("  no standalone-window option found; opened a browser tab instead.")
            print("  Tip: pip install pywebview  (or install Edge/Chrome) for a true app window.")
            print("Press Ctrl+C to stop the server.")
            try:
                _STOP.wait()
            except KeyboardInterrupt:
                pass
        else:
            print("Close the window to quit.")
            try:
                _STOP.wait()
            except KeyboardInterrupt:
                pass
    srv.shutdown()
    _SERVER = None
    print("bye")
