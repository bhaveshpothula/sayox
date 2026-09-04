"""Tests for the selective publishing workflow: 3 tweet options with
best-hook selection, the 0-1 hashtag policy (no forced tags), the
publish-score quality bar, the global post cooldown (no next story
immediately after a post), the topic cooldown, and the breaking-story
override. Fully offline."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import CONFIG
from app.dashboard import service
from app.database import Database


def _fresh_iso(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backdate_post(db, article_id, minutes_ago):
    old = (datetime.now(timezone.utc) -
           timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE tweets SET created_at=? WHERE article_id=? "
               "AND status='posted'", (old, article_id))


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._d.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self._d.cleanup()

    def _seed(self, title, summary, url, india=0.95, imp=0.9,
              published=None, source="The Hindu", n=1, coverage=False):
        """Insert one scored article. coverage=True adds a second,
        weaker article from another outlet in the same cluster, so the
        story is 'confirmed' by 2 independent sources (momentum >= 50)."""
        aid = self.db.insert_article({
            "url": url, "normalized_url": url, "url_hash": "h-%s" % url,
            "title": title, "summary": summary, "source": source,
            "category": "india", "country": "IN", "reliability": 0.95,
            "published_at": published or _fresh_iso(1)})
        self.db.update_scores(aid, india, imp, aid, "new")
        if coverage:
            self._sibling(aid, title, summary, url, india)
        return aid

    def _sibling(self, aid, title, summary, url, india=0.95, imp=0.75,
                 source="NDTV", published=None):
        """A second outlet's report on the same story: same cluster,
        lower importance — real coverage for momentum, but never
        outranks the primary article."""
        sid = self.db.insert_article({
            "url": url + "-coverage", "normalized_url": url + "-coverage",
            "url_hash": "h-%s-coverage" % url,
            "title": title + " — latest updates",
            "summary": summary, "source": source,
            "category": "india", "country": "IN", "reliability": 0.9,
            "published_at": published or _fresh_iso(1)})
        self.db.update_scores(sid, india, imp, aid, "new")
        return sid

    def _landslide(self, n=1):
        return self._seed(
            "Kerala landslide in Kozhikode kills %d, NDRF deploys teams" % n,
            "A landslide triggered by heavy rainfall struck Kozhikode in "
            "Kerala on Tuesday. %d people died. NDRF teams were deployed. "
            "Rail connectivity was restored by evening." % n,
            "https://example.com/landslide-%d" % n, coverage=True)

    def _rbi(self):
        return self._seed(
            "RBI holds repo rate at 6.5% in June policy review",
            "The Monetary Policy Committee voted to keep the repo rate "
            "unchanged at 6.5%. Inflation eased to 4.8%. GDP growth was "
            "revised to 7.2%.",
            "https://example.com/rbi", coverage=True)


class TestTweetOptions(_Base):
    def test_three_options_and_story_conditioned_selection(self):
        self._landslide()
        s = service.state(self.db)
        cur = s["current"]
        styles = [o["style"] for o in cur["options"]]
        self.assertIn("briefing", styles)
        self.assertIn("flash", styles)
        self.assertGreaterEqual(len(cur["options"]), 2)
        # the recommended tweet IS one of the generated variants — the
        # story-conditioned objective picks among them (deterministic;
        # selection rules tested in test_reach_potential.py)
        self.assertIn(cur["tweet"], [o["text"] for o in cur["options"]])
        self.assertLessEqual(len(cur["tweet"]), CONFIG.TWEET_CHAR_LIMIT)
        for o in cur["options"]:
            self.assertLessEqual(len(o["text"]), CONFIG.TWEET_CHAR_LIMIT)

    def test_every_option_is_valid_tweet(self):
        self._landslide()
        from app.tweet.validator import validate_tweet
        s = service.state(self.db)
        for o in s["current"]["options"]:
            ok, reason = validate_tweet(o["text"], CONFIG.TWEET_CHAR_LIMIT)
            self.assertTrue(ok, reason)


class TestHashtagPolicy(_Base):
    def test_at_most_two_tags_and_no_generic_filler(self):
        """0-2 tags: the first is the strongest specific topical match;
        a second appears only when it names the story's own location
        (#Landslide + #Kerala). #India and other generic tags never
        fill the quota, and fewer tags is a fine outcome."""
        aid = self._landslide()
        article = dict(self.db.article_by_id(aid))
        tweet, tags, _ = service._build_tweet(self.db, article)
        self.assertIsNotNone(tweet)
        self.assertLessEqual(len(tags), 2)
        self.assertIn("#Landslide", tags)
        self.assertNotIn("#India", tags)
        if len(tags) == 2:
            # the second tag must be a location specific to THIS story
            self.assertIn(tags[1], ("#Kerala", "#Kozhikode"))
        self.assertLessEqual(tweet.count("#"), 2)

    def test_two_specific_tags_entity_plus_location(self):
        """#Earthquake #Delhi — two strongly specific tags are allowed
        together (the approved 1-2 policy)."""
        aid = self._seed(
            "Massive earthquake strikes Delhi, several buildings collapse",
            "A strong earthquake struck Delhi on Tuesday. Rescue teams "
            "were deployed across the region. Several buildings "
            "collapsed.",
            "https://example.com/quake-tags", india=0.95, imp=0.9)
        article = dict(self.db.article_by_id(aid))
        tweet, tags, _ = service._build_tweet(self.db, article)
        self.assertIsNotNone(tweet)
        self.assertEqual(tags, ["#Earthquake", "#Delhi"])
        self.assertLessEqual(len(tweet), CONFIG.TWEET_CHAR_LIMIT)

    def test_second_tag_never_generic(self):
        """A story with a topical tag but NO location match keeps exactly
        one tag — no generic filler is added to reach the quota."""
        aid = self._seed(
            "RBI holds repo rate at 6.5% in June policy review",
            "The Monetary Policy Committee voted to keep the repo rate "
            "unchanged at 6.5%. Inflation eased to 4.8%.",
            "https://example.com/rbi-tags", india=0.95, imp=0.9)
        article = dict(self.db.article_by_id(aid))
        tweet, tags, _ = service._build_tweet(self.db, article)
        self.assertIsNotNone(tweet)
        self.assertEqual(tags, ["#RBI"])

    def test_no_tag_when_nothing_highly_relevant(self):
        """A story with no topical/location match gets NO hashtag —
        tags are never forced into a tweet."""
        aid = self._seed(
            "Unusual weather pattern puzzles researchers",
            "Scientists are studying an unusual shift observed over "
            "several weeks. The pattern does not match known cycles.",
            "https://example.com/weather", india=0.95)
        article = dict(self.db.article_by_id(aid))
        tweet, tags, _ = service._build_tweet(self.db, article)
        self.assertIsNotNone(tweet)
        self.assertEqual(tags, [])
        self.assertEqual(tweet.count("#"), 0)


class TestQualityBar(_Base):
    def test_weak_story_never_recommended(self):
        self._seed(
            "Local club announces weekly meeting schedule",
            "The club published its timetable for the coming month. "
            "Members may attend any session. Refreshments are included.",
            "https://example.com/club", india=0.4, imp=0.3)
        s = service.state(self.db)
        self.assertIsNone(s["current"])
        self.assertIn("No story worth posting", s["recommendation_note"])


class TestGlobalPostCooldown(_Base):
    def test_no_recommendation_immediately_after_post(self):
        a1 = self._landslide(1)
        a2 = self._rbi()
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        service.mark_posted(self.db, a1)
        s2 = service.state(self.db)
        self.assertIsNone(s2["current"])       # quality over volume
        self.assertGreater(s2["cooldown_minutes_left"], 0)
        # after the cooldown, the next strong story is recommended
        _backdate_post(self.db, a1, CONFIG.NEWS_COOLDOWN_MINUTES + 5)
        s3 = service.state(self.db)
        self.assertEqual(s3["current"]["article_id"], a2)


class TestTopicCooldown(_Base):
    def test_same_topic_story_suppressed_after_post(self):
        a1 = self._landslide(1)
        service.mark_posted(self.db, a1, "posted text")
        # global cooldown expired, topic cooldown still active
        _backdate_post(self.db, a1, CONFIG.NEWS_COOLDOWN_MINUTES + 5)
        # a NEW story about the same topic is suppressed… (importance
        # below the major-new-development override bar)
        self._seed(
            "Kerala landslide in Kozhikode kills 2, NDRF deploys teams",
            "A landslide triggered by heavy rainfall struck Kozhikode in "
            "Kerala on Tuesday. 2 people died. NDRF teams were deployed. "
            "Rail connectivity was restored by evening.",
            "https://example.com/landslide-2", imp=0.85, coverage=True)
        s = service.state(self.db)
        self.assertIsNone(s["current"])
        # …while a different-topic strong story is recommended
        self._rbi()
        s2 = service.state(self.db)
        self.assertIn("repo rate", s2["current"]["headline"])


class TestBreakingOverride(_Base):
    def test_breaking_story_overrides_global_cooldown(self):
        a1 = self._landslide(1)
        service.mark_posted(self.db, a1, "posted text")
        # still inside the global post cooldown
        s = service.state(self.db)
        self.assertIsNone(s["current"])
        # a genuinely breaking story: 6 outlets, just published, major
        a2 = self._seed(
            "BREAKING: massive earthquake strikes Delhi, several "
            "buildings collapse",
            "A strong earthquake struck the capital on Tuesday. Rescue "
            "teams were deployed across the region. Several buildings "
            "collapsed.",
            "https://example.com/quake", india=0.95, imp=0.95,
            published=_fresh_iso(0))
        for i, src in enumerate(("Hindu", "NDTV", "TOI", "IE", "HT",
                                 "BS")):
            aid = self.db.insert_article({
                "url": "https://example.com/quake-%d" % i,
                "normalized_url": "https://example.com/quake-%d" % i,
                "url_hash": "hq-%d" % i,
                "title": "Earthquake in Delhi — %s report" % src,
                "summary": "A strong earthquake struck the capital.",
                "source": src, "category": "india", "country": "IN",
                "reliability": 0.95, "published_at": _fresh_iso(0)})
            self.db.update_scores(aid, 0.95, 0.95, a2, "new")
        self.db.update_scores(a2, 0.95, 0.95, a2, "new")
        s2 = service.state(self.db)
        self.assertIsNotNone(s2["current"])
        self.assertEqual(s2["current"]["article_id"], a2)


class TestCooldownUIState(_Base):
    """During cooldown the engine recommends NOTHING. The queue may
    still exist (for browsing), but the recommendation gate must stay
    closed — this is the state the UI relies on to hide all posting
    actions."""

    def test_cooldown_state_carries_no_recommendation_but_queue_exists(self):
        a1 = self._landslide(1)
        self._rbi()
        service.mark_posted(self.db, a1)
        s = service.state(self.db)
        self.assertIsNone(s["current"])              # no recommendation
        self.assertGreater(s["cooldown_minutes_left"], 0)
        self.assertGreaterEqual(s["candidates"], 1)  # queue still browsable
        self.assertIn("Cooldown", s["recommendation_note"])
        # the cooldown panel needs the last posted story
        self.assertIsNotNone(s["last_posted"])
        self.assertIn("landslide", (s["last_posted"]["headline"] or "").lower())
        self.assertIsNotNone(s["last_posted"]["posted_at"])

    def test_ui_has_no_queue_fallback_for_current_story(self):
        """The dashboard UI must never promote queue[0] to the current
        story while the recommendation gate is closed."""
        import io
        import os.path
        html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "dashboard", "static", "index.html")
        with io.open(html_path, encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("state.queue[0]", html)
        # the gated entry point is present
        self.assertIn("state.current == null", html)

    def test_recommendation_returns_after_cooldown_expires(self):
        a1 = self._landslide(1)
        a2 = self._rbi()
        service.mark_posted(self.db, a1)
        _backdate_post(self.db, a1, CONFIG.NEWS_COOLDOWN_MINUTES + 5)
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        self.assertEqual(s["current"]["article_id"], a2)
        self.assertEqual(s["cooldown_minutes_left"], 0)


class TestRecommendationStability(_Base):
    """The current recommendation is sticky: a challenger needs the
    configured MIN_SCORE_IMPROVEMENT margin (or a genuinely breaking
    story) to displace it; an ineligible incumbent is replaced."""

    def setUp(self):
        super().setUp()
        self._saved_margin = CONFIG.MIN_SCORE_IMPROVEMENT

    def tearDown(self):
        CONFIG.MIN_SCORE_IMPROVEMENT = self._saved_margin
        super().tearDown()

    def test_recommendation_is_persisted(self):
        aid = self._landslide(1)
        service.state(self.db)
        row = self.db.get_recommendation()
        self.assertIsNotNone(row)
        self.assertEqual(row["article_id"], aid)

    def test_marginal_challenger_does_not_flip_recommendation(self):
        """A challenger only marginally better (well under the margin)
        must NOT displace the incumbent across refreshes."""
        a1 = self._seed(
            "Cyclone Remal kills 3 as it intensifies over Bengal coast",
            "Cyclone Remal intensified over the Bay of Bengal on Tuesday. "
            "Three villages were cut off by flooding. The weather "
            "department issued warnings for the coast.",
            "https://example.com/cyclone-a", imp=0.95, coverage=True)
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], a1)
        # nearly identical strength: importance +0.03 => publish +0.45
        self._seed(
            "Cyclone Shakti kills 3 as it intensifies over Bengal coast",
            "Cyclone Shakti intensified over the Bay of Bengal on Tuesday. "
            "Three villages were cut off by flooding. The weather "
            "department issued warnings for the coast.",
            "https://example.com/cyclone-b", imp=0.98, coverage=True)
        s2 = service.state(self.db)
        self.assertEqual(s2["current"]["article_id"], a1)   # incumbent kept

    def test_clearly_better_challenger_replaces_incumbent(self):
        """With no margin required, any strictly better story replaces
        the incumbent."""
        CONFIG.MIN_SCORE_IMPROVEMENT = 0
        a1 = self._seed(
            "Cyclone Remal kills 3 as it intensifies over Bengal coast",
            "Cyclone Remal intensified over the Bay of Bengal on Tuesday. "
            "Three villages were cut off by flooding. The weather "
            "department issued warnings for the coast.",
            "https://example.com/cyclone-a", imp=0.90, coverage=True)
        service.state(self.db)
        a2 = self._seed(
            "Massive earthquake strikes Delhi, several buildings collapse",
            "A strong earthquake struck the capital on Tuesday. Rescue "
            "teams were deployed across the region. Several buildings "
            "collapsed.",
            "https://example.com/quake", imp=0.97)
        # multi-outlet coverage => clearly stronger publish score
        for i, src in enumerate(("NDTV", "TOI", "IE")):
            aid = self.db.insert_article({
                "url": "https://example.com/quake-%d" % i,
                "normalized_url": "https://example.com/quake-%d" % i,
                "url_hash": "hq-%d" % i,
                "title": "Earthquake in Delhi — %s report" % src,
                "summary": "A strong earthquake struck the capital.",
                "source": src, "category": "india", "country": "IN",
                "reliability": 0.95, "published_at": _fresh_iso(1)})
            self.db.update_scores(aid, 0.95, 0.95, a2, "new")
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], a2)

    def test_ineligible_incumbent_is_replaced(self):
        """The incumbent loses all protection once it stops passing the
        gates (e.g. the user skips it)."""
        a1 = self._landslide(1)
        a2 = self._rbi()
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], a1)
        service.skip(self.db, a1, "Not important")
        s2 = service.state(self.db)
        self.assertIsNotNone(s2["current"])
        self.assertEqual(s2["current"]["article_id"], a2)

    def test_breaking_story_replaces_incumbent_without_margin(self):
        """A genuinely breaking story overrides the stability margin."""
        CONFIG.MIN_SCORE_IMPROVEMENT = 200     # impossible margin
        a1 = self._landslide(1)
        service.state(self.db)
        a2 = self._seed(
            "BREAKING: massive earthquake strikes Delhi, several "
            "buildings collapse",
            "A strong earthquake struck the capital on Tuesday. Rescue "
            "teams were deployed across the region. Several buildings "
            "collapsed.",
            "https://example.com/quake", india=0.95, imp=0.95,
            published=_fresh_iso(0))
        for i, src in enumerate(("Hindu", "NDTV", "TOI", "IE", "HT", "BS")):
            aid = self.db.insert_article({
                "url": "https://example.com/quake-%d" % i,
                "normalized_url": "https://example.com/quake-%d" % i,
                "url_hash": "hq-%d" % i,
                "title": "Earthquake in Delhi — %s report" % src,
                "summary": "A strong earthquake struck the capital.",
                "source": src, "category": "india", "country": "IN",
                "reliability": 0.95, "published_at": _fresh_iso(0)})
            self.db.update_scores(aid, 0.95, 0.95, a2, "new")
        self.db.update_scores(a2, 0.95, 0.95, a2, "new")
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], a2)

    def test_no_recommendation_clears_persisted_state(self):
        """When nothing passes the gates, no stale recommendation may
        survive in the DB."""
        a1 = self._landslide(1)
        service.state(self.db)
        self.assertIsNotNone(self.db.get_recommendation())
        service.mark_posted(self.db, a1)       # starts the cooldown
        service.state(self.db)
        self.assertIsNone(self.db.get_recommendation())


class TestTopicSimilarity(_Base):
    """Topic cooldown must key on entities/topical terms — generic
    newsroom vocabulary alone never makes two stories the same topic."""

    def test_generic_word_overlap_is_not_same_topic(self):
        posted = ["Government announces new tax policy for businesses"]
        for title in ("Police arrest suspect in Hyderabad murder case",
                      "State minister says relief work is complete",
                      "Officials report progress in rescue operations"):
            self.assertFalse(service._similar_to_posted(title, posted),
                             "false positive: %s" % title)

    def test_same_topic_story_is_still_detected(self):
        posted = ["Kerala landslide in Kozhikode kills 5, NDRF deploys teams"]
        self.assertTrue(service._similar_to_posted(
            "Kerala landslide in Kozhikode kills 9, NDRF deploys teams",
            posted))

    def test_headline_of_only_generic_words_matches_nothing(self):
        # no topical tokens survive the filter — never "similar"
        self.assertFalse(service._similar_to_posted(
            "Government announces new policy", ["Officials said the state"]))

    def test_unrelated_stories_are_both_recommendable(self):
        """End-to-end: after posting a generic-word story, an unrelated
        story sharing only generic words is NOT topic-suppressed."""
        a1 = self._seed(
            "Government announces new tax policy for businesses",
            "The government announced a new tax policy on Tuesday. "
            "Officials said the policy applies from next month. "
            "Businesses welcomed the changes.",
            "https://example.com/tax", imp=0.9)
        service.mark_posted(self.db, a1, "posted text")
        _backdate_post(self.db, a1, CONFIG.NEWS_COOLDOWN_MINUTES + 5)
        self._seed(
            "Police arrest 3 suspects in Hyderabad murder case, probe "
            "underway",
            "Police arrested three suspects in Hyderabad on Tuesday. "
            "One person was killed in the attack. Investigators "
            "recovered the weapon. The probe is ongoing.",
            "https://example.com/arrest", imp=0.9, coverage=True)
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])   # not topic-suppressed
        self.assertIn("Hyderabad", s["current"]["headline"])


class TestQueueDeduplication(_Base):
    """One entry per STORY: cluster siblings never fill multiple queue
    slots; the highest-reach article represents the story. Presentation
    only — sibling articles are never deleted."""

    def _article(self, title, imp, cluster=None, url=None, source="The Hindu"):
        aid = self.db.insert_article({
            "url": url, "normalized_url": url, "url_hash": "h-%s" % url,
            "title": title,
            "summary": "Officials confirmed the developments reported in "
                       "the story on Tuesday. Further details are awaited.",
            "source": source, "category": "india", "country": "IN",
            "reliability": 0.95, "published_at": _fresh_iso(1)})
        self.db.update_scores(aid, 0.95, imp, cluster or aid, "new")
        return aid

    def test_one_entry_per_cluster_and_best_representative(self):
        # one story, 3 articles in the same cluster; the middle one has
        # the highest reach (importance), so it represents the story
        a1 = self._article("Cyclone Remal hits Bengal coast", 0.85,
                           url="https://e.com/a1")
        a2 = self._article("Cyclone Remal hits Bengal coast, alerts", 0.92,
                           cluster=a1, url="https://e.com/a2", source="NDTV")
        a3 = self._article("Cyclone Remal hits Bengal coast, damage", 0.80,
                           cluster=a1, url="https://e.com/a3", source="TOI")
        queue = service.refresh_queue(self.db)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["article_id"], a2)
        # siblings survive as articles (presentation-only dedup)
        self.assertEqual(self.db.query(
            "SELECT COUNT(*) c FROM articles WHERE story_cluster_id=?",
            (a1,))[0]["c"], 3)

    def test_current_story_cluster_not_in_up_next(self):
        """The current story keeps its single queue entry (it stays
        browsable) but its cluster never ALSO fills Up Next."""
        a1 = self._landslide(1)   # primary + coverage sibling, one cluster
        self._rbi()
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        for e in s["up_next"]:
            self.assertNotEqual(e["cluster_id"], s["current"]["cluster_id"])
        # one entry per cluster still holds in the browsable queue
        clusters = [e["cluster_id"] for e in s["queue"]]
        self.assertEqual(len(clusters), len(set(clusters)))


class TestTwoTierQualityGate(_Base):
    """An ordinary story must be both genuinely important AND confirmed
    by multiple outlets (momentum) before it is recommended — a
    single-source story with hot keywords is not enough."""

    def test_single_source_hot_story_not_recommended(self):
        # importance 0.92, publish score above the quality bar — but ONE
        # outlet: momentum 40 < 50, so no recommendation
        self._seed(
            "Massive earthquake strikes Delhi, several buildings collapse",
            "A strong earthquake struck the capital on Tuesday. Rescue "
            "teams were deployed across the region. Several buildings "
            "collapsed.",
            "https://example.com/solo-quake", imp=0.92)
        s = service.state(self.db)
        self.assertIsNone(s["current"])

    def test_confirmed_story_recommended(self):
        # same story, two independent outlets => momentum >= 50
        a1 = self._seed(
            "Massive earthquake strikes Delhi, several buildings collapse",
            "A strong earthquake struck the capital on Tuesday. Rescue "
            "teams were deployed across the region. Several buildings "
            "collapsed.",
            "https://example.com/quake-1", imp=0.92)
        a2 = self._seed(
            "Earthquake in Delhi: rescue teams search collapsed buildings",
            "Rescue teams searched collapsed buildings in Delhi after a "
            "strong earthquake on Tuesday. Officials confirmed the "
            "damage.",
            "https://example.com/quake-2", imp=0.85, source="NDTV")
        self.db.update_scores(a2, 0.95, 0.85, a1, "new")
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        self.assertEqual(s["current"]["article_id"], a1)

    def test_low_importance_confirmed_story_not_recommended(self):
        # confirmed by two outlets, publish above the bar — but
        # importance 0.7 < 80, so no recommendation
        self._landslide(1)   # rich story, but importance floored at 0.7
        self.db.execute(
            "UPDATE articles SET importance_score=0.7 "
            "WHERE id IN (SELECT id FROM articles)")
        s = service.state(self.db)
        self.assertIsNone(s["current"])


if __name__ == "__main__":
    unittest.main()
