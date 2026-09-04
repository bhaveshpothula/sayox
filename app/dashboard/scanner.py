"""Background news scanner: ONE daemon thread per dashboard process.

Reuses the production scan pipeline (service.scan -> collect_and_process)
— there is no second collector implementation here. Guarantees:
  * exactly one scanner thread per process (web.serve creates it once)
  * scans never overlap (scan_lock; a tick arriving mid-scan is skipped)
  * the first scan runs immediately at startup, then every interval
  * a failing scan is logged and the scheduler keeps running
  * new-news notices are scan-scoped: exactly ONE notice per scan that
    genuinely discovered articles (discovered > 0) — never per article,
    never for pure duplicates / unchanged feeds
Never posts, never touches the X API — scanning is collection only."""
import threading
from datetime import datetime, timedelta, timezone

from app.dashboard import service
from app.logger import get_logger

log = get_logger("dashboard.scanner")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")\
        .replace(tzinfo=timezone.utc)


class ScanScheduler:
    """Runs service.scan every `interval` seconds on a daemon thread.

    db_lock must be the SAME lock the HTTP handlers use (the SQLite
    connection is shared). scan_fn is injectable for tests; production
    always uses service.scan."""

    def __init__(self, db, interval, db_lock=None, scan_fn=None):
        self.db = db
        self.interval = interval
        self.db_lock = db_lock or threading.Lock()
        self._scan = scan_fn or (lambda: service.scan(db))
        self.scan_lock = threading.Lock()
        self.last_scan_at = None
        self.next_scan_at = None
        self.last_stats = None
        self.last_error = None
        self.notification = None    # latest new-news event (dict/None)
        self._notice_seq = 0
        self._stop = threading.Event()
        self._thread = None

    # --- lifecycle -------------------------------------------------------

    def start(self):
        """Start the single scanner thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="sayox-scanner", daemon=True)
        self._thread.start()
        log.info("[scanner] started — interval %ds, first scan immediate",
                 self.interval)
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self.run_scan()          # first scan runs immediately
            self.next_scan_at = _iso(datetime.now(timezone.utc)
                                     + timedelta(seconds=self.interval))
            self._sleep_until_next()

    def _sleep_until_next(self):
        """Sleep until next_scan_at. A manual scan moves next_scan_at
        forward, so the schedule follows the latest scan."""
        while not self._stop.is_set():
            delay = (_parse(self.next_scan_at) -
                     datetime.now(timezone.utc)).total_seconds()
            if delay <= 0:
                return
            if self._stop.wait(min(delay, 5.0)):
                return

    # --- scanning --------------------------------------------------------

    def run_scan(self):
        """One scan — the ONLY entry point for background AND manual
        scans (same lock, so they can never run concurrently). Returns
        the collector stats dict, or None when a scan is already
        running or the scan failed."""
        if not self.scan_lock.acquire(blocking=False):
            log.info("[scanner] scan already running — skipping")
            return None
        started_iso = _iso(datetime.now(timezone.utc))
        log.info("[scanner] scan started")
        try:
            with self.db_lock:
                stats = self._scan()
            self.last_stats = stats
            self.last_error = None
            self.last_scan_at = _iso(datetime.now(timezone.utc))
            # next scan is a full interval after THIS one (manual scans
            # included — the schedule always follows the latest scan)
            self.next_scan_at = _iso(datetime.now(timezone.utc)
                                     + timedelta(seconds=self.interval))
            log.info("[scanner] scan completed: %s", stats)
            log.info("[scanner] next scan: %s", self.next_scan_at)
            self._maybe_notify(stats, started_iso)
            return stats
        except Exception:
            # never crash the scheduler — log, keep state, retry next tick
            self.last_error = "scan failed — see newsbot.log"
            log.exception("[scanner] scan failed (scheduler keeps running)")
            return None
        finally:
            self.scan_lock.release()

    def _maybe_notify(self, stats, started_iso):
        """One notice per scan with genuinely discovered articles.
        discovered == 0 (only duplicates / stale / unchanged feeds)
        never notifies. The recommendation headline is included ONLY
        when the gated recommendation is an article discovered during
        THIS scan — never claim a recommendation that the gates did
        not actually produce."""
        discovered = int((stats or {}).get("discovered") or 0)
        if discovered <= 0:
            return
        headline = None
        try:
            with self.db_lock:
                row = self.db.get_recommendation()
                if row and row["article_id"]:
                    art = self.db.article_by_id(row["article_id"])
                    # only a recommendation that IS this scan's news
                    # (article_by_id returns sqlite3.Row — no .get())
                    if art and (art["discovered_at"] or "") >= \
                            started_iso:
                        headline = art["title"]
        except Exception:
            headline = None          # notice degrades to the generic form
        self._notice_seq += 1
        self.notification = {
            "id": self._notice_seq,
            "type": "new_news",
            "discovered": discovered,
            "clustered": int((stats or {}).get("clustered") or 0),
            "headline": headline,
            "at": self.last_scan_at,
        }
        log.info("[scanner] %d new article(s), %d clustered — notifying",
                 discovered, self.notification["clustered"])

    # --- status ----------------------------------------------------------

    def status(self):
        """Everything the dashboard needs about the scanner (JSON-safe).
        `notification` carries the latest new-news event; the frontend
        tracks its id so a notification is shown exactly once."""
        return {
            "auto_scan": True,
            "scan_interval_seconds": self.interval,
            "scan_running": self.scan_lock.locked(),
            "last_scan_at": self.last_scan_at,
            "next_scan_at": self.next_scan_at,
            "last_scan_stats": self.last_stats,
            "last_scan_error": self.last_error,
            "notification": self.notification,
        }
