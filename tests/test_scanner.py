"""Background scanner tests: one daemon thread per process, scans never
overlap (manual + scheduled share one lock), first scan immediate,
new-news notification exactly once per discovering scan, scanner survives
failures. The scanner never posts and never touches the X API."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import CONFIG
from app.dashboard import service, web
from app.dashboard.scanner import ScanScheduler
from app.database import Database


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._d.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self._d.cleanup()

    def _seed_article(self, title="Massive earthquake strikes Delhi",
                      hours_ago=0):
        return self.db.insert_article({
            "url": "https://e.com/%s" % title[:12].replace(" ", ""),
            "normalized_url": "https://e.com/x", "url_hash": "h-%s" % title[:8],
            "title": title, "summary": "A strong quake struck the region.",
            "source": "The Hindu", "category": "india", "country": "IN",
            "reliability": 0.95,
            "published_at": _iso(datetime.now(timezone.utc) -
                                 timedelta(hours=hours_ago + 1))})


class TestSchedulerLifecycle(_Base):
    def test_interval_default_is_120_seconds(self):
        self.assertEqual(CONFIG.AUTO_SCAN_INTERVAL_SECONDS, 120)

    def test_default_scan_fn_is_the_production_scan(self):
        # no scan_fn injected -> the scheduler runs service.scan on the
        # SAME pipeline the manual button uses (empty db: offline, 0 sources)
        sched = ScanScheduler(self.db, CONFIG.AUTO_SCAN_INTERVAL_SECONDS)
        stats = sched.run_scan()
        self.assertIsNotNone(stats)
        self.assertIn("sources", stats)
        self.assertIn("discovered", stats)

    def test_first_scan_runs_immediately(self):
        entered = threading.Event()

        def scan_fn():
            entered.set()
            return {"discovered": 0}

        sched = ScanScheduler(self.db, 120, scan_fn=scan_fn)
        self.addCleanup(sched.stop)
        sched.start()
        self.assertTrue(entered.wait(3), "first scan did not run")

    def test_start_is_idempotent_one_thread(self):
        def scan_fn():
            return {"discovered": 0}
        sched = ScanScheduler(self.db, 120, scan_fn=scan_fn)
        self.addCleanup(sched.stop)
        sched.start()
        first = sched._thread
        sched.start()                      # second call must not spawn
        self.assertIs(sched._thread, first)
        self.assertTrue(first.is_alive())
        # cleanup: stop() lets the daemon thread exit within one 5s tick

    def test_next_scan_scheduled_after_completion(self):
        sched = ScanScheduler(self.db, 120,
                              scan_fn=lambda: {"discovered": 0})
        sched.run_scan()
        due = datetime.strptime(sched.status()["next_scan_at"],
                                "%Y-%m-%dT%H:%M:%SZ").replace(
                                    tzinfo=timezone.utc)
        ahead = (due - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(ahead, 100)
        self.assertLess(ahead, 130)

    def test_one_scheduler_per_process_singleton(self):
        self.addCleanup(setattr, web, "_SCHEDULER", None)
        s1 = web._get_scheduler(self.db)
        s2 = web._get_scheduler(self.db)
        self.assertIs(s1, s2)
        self.assertIs(s1.db_lock, web._DB_LOCK)   # shares the handler lock


class TestScanOverlap(_Base):
    def test_concurrent_scan_skipped_not_queued(self):
        entered, release = threading.Event(), threading.Event()

        def slow_scan():
            entered.set()
            release.wait(5)
            return {"discovered": 0}

        sched = ScanScheduler(self.db, 120, scan_fn=slow_scan)
        t = threading.Thread(target=sched.run_scan)   # e.g. scheduled
        t.start()
        self.assertTrue(entered.wait(3))
        # the manual-path call while a scan runs: skipped immediately
        self.assertIsNone(sched.run_scan())
        release.set()
        t.join(3)
        self.assertFalse(t.is_alive())

    def test_sequential_scans_both_run(self):
        calls = []

        def scan_fn():
            calls.append(1)
            return {"discovered": 0}

        sched = ScanScheduler(self.db, 120, scan_fn=scan_fn)
        self.assertIsNotNone(sched.run_scan())
        self.assertIsNotNone(sched.run_scan())
        self.assertEqual(len(calls), 2)


class TestNotifications(_Base):
    def test_discovered_zero_no_notification(self):
        sched = ScanScheduler(self.db, 120,
                              scan_fn=lambda: {"discovered": 0, "clustered": 0})
        sched.run_scan()
        self.assertIsNone(sched.status()["notification"])

    def test_discovered_positive_one_notification(self):
        sched = ScanScheduler(
            self.db, 120,
            scan_fn=lambda: {"discovered": 4, "clustered": 1})
        sched.run_scan()
        n = sched.status()["notification"]
        self.assertIsNotNone(n)
        self.assertEqual(n["id"], 1)
        self.assertEqual(n["type"], "new_news")
        self.assertEqual(n["discovered"], 4)
        self.assertIsNone(n["headline"])   # no gated recommendation

    def test_multiple_articles_still_one_event_per_scan(self):
        # 5 articles in one scan -> ONE notification (never per article);
        # a second discovering scan -> a NEW id (frontend dedup key)
        sched = ScanScheduler(
            self.db, 120,
            scan_fn=lambda: {"discovered": 5, "clustered": 2})
        sched.run_scan()
        first = sched.status()["notification"]
        sched.run_scan()
        second = sched.status()["notification"]
        self.assertEqual(first["discovered"], 5)
        self.assertEqual(second["id"], first["id"] + 1)

    def test_headline_only_for_this_scan_recommendation(self):
        # a recommendation the gates produced BEFORE this scan must not
        # be reported as this scan's news
        aid = self._seed_article(hours_ago=5)
        # discovered_at is insert time; age it so it predates this scan
        self.db.execute(
            "UPDATE articles SET discovered_at=? WHERE id=?",
            (_iso(datetime.now(timezone.utc) - timedelta(hours=5)), aid))
        self.db.set_recommendation(aid, 80.0)
        sched = ScanScheduler(
            self.db, 120,
            scan_fn=lambda: {"discovered": 3, "clustered": 0})
        sched.run_scan()
        self.assertIsNone(sched.status()["notification"]["headline"])

    def test_headline_when_recommendation_discovered_this_scan(self):
        # the article must be discovered DURING the scan (insert stamps
        # discovered_at after the scan's start) — exactly what production
        # does when the gates recommend freshly-collected news
        def scan_fn():
            aid = self._seed_article()
            self.db.set_recommendation(aid, 80.0)
            return {"discovered": 3, "clustered": 0}
        sched = ScanScheduler(self.db, 120, scan_fn=scan_fn)
        sched.run_scan()
        n = sched.status()["notification"]
        self.assertEqual(n["headline"],
                         "Massive earthquake strikes Delhi")


class TestFailureSafety(_Base):
    def test_scan_failure_keeps_scheduler_alive(self):
        def boom():
            raise RuntimeError("feed exploded")
        sched = ScanScheduler(self.db, 120, scan_fn=boom)
        self.assertIsNone(sched.run_scan())
        self.assertTrue(sched.status()["last_scan_error"])
        # recovers on the next tick
        sched._scan = lambda: {"discovered": 1}
        stats = sched.run_scan()
        self.assertEqual(stats["discovered"], 1)
        self.assertIsNone(sched.status()["last_scan_error"])


class TestHttpWiring(_Base):
    """Real HTTP round-trip on an ephemeral port (empty db: offline)."""

    def _serve(self):
        self.addCleanup(setattr, web, "_SCHEDULER", None)
        httpd = web.serve(self.db, host="127.0.0.1", port=0)
        # web.serve() only binds+listens; the accept loop must run or no
        # request is ever handled (urlopen would block forever). This is
        # what web.run() does in production.
        threading.Thread(target=httpd.serve_forever,
                         daemon=True).start()

        def _shutdown():
            web.DashboardHandler.scheduler.stop()   # stop the scanner
            httpd.shutdown()                        # stop the accept loop
            httpd.server_close()                    # then release the port
        self.addCleanup(_shutdown)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def test_state_exposes_scan_status(self):
        base = self._serve()
        with urllib.request.urlopen(base + "/api/state") as r:
            payload = json.loads(r.read())
        self.assertIn("scan_status", payload)
        self.assertTrue(payload["scan_status"]["auto_scan"])
        self.assertEqual(payload["scan_status"]["scan_interval_seconds"],
                         CONFIG.AUTO_SCAN_INTERVAL_SECONDS)

    def test_scan_status_endpoint_and_manual_scan(self):
        base = self._serve()
        req = urllib.request.Request(base + "/api/scan", data=b"{}",
                                     headers={"Content-Type":
                                              "application/json"})
        with urllib.request.urlopen(req) as r:
            payload = json.loads(r.read())
        self.assertIn("scan_stats", payload)
        self.assertIn("scan_status", payload)
        with urllib.request.urlopen(base + "/api/scan-status") as r:
            light = json.loads(r.read())
        self.assertIn("scan_status", light)
        self.assertIsNotNone(light["scan_status"]["last_scan_at"])


class TestSafetyUnchanged(unittest.TestCase):
    """Scanning is collection only: gates, cooldowns and the manual
    posting contract are untouched; nothing ever auto-posts."""

    def test_gates_and_cooldowns_unchanged(self):
        self.assertEqual(CONFIG.MIN_PUBLISH_SCORE, 70)
        self.assertEqual(CONFIG.MIN_STORY_MOMENTUM, 50)
        self.assertEqual(CONFIG.MIN_STORY_IMPORTANCE, 80)
        self.assertEqual(CONFIG.NEWS_COOLDOWN_MINUTES, 60)
        self.assertEqual(CONFIG.TOPIC_COOLDOWN_MINUTES, 180)
        self.assertEqual(CONFIG.BREAKING_IMPORTANCE, 90)
        self.assertEqual(CONFIG.BREAKING_MOMENTUM, 90)
        self.assertEqual(CONFIG.BREAKING_FRESHNESS, 90)
        self.assertEqual(CONFIG.CLUSTER_TITLE_THRESHOLD, 0.45)
        self.assertEqual(CONFIG.XRP_CHALLENGE_MARGIN, 15)

    def test_auto_posting_still_disabled(self):
        self.assertFalse(CONFIG.AUTO_POST)
        self.assertFalse(CONFIG.BOT_ENABLED)

    def test_scan_never_writes_tweets(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        db = Database(os.path.join(d.name, "t.db"))
        self.addCleanup(db.close)
        # a scan that genuinely discovers an article
        def scan_fn():
            db.insert_article({
                "url": "https://e.com/q", "normalized_url": "https://e.com/q",
                "url_hash": "h-q", "title": "Big story develops in Delhi",
                "summary": "Events unfolded on Tuesday across the city.",
                "source": "The Hindu", "category": "india", "country": "IN",
                "reliability": 0.95, "published_at": "Tue, 01 Sep 2026 "
                "17:00:00 +0000"})
            return {"discovered": 1, "clustered": 0}
        sched = ScanScheduler(db, 120, scan_fn=scan_fn)
        sched.run_scan()
        posted = db.query_one("SELECT COUNT(*) c FROM tweets")
        self.assertEqual(posted["c"], 0)   # only Mark Posted ever records


if __name__ == "__main__":
    unittest.main()
