"""Tests for the dashboard v2 UX/workflow behaviors: tweet editor
support (exact edited text recorded on mark-posted), skip reasons +
reconsider, story-kind badge, unified filtered history (posted /
skipped / copied), auto-refresh state idempotency, and the safety
contract (copy / X / refresh never post and never duplicate stories).
Fully offline."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.dashboard import service
from app.database import Database


def _fresh_iso(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._d.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self._d.cleanup()

    def _seed(self, n=1, title=None, summary=None, url=None,
              india=0.95, imp=0.9, source="The Hindu", coverage=False):
        """coverage=True adds a second, weaker article from another
        outlet in the same cluster, so the story is confirmed by 2
        independent sources and passes the two-tier quality gate."""
        title = title or ("Kerala landslide in Kozhikode kills %d, NDRF "
                          "deploys teams" % n)
        summary = summary or (
            "A landslide triggered by heavy rainfall struck Kozhikode in "
            "Kerala on Tuesday. %d people died. NDRF teams were deployed. "
            "Rail connectivity was restored by evening." % n)
        url = url or ("https://example.com/story-%d" % n)
        aid = self.db.insert_article({
            "url": url, "normalized_url": url,
            "url_hash": "h-%d" % n, "title": title, "summary": summary,
            "source": source, "category": "india", "country": "IN",
            "reliability": 0.95, "published_at": _fresh_iso(1)})
        self.db.update_scores(aid, india, imp, aid, "new")
        if coverage:
            sid = self.db.insert_article({
                "url": url + "-coverage",
                "normalized_url": url + "-coverage",
                "url_hash": "h-%d-coverage" % n,
                "title": title + " — latest updates", "summary": summary,
                "source": "NDTV", "category": "india", "country": "IN",
                "reliability": 0.9, "published_at": _fresh_iso(1)})
            self.db.update_scores(sid, india, 0.75, aid, "new")
        return aid


class TestEditedTweetRecordedVerbatim(_Base):
    """The dashboard's tweet editor: whatever text the user marks as
    posted is stored EXACTLY — never the generated version, never
    modified."""

    def test_mark_posted_stores_edited_text_verbatim(self):
        aid = self._seed()
        edited = ("EDITED BY USER: Kozhikode landslide briefing — 1 dead, "
                  "NDRF deployed. #Kerala #India")
        r = service.mark_posted(self.db, aid, edited)
        self.assertTrue(r["ok"])
        row = self.db.query_one(
            "SELECT tweet_text FROM tweets WHERE article_id=? "
            "AND status='posted'", (aid,))
        self.assertEqual(row["tweet_text"], edited)

    def test_history_shows_exact_edited_tweet(self):
        aid = self._seed()
        edited = "User's final wording for the landslide story. #Kerala"
        service.mark_posted(self.db, aid, edited)
        h = service.history(self.db)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["tweet"], edited)
        self.assertEqual(h[0]["status"], "posted")

    def test_editing_does_not_modify_the_article(self):
        """Editing happens client-side; the article record must be
        untouched by anything except an explicit user action."""
        aid = self._seed()
        before = dict(self.db.article_by_id(aid))
        service.state(self.db)     # display refresh — no user action
        after = dict(self.db.article_by_id(aid))
        self.assertEqual(before["title"], after["title"])
        self.assertEqual(before["summary"], after["summary"])
        self.assertEqual(before["status"], after["status"])


class TestSkipReasons(_Base):
    def test_curated_reason_recorded(self):
        aid = self._seed()
        self.assertIn("Not important", service.SKIP_REASONS)
        self.assertIn("Duplicate", service.SKIP_REASONS)
        self.assertIn("Not suitable for X", service.SKIP_REASONS)
        self.assertIn("Already covered", service.SKIP_REASONS)
        r = service.skip(self.db, aid, "Duplicate")
        self.assertTrue(r["ok"])
        art = self.db.article_by_id(aid)
        self.assertEqual(art["status"], "skipped")
        self.assertEqual(art["skip_reason"], "Duplicate")

    def test_skipped_story_hidden_but_never_deleted(self):
        aid = self._seed()
        service.skip(self.db, aid, "Not important")
        s = service.state(self.db)
        self.assertTrue(not s["current"] or
                        s["current"]["article_id"] != aid)
        self.assertIsNotNone(self.db.article_by_id(aid))
        # it never comes back on later refreshes
        for _ in range(3):
            s = service.state(self.db)
            self.assertTrue(not s["current"] or
                            s["current"]["article_id"] != aid)

    def test_unskip_restores_candidate_only_for_skipped(self):
        aid = self._seed(coverage=True)
        service.skip(self.db, aid, "Duplicate")
        self.assertTrue(service.unskip(self.db, aid)["ok"])
        art = self.db.article_by_id(aid)
        self.assertEqual(art["status"], "ready")
        self.assertIsNone(art["skip_reason"])
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], aid)
        # unskipping twice fails (already ready, not skipped)
        self.assertFalse(service.unskip(self.db, aid)["ok"])

    def test_unskip_never_touches_posted_story(self):
        aid = self._seed()
        service.mark_posted(self.db, aid, "posted text")
        self.assertFalse(service.unskip(self.db, aid)["ok"])
        self.assertEqual(self.db.article_by_id(aid)["status"], "posted")

    def test_skip_does_not_mark_posted(self):
        aid = self._seed()
        service.skip(self.db, aid, "Not important")
        rows = self.db.query(
            "SELECT * FROM tweets WHERE article_id=? AND status='posted'",
            (aid,))
        self.assertEqual(len(rows), 0)


class TestStoryKind(_Base):
    def _entry(self, **kw):
        aid = self._seed(**kw)
        s = service.state(self.db)
        for q in s["queue"]:
            if q["article_id"] == aid:
                return q
        return None

    def test_standard_story(self):
        e = self._entry()
        self.assertEqual(e["kind"], "Standard")

    def test_breaking_story(self):
        e = self._entry(
            title="BREAKING: massive earthquake strikes Delhi, several "
                  "buildings collapse",
            summary="Just in: rescue teams deployed to the region.")
        self.assertEqual(e["kind"], "Breaking")

    def test_developing_story(self):
        e = self._entry(
            title="Rescue operations continue as flood waters rise",
            summary="This is a developing story with live updates.")
        self.assertEqual(e["kind"], "Developing")

    def test_trending_story(self):
        """3+ outlets covering the same event (cluster size 3 -> trending
        signal 60) is shown as Trending."""
        aids = [self._seed(n) for n in (1, 2, 3)]
        for aid in aids:
            self.db.execute("UPDATE articles SET story_cluster_id=? "
                            "WHERE id=?", (aids[0], aid))
        s = service.state(self.db)
        entry = [q for q in s["queue"] if q["article_id"] == aids[0]][0]
        self.assertEqual(entry["kind"], "Trending")


class TestUnifiedHistory(_Base):
    def test_all_three_statuses_in_default_history(self):
        a1 = self._seed(1)
        a2 = self._seed(2)
        a3 = self._seed(3)
        service.mark_posted(self.db, a1, "posted tweet one")
        service.skip(self.db, a2, "Not important")
        service.mark_copied(self.db, a3)
        h = service.history(self.db)
        statuses = {r["status"] for r in h}
        self.assertEqual(statuses, {"posted", "skipped", "copied"})

    def test_filter_by_status(self):
        a1 = self._seed(1)
        a2 = self._seed(2)
        service.mark_posted(self.db, a1, "posted tweet one")
        service.skip(self.db, a2, "Duplicate")
        for status, expected in (("posted", 1), ("skipped", 1),
                                 ("copied", 0)):
            h = service.history(self.db, status=status)
            self.assertEqual(len(h), expected, status)
            if expected:
                self.assertEqual({r["status"] for r in h}, {status})

    def test_filter_by_source(self):
        a1 = self._seed(1, source="The Hindu")
        a2 = self._seed(2, source="NDTV")
        service.mark_posted(self.db, a1, "hindu tweet")
        service.mark_posted(self.db, a2, "ndtv tweet")
        h = service.history(self.db, source="NDTV")
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["source"], "NDTV")
        self.assertEqual(h[0]["tweet"], "ndtv tweet")

    def test_filter_by_date(self):
        a1 = self._seed(1)
        service.mark_posted(self.db, a1, "old tweet")
        # backdate the tweet row beyond the filter window
        old = (datetime.now(timezone.utc) -
               timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.execute("UPDATE tweets SET created_at=? "
                        "WHERE article_id=?", (old, a1))
        self.assertEqual(len(service.history(self.db, since=old)), 1)
        recent = (datetime.now(timezone.utc) -
                  timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(len(service.history(self.db, since=recent)), 0)

    def test_keyword_search_covers_all_statuses(self):
        a1 = self._seed(1, title="Cyclone Remal hits the Bengal coast")
        a2 = self._seed(2, title="RBI holds repo rate steady")
        service.mark_posted(self.db, a1, "cyclone tweet")
        service.skip(self.db, a2, "Not important")
        self.assertEqual(len(service.history(self.db, q="cyclone")), 1)
        self.assertEqual(len(service.history(self.db, q="repo")), 1)

    def test_skipped_history_row_has_reason(self):
        aid = self._seed()
        service.skip(self.db, aid, "Not suitable for X")
        h = service.history(self.db, status="skipped")
        self.assertEqual(h[0]["skip_reason"], "Not suitable for X")
        self.assertEqual(h[0]["article_id"], aid)

    def test_copied_history_row_present_until_posted(self):
        aid = self._seed()
        service.state(self.db)
        service.mark_copied(self.db, aid)
        h = service.history(self.db, status="copied")
        self.assertEqual(len(h), 1)
        # posting later removes it from the copied view
        service.mark_posted(self.db, aid, "final text")
        self.assertEqual(len(service.history(self.db, status="copied")), 0)
        self.assertEqual(len(service.history(self.db, status="posted")), 1)


class TestStatePayload(_Base):
    def test_state_exposes_new_ui_fields(self):
        self._seed(coverage=True)
        s = service.state(self.db)
        self.assertIn("skip_reasons", s)
        self.assertIn("Not important", s["skip_reasons"])
        self.assertIn("auto_refresh_seconds", s)
        self.assertGreaterEqual(s["auto_refresh_seconds"], 300)
        self.assertIn("copied", s)
        cur = s["current"]
        self.assertIn(cur["kind"],
                      ("Breaking", "Developing", "Trending", "Standard"))
        self.assertIn(cur["source_quality"], s["current"].values())

    def test_refresh_is_idempotent_no_duplicates(self):
        """Auto-refresh re-reads state repeatedly: no article rows are
        ever duplicated and the queue stays stable."""
        self._seed(1)
        self._seed(2)
        first = service.state(self.db)
        for _ in range(5):
            s = service.state(self.db)
            self.assertEqual(s["candidates"], first["candidates"])
            self.assertEqual(s["stories_found"], first["stories_found"])
        total = self.db.query_one(
            "SELECT COUNT(*) c FROM articles")["c"]
        self.assertEqual(total, 2)


class TestSafetyContract(_Base):
    def test_copy_and_refresh_never_post(self):
        aid = self._seed()
        service.state(self.db)
        service.mark_copied(self.db, aid)
        service.state(self.db)      # auto-refresh after copy
        rows = self.db.query(
            "SELECT * FROM tweets WHERE status='posted'")
        self.assertEqual(len(rows), 0)
        self.assertEqual(self.db.article_by_id(aid)["status"], "copied")

    def test_no_x_api_interaction_from_dashboard_paths(self):
        """Nothing in the dashboard service layer touches the X client."""
        import inspect
        from app.dashboard import service as svc
        src = inspect.getsource(svc)
        self.assertNotIn("XClient", src)
        self.assertNotIn("post_tweet", src)
        self.assertNotIn("api.x.com", src)

    def test_mark_posted_removes_story_from_queue_permanently(self):
        aid = self._seed()
        service.mark_posted(self.db, aid, "final text")
        for _ in range(3):
            s = service.state(self.db)
            for q in s["queue"]:
                self.assertNotEqual(q["article_id"], aid)
            self.assertTrue(not s["current"] or
                            s["current"]["article_id"] != aid)
        art = self.db.article_by_id(aid)
        self.assertIsNotNone(art)                    # never deleted
        self.assertEqual(art["status"], "posted")


class TestHistoryParamsParsing(unittest.TestCase):
    """The web layer's filter parsing (offline, handler-level)."""
    def _parse(self, qs):
        from urllib.parse import urlparse, parse_qs
        from app.dashboard.web import DashboardHandler
        return DashboardHandler._history_params(
            None, parse_qs(urlparse(qs).query))

    def test_all_filters(self):
        q, status, source, since = self._parse(
            "/api/history?q=landslide&status=skipped&source=NDTV&days=7")
        self.assertEqual(q, "landslide")
        self.assertEqual(status, "skipped")
        self.assertEqual(source, "NDTV")
        self.assertIsNotNone(since)
        self.assertLess(since, _fresh_iso(0))

    def test_bad_status_ignored(self):
        _, status, _, _ = self._parse("/api/history?status=hacked")
        self.assertIsNone(status)

    def test_bad_days_ignored(self):
        _, _, _, since = self._parse("/api/history?days=abc")
        self.assertIsNone(since)
        _, _, _, since = self._parse("/api/history?days=-3")
        self.assertIsNone(since)

    def test_empty_query(self):
        q, status, source, since = self._parse("/api/history")
        self.assertIsNone(q)
        self.assertIsNone(status)
        self.assertIsNone(source)
        self.assertIsNone(since)

    def test_blank_q_treated_as_none(self):
        q, _, _, _ = self._parse("/api/history?q=%20%20")
        self.assertIsNone(q)


if __name__ == "__main__":
    unittest.main()
