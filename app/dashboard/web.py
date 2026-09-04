"""Local web dashboard: stdlib http.server, JSON API, no dependencies.

GET  /            -> dashboard UI (static/index.html)
GET  /api/state   -> full dashboard payload (incl. scan_status)
GET  /api/scan-status -> lightweight scanner status (fast polling)
POST /api/scan    -> collect + rank + prepare (never posts); goes
                      through the SAME scheduler lock as the background
                      scanner, so manual and automatic scans never overlap
POST /api/copied  -> {article_id} record copy (never means posted)
POST /api/posted  -> {article_id, tweet} mark manually posted (✓)
                      tweet = the EXACT text the user marked as posted
                      (edited or unedited) — stored verbatim
POST /api/skip    -> {article_id, reason}
POST /api/unskip  -> {article_id} reconsider a skipped story
GET  /api/history?q=&status=&source=&days= -> searchable, filterable
                      story history (posted / skipped / copied)

The X button is handled entirely in the browser (opens
https://x.com/compose/post); the COPY button uses the browser's own
clipboard API. Neither contacts this server in a way that could post.
No route here ever calls the X API.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app.config import CONFIG
from app.dashboard import service
from app.dashboard.scanner import ScanScheduler
from app.logger import get_logger

log = get_logger("dashboard.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
COMPOSE_URL = "https://x.com/compose/post"


class DashboardHandler(BaseHTTPRequestHandler):
    db = None          # injected by serve()
    scheduler = None   # injected by serve() (background scanner)

    def log_message(self, fmt, *args):   # quiet default access log
        log.debug(fmt, *args)

    # --- helpers ---
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def _article_id(self, data):
        try:
            aid = int(data.get("article_id"))
            return aid if aid > 0 else None
        except (TypeError, ValueError):
            return None

    # --- routes ---
    def _history_params(self, query):
        """Parse optional history filters: q (keyword), status
        (posted/skipped/copied), source (exact name), days (lookback)."""
        def get(key):
            return (query.get(key) or [None])[0]

        def iso_days_ago(days):
            dt = datetime.now(timezone.utc) - timedelta(days=days)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        status = get("status")
        if status not in (None, "", "posted", "skipped", "copied"):
            status = None
        since = None
        days = get("days")
        if days and str(days).isdigit() and int(days) > 0:
            since = iso_days_ago(int(days))
        q = get("q")
        return (q if q and q.strip() else None), status, get("source"), since

    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/" or path == "/index.html":
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif path == "/api/state":
            with self.db_lock():
                payload = service.state(self.db)
            payload["scan_status"] = self._scan_status()
            self._json(payload)
        elif path == "/api/scan-status":
            # cheap endpoint for the frontend's notification polling —
            # does not read the articles tables, only scheduler state
            self._json({"scan_status": self._scan_status()})
        elif path == "/api/history":
            q, status, source, since = self._history_params(query)
            with self.db_lock():
                self._json({"history": service.history(
                    self.db, q=q, status=status, source=source,
                    since=since)})
        elif path == "/api/health":
            self._json({"ok": True, "compose_url": COMPOSE_URL})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()
        if path == "/api/scan":
            # manual scan goes through the SAME scheduler the background
            # thread uses — one lock, so scans never overlap. Do NOT hold
            # db_lock around this call: the scanner takes db_lock itself
            # and threading.Lock is not reentrant.
            sched = self.scheduler
            if sched is not None:
                stats = sched.run_scan()
            else:                      # no scheduler (direct service use)
                with self.db_lock():
                    stats = service.scan(self.db)
            with self.db_lock():
                payload = service.state(self.db)
            payload["scan_stats"] = stats
            payload["scan_status"] = self._scan_status()
            self._json(payload)
        elif path == "/api/copied":
            aid = self._article_id(data)
            if aid is None:
                self._json({"ok": False, "error": "bad_article_id"}, 400)
                return
            with self.db_lock():
                service.mark_copied(self.db, aid)
                payload = service.state(self.db)
            self._json(payload)
        elif path == "/api/posted":
            aid = self._article_id(data)
            if aid is None:
                self._json({"ok": False, "error": "bad_article_id"}, 400)
                return
            with self.db_lock():
                result = service.mark_posted(
                    self.db, aid, data.get("tweet"))
                payload = service.state(self.db)
            payload["result"] = result
            self._json(payload, 200 if result["ok"] else 409)
        elif path == "/api/skip":
            aid = self._article_id(data)
            if aid is None:
                self._json({"ok": False, "error": "bad_article_id"}, 400)
                return
            with self.db_lock():
                result = service.skip(self.db, aid,
                                      data.get("reason") or "manual")
                payload = service.state(self.db)
            payload["result"] = result
            self._json(payload)
        elif path == "/api/unskip":
            aid = self._article_id(data)
            if aid is None:
                self._json({"ok": False, "error": "bad_article_id"}, 400)
                return
            with self.db_lock():
                result = service.unskip(self.db, aid)
                payload = service.state(self.db)
            payload["result"] = result
            self._json(payload)
        else:
            self.send_error(404)

    # --- static ---
    def _serve_static(self, name, ctype):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def db_lock(self):
        """SQLite conn is shared; serialize all handler access."""
        return _DB_LOCK

    def _scan_status(self):
        return self.scheduler.status() if self.scheduler else None


_DB_LOCK = threading.Lock()

# one scanner per process (import-safe singleton)
_SCHEDULER = None
_SCHEDULER_LOCK = threading.Lock()


def _get_scheduler(db):
    """Create (once) the process-wide background scanner. Subsequent
    calls return the same instance no matter which db handle is passed —
    the dashboard is the only entry point that starts it."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = ScanScheduler(
                db, CONFIG.AUTO_SCAN_INTERVAL_SECONDS, db_lock=_DB_LOCK)
        return _SCHEDULER


def serve(db, host="127.0.0.1", port=8300):
    DashboardHandler.db = db
    scheduler = _get_scheduler(db)
    DashboardHandler.scheduler = scheduler
    scheduler.start()            # idempotent: one thread per process
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    log.info("dashboard: http://%s:%d (manual posting mode, "
             "no X API calls)", host, port)
    return httpd


def run(db, host="127.0.0.1", port=8300):
    httpd = serve(db, host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if DashboardHandler.scheduler:
            DashboardHandler.scheduler.stop()
        httpd.server_close()
