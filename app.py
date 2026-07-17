"""Local web server for the EVIE demand dashboard.

The server is intentionally small and dependency-free.  It serves the files in
``static/`` and exposes JSON endpoints that call the demand engine.  All model
logic lives in ``demand_engine.py``; this file only handles HTTP, browser
launching, and background optimization jobs so the browser can show progress.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from demand_engine import data_options, latest_pricing, run_optimization, run_validation, run_what_if


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
HOST = "127.0.0.1"
PORT = 8766

# In-memory job state for long optimization runs.  Jobs are not persisted
# because the dashboard is intended to run as a local analyst tool.
OPTIMIZATION_JOBS: dict[str, dict] = {}
OPTIMIZATION_JOBS_LOCK = threading.Lock()


def start_optimization_job(payload: dict) -> str:
    """Start an optimization run in the background and return its job id."""
    job_id = uuid.uuid4().hex
    with OPTIMIZATION_JOBS_LOCK:
        OPTIMIZATION_JOBS[job_id] = {
            "status": "running",
            "progress": {"stage": "queued", "phase": "Queued"},
            "result": None,
            "error": None,
        }

    def update_progress(progress: dict) -> None:
        with OPTIMIZATION_JOBS_LOCK:
            job = OPTIMIZATION_JOBS.get(job_id)
            if job is not None:
                job["progress"] = progress

    def worker() -> None:
        try:
            result = run_optimization(
                payload, progress_callback=update_progress)
            with OPTIMIZATION_JOBS_LOCK:
                job = OPTIMIZATION_JOBS.get(job_id)
                if job is not None:
                    job["status"] = "complete"
                    job["result"] = result
                    job["progress"] = {
                        **(job.get("progress") or {}),
                        "stage": "complete",
                        "phase": "Complete",
                    }
        except Exception as exc:
            traceback.print_exc()
            with OPTIMIZATION_JOBS_LOCK:
                job = OPTIMIZATION_JOBS.get(job_id)
                if job is not None:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["progress"] = {
                        **(job.get("progress") or {}),
                        "stage": "error",
                        "phase": "Error",
                    }

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def optimization_job_status(job_id: str) -> dict:
    """Return a shallow copy of the current state for an optimization job."""
    with OPTIMIZATION_JOBS_LOCK:
        job = OPTIMIZATION_JOBS.get(job_id)
        if job is None:
            raise ValueError(f"Unknown optimization job {job_id}")
        return dict(job)


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for static assets and the dashboard API."""

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        """Send a JSON response with no-store caching headers."""
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc: Exception) -> None:
        traceback.print_exc()
        self.send_json({"ok": False, "error": str(exc)}, status=500)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        """Route read-only API calls and static-file requests."""
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/options":
                self.send_json({"ok": True, "data": data_options()})
                return
            if parsed.path == "/api/base-pricing":
                qs = parse_qs(parsed.query)
                segment = qs.get("segment", ["Regular"])[0]
                train_end = qs.get("train_end", [None])[0] or None
                drop_latest = qs.get("drop_latest", ["true"])[
                    0].lower() != "false"
                self.send_json({"ok": True, "data": latest_pricing(
                    segment, train_end=train_end, drop_latest=drop_latest)})
                return
            if parsed.path == "/api/optimize/status":
                qs = parse_qs(parsed.query)
                job_id = qs.get("job_id", [""])[0]
                self.send_json(
                    {"ok": True, "data": optimization_job_status(job_id)})
                return
            self.serve_static(parsed.path)
        except Exception as exc:
            self.send_error_json(exc)

    def do_POST(self) -> None:
        """Route API calls that run estimation, forecasting, or optimization."""
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/validate":
                self.send_json({"ok": True, "data": run_validation(payload)})
                return
            if parsed.path == "/api/what-if":
                self.send_json({"ok": True, "data": run_what_if(payload)})
                return
            if parsed.path == "/api/optimize":
                self.send_json({"ok": True, "data": run_optimization(payload)})
                return
            if parsed.path == "/api/optimize/start":
                self.send_json(
                    {"ok": True, "data": {"job_id": start_optimization_job(payload)}})
                return
            self.send_json(
                {"ok": False, "error": f"Unknown endpoint {parsed.path}"}, status=404)
        except Exception as exc:
            self.send_error_json(exc)

    def serve_static(self, request_path: str) -> None:
        if request_path in ("", "/"):
            path = STATIC_DIR / "index.html"
        else:
            clean = request_path.lstrip("/")
            path = STATIC_DIR / clean
        if not path.exists() or not path.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        content_type = mimetypes.guess_type(
            path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(open_browser: bool = False) -> None:
    """Run the local dashboard server until the user stops the process."""
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"EVIE demand dashboard running at {url}")
    print("In Spyder: keep this console running while using the dashboard.")
    print("To close the dashboard app, click the red Stop button in Spyder.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main(open_browser=True)
