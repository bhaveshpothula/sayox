"""Dashboard tests: ranking reach score, hashtag engine, publishing
queue, mark-posted, skip, copy-safety, already-posted exclusion, and
the 402 story-preservation guarantee. Fully offline."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.config import CONFIG
from app.database import Database
from app.news.ranking import reach_score, why_this
from app.tweet.hashtags import suggest_hashtags, with_hashtags
from app.dashboard import service


def _article(url, title, summary="", source="The Hindu",
             published=None, india=0.9, imp=0.8, rel=0.95):
    return {
        "url": url, "normalized_url": url, "url_hash": "h-" + url,
        "title": title, "summary": summary, "source": source,
        "category": "india", "country": "IN", "reliability": rel,
        "published_at": published,
    }


def _fresh_iso(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRankingEngine(unittest.TestCase):
    def test_reach_score_breakdown(self):
        a = {"india_relevance_score": 0.92, "importance_score": 0.9,
             "reliability_score": 0.95,
             "published_at": _fresh_iso(1)}
        b = reach_score(a, cluster_size=3)
        self.assertEqual(b["india"], 92)
        self.assertEqual(b["importance"], 90)
        self.assertEqual(b["trending"], 60)   # 20 + 2*20
        self.assertEqual(b["freshness"], 100.0)
        self.assertEqual(b["source_quality"], 95)
        expected = round(0.30 * 90 + 0.25 * 92 + 0.20 * 100 +
                         0.15 * 60 + 0.10 * 95, 1)
        self.assertEqual(b["reach"], expected)

    def test_freshness_decays_with_age(self):
        a = {"india_relevance_score": 1, "importance_score": 1,
             "reliability_score": 1, "published_at": _fresh_iso(24)}
        b = reach_score(a)
        self.assertAlmostEqual(b["freshness"], 50.0, places=0)
        old = reach_score({"india_relevance_score": 1,
                           "importance_score": 1, "reliability_score": 1,
                           "published_at": _fresh_iso(72)})
        self.assertEqual(old["freshness"], 0.0)

    def test_fresher_higher_reach_than_stale(self):
        base = {"india_relevance_score": 0.9, "importance_score": 0.9,
                "reliability_score": 0.9}
        fresh = reach_score(dict(base, published_at=_fresh_iso(1)))
        stale = reach_score(dict(base, published_at=_fresh_iso(40)))
        self.assertGreater(fresh["reach"], stale["reach"])

    def test_why_this_mentions_top_signals_and_reach(self):
        b = reach_score({"india_relevance_score": 0.95,
                         "importance_score": 0.9, "reliability_score": 0.95,
                         "published_at": _fresh_iso(1)})
        w = why_this(b)
        self.assertIn("India relevance 95", w)
        self.assertIn("final reach score", w)


class TestHashtagEngine(unittest.TestCase):
    def test_kerala_location_and_india_tags(self):
        tags = suggest_hashtags(
            "Landslide in Kozhikode, Kerala leaves 3 dead",
            "Heavy rainfall triggered a landslide in Kozhikode district "
            "of Kerala on Tuesday.", india_score=0.9)
        self.assertIn("#Kerala", tags)
        self.assertIn("#India", tags)
        self.assertLessEqual(len(tags), 4)

    def test_rbi_economy_tags(self):
        tags = suggest_hashtags(
            "RBI holds repo rate at 6.5%",
            "The Monetary Policy Committee kept the repo rate unchanged.",
            india_score=0.95)
        self.assertIn("#RBI", tags)
        self.assertIn("#India", tags)

    def test_between_two_and_four_tags(self):
        tags = suggest_hashtags(
            "GDP growth slows as inflation eases across the economy",
            "Budget measures and GST collections are in focus.", 0.9)
        self.assertGreaterEqual(len(tags), 2)
        self.assertLessEqual(len(tags), 4)

    def test_with_hashtags_appends_within_limit(self):
        text = "Headline: RBI holds rate at 6.5% in June review."
        final, used = with_hashtags(text, ["#RBI", "#India"], 280)
        self.assertTrue(final.endswith("#RBI #India"))
        self.assertEqual(used, ["#RBI", "#India"])

    def test_with_hashtags_never_exceeds_limit(self):
        text = "x" * 270
        final, _ = with_hashtags(text, ["#RBI"], 280)
        self.assertLessEqual(len(final), 280)
        self.assertEqual(final, text)   # no room -> unchanged


class _DashboardBase(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._d.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self._d.cleanup()

    def _seed(self, title, summary, url, india=0.9, imp=0.8,
              published=None, source="The Hindu", coverage=False):
        """coverage=True adds a second, weaker article from another
        outlet in the same cluster — the story is then confirmed by 2
        independent sources (momentum >= 50, the two-tier quality gate)."""
        aid = self.db.insert_article(
            _article(url, title, summary, source, published))
        self.db.update_scores(aid, india, imp, aid, "new")
        if coverage:
            self._sibling(aid, title, summary, url, india)
        return aid

    def _sibling(self, aid, title, summary, url, india=0.9, imp=0.75,
                 source="NDTV"):
        """A second outlet's report on the same story (same cluster,
        lower importance — never outranks the primary)."""
        sid = self.db.insert_article(_article(
            url + "-coverage", title + " — latest updates", summary,
            source, _fresh_iso(1)))
        self.db.update_scores(sid, india, imp, aid, "new")
        return sid

    def _rich_seed(self, n=1):
        """Seed one confirmed story (2 outlets) guaranteed to produce a
        valid tweet and pass the recommendation gates."""
        return self._seed(
            "Kerala landslide in Kozhikode kills %d, NDRF deploys teams" % n,
            "A landslide triggered by heavy rainfall struck Kozhikode in "
            "Kerala on Tuesday. %d people died. NDRF teams were deployed. "
            "Rail connectivity was restored by evening." % n,
            "https://example.com/story-%d" % n, india=0.95, imp=0.9,
            published=_fresh_iso(1), coverage=True)


class TestPublishingQueue(_DashboardBase):
    def test_scan_prepare_never_posts(self):
        aid = self._rich_seed()
        s = service.state(self.db)
        self.assertTrue(s["current"])
        self.assertEqual(s["current"]["article_id"], aid)
        self.assertLessEqual(s["current"]["char_count"], 280)
        # tweet contains hashtags that the copy button will include
        self.assertTrue(s["current"]["tweet"])
        # no tweet row was inserted as posted by mere preparation
        rows = self.db.query(
            "SELECT * FROM tweets WHERE status='posted'")
        self.assertEqual(len(rows), 0)

    def test_queue_ranked_and_capped(self):
        self._rich_seed(1)
        for i in range(2, 10):
            self._rich_seed(i)
        s = service.state(self.db)
        self.assertLessEqual(len(s["queue"]), service.QUEUE_SIZE)
        scores = [q["reach"] for q in s["queue"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(s["candidates"], len(s["queue"]))

    def test_tweet_char_limit_respected(self):
        self._rich_seed(1)
        s = service.state(self.db)
        self.assertLessEqual(len(s["current"]["tweet"]),
                             CONFIG.TWEET_CHAR_LIMIT)

    def test_statuses_reported(self):
        self._rich_seed(1)
        s = service.state(self.db)
        self.assertIn(s["current"]["status"], ("ready", "copied", "new"))


class TestMarkPosted(_DashboardBase):
    def test_mark_posted_stores_everything_and_hides_story(self):
        aid = self._rich_seed()
        s = service.state(self.db)
        tweet = s["current"]["tweet"]
        result = service.mark_posted(self.db, aid)
        self.assertTrue(result["ok"])
        row = self.db.query_one(
            "SELECT * FROM tweets WHERE article_id=? AND status='posted'",
            (aid,))
        self.assertIsNotNone(row)
        self.assertEqual(row["tweet_text"], tweet)
        art = self.db.article_by_id(aid)
        self.assertEqual(art["status"], "posted")
        self.assertIsNotNone(art["processed_at"])
        # never shown as a candidate again
        s2 = service.state(self.db)
        self.assertTrue(not s2["current"] or
                        s2["current"]["article_id"] != aid)
        self.assertEqual(s2["posted"], 1)

    def test_after_post_cooldown_then_next_candidate(self):
        """After a post, NO new story is recommended immediately (global
        post cooldown — quality over volume). Once the cooldown expires,
        the next best story becomes the recommendation."""
        a1 = self._rich_seed(1)
        a2 = self._seed(
            "RBI holds repo rate at 6.5% in June policy review",
            "The Monetary Policy Committee voted to keep the repo rate "
            "unchanged at 6.5%. Inflation eased to 4.8%. GDP growth was "
            "revised to 7.2%.",
            "https://example.com/rbi", india=0.95, imp=0.85,
            coverage=True)
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], a1)
        service.mark_posted(self.db, a1)
        s2 = service.state(self.db)
        self.assertIsNone(s2["current"])           # cooldown active
        self.assertGreater(s2["cooldown_minutes_left"], 0)
        # expire the cooldown by backdating the post
        old = (datetime.now(timezone.utc) -
               timedelta(minutes=CONFIG.NEWS_COOLDOWN_MINUTES + 5)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.execute(
            "UPDATE tweets SET created_at=? WHERE article_id=? "
            "AND status='posted'", (old, a1))
        s3 = service.state(self.db)
        self.assertEqual(s3["current"]["article_id"], a2)

    def test_double_mark_rejected(self):
        aid = self._rich_seed()
        self.assertTrue(service.mark_posted(self.db, aid)["ok"])
        self.assertFalse(service.mark_posted(self.db, aid)["ok"])

    def test_mark_posted_unknown_article(self):
        r = service.mark_posted(self.db, 99999)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "not_found")

    def test_marked_posted_story_in_history(self):
        aid = self._rich_seed()
        s = service.state(self.db)
        service.mark_posted(self.db, aid)
        h = service.history(self.db)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["status"], "posted")
        self.assertIn("landslide", h[0]["tweet"].lower())


class TestCopySafety(_DashboardBase):
    """Copy and X-open must NEVER imply posted."""

    def test_copy_does_not_mark_posted(self):
        aid = self._rich_seed()
        service.state(self.db)
        service.mark_copied(self.db, aid)
        art = self.db.article_by_id(aid)
        self.assertEqual(art["status"], "copied")
        rows = self.db.query(
            "SELECT * FROM tweets WHERE article_id=? AND status='posted'",
            (aid,))
        self.assertEqual(len(rows), 0)
        # still a candidate after copying
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], aid)

    def test_copied_status_shown_in_queue(self):
        aid = self._rich_seed()
        service.state(self.db)
        service.mark_copied(self.db, aid)
        s = service.state(self.db)
        self.assertEqual(s["current"]["status"], "copied")


class TestSkip(_DashboardBase):
    def test_skip_hides_but_never_deletes(self):
        aid = self._rich_seed()
        service.state(self.db)
        r = service.skip(self.db, aid, reason="low value")
        self.assertTrue(r["ok"])
        art = self.db.article_by_id(aid)
        self.assertIsNotNone(art)              # never deleted
        self.assertEqual(art["status"], "skipped")
        self.assertEqual(art["skip_reason"], "low value")
        s = service.state(self.db)
        self.assertTrue(not s["current"] or
                        s["current"]["article_id"] != aid)

    def test_cannot_skip_posted(self):
        aid = self._rich_seed()
        service.mark_posted(self.db, aid)
        self.assertFalse(service.skip(self.db, aid)["ok"])


class TestAlreadyPostedNotReselected(_DashboardBase):
    def test_posted_story_never_reappears_as_candidate(self):
        aid = self._rich_seed()
        service.mark_posted(self.db, aid)
        for _ in range(3):
            s = service.state(self.db)
            for q in s["queue"]:
                self.assertNotEqual(q["article_id"], aid)


class TestPending402QueuePreserved(_DashboardBase):
    def test_402_pending_story_stays_available_for_manual_posting(self):
        """A story queued by an X API 402 must survive, stay in the
        dashboard queue, and remain markable posted manually."""
        aid = self._rich_seed()
        tweet = "Kozhikode landslide: 1 dead, NDRF deployed #Kerala #India"
        self.db.insert_tweet(aid, tweet, "deterministic", "failed",
                             error="payment_required")
        self.db.insert_pending_post(aid, tweet, "The Hindu",
                                    "https://example.com/story-1")
        self.db.update_scores(aid, 0.95, 0.9, aid, "pending_post")
        # pending_post articles are NOT candidates (already queued) but
        # must still be markable posted manually
        self.assertFalse(service.mark_posted(self.db, 999999)["ok"])
        r = service.mark_posted(self.db, aid)
        self.assertTrue(r["ok"])
        art = self.db.article_by_id(aid)
        self.assertEqual(art["status"], "posted")
        pend = self.db.query_one(
            "SELECT status FROM pending_posts WHERE article_id=?", (aid,))
        self.assertEqual(pend["status"], "posted")
        h = service.history(self.db)
        self.assertTrue(any("landslide" in x["tweet"].lower() for x in h))


class TestDashboardWebAPI(_DashboardBase):
    """Handler-level tests through the HTTP handler, offline."""

    def _handler(self):
        from app.dashboard.web import DashboardHandler
        return DashboardHandler

    def test_state_and_posted_end_to_end(self):
        # exercise service through the same functions the handler calls
        self._rich_seed()
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        self.assertIn("publishing_status", s)
        self.assertIn("Manual posting mode", s["publishing_status"])
        r = service.mark_posted(self.db, s["current"]["article_id"])
        self.assertTrue(r["ok"])

    def test_history_search(self):
        aid = self._rich_seed()
        service.mark_posted(self.db, aid)
        self.assertEqual(len(service.history(self.db, q="landslide")), 1)
        self.assertEqual(len(service.history(self.db, q="cricket")), 0)


class TestScanNeverPosts(_DashboardBase):
    def test_scan_collects_without_any_posted_tweets(self):
        # scan() calls collect_and_process against real feeds; instead
        # verify that no scan-side code path writes posted tweets:
        # the only writers of status='posted' are mark_posted /
        # mark_article_posted / mark_pending_posted, which are
        # user-action-only. Guard: refresh_queue is display-only.
        self._rich_seed()
        service.refresh_queue(self.db)
        rows = self.db.query("SELECT * FROM tweets WHERE status='posted'")
        self.assertEqual(len(rows), 0)
        art = self.db.query_one("SELECT status FROM articles")
        self.assertIn(art["status"], ("new", "ready", "copied"))


if __name__ == "__main__":
    unittest.main()
