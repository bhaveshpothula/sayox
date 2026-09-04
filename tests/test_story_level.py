"""Story-level pipeline tests (P1-P7 changeset): clusterer any-member
matching, headline-justified hashtags, cluster-level recommendation
competition, descriptive tiers, and the XRP tiebreak (advisory, never
a gate). Fully offline; no existing tests are weakened."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import CONFIG
from app.dashboard import service
from app.database import Database
from app.news.clusterer import cluster_articles
from app.tweet.hashtags import suggest_hashtags


def _fresh_iso(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestClustererAnyMember(unittest.TestCase):
    """P1: a candidate joins a cluster when it matches ANY member —
    cross-outlet phrasing must not split one event into single-article
    clusters (which downstream reads as 'no momentum')."""

    def test_chain_match_merges_into_existing_cluster(self):
        # art3 matches art2 but NOT the cluster's first article (jaccard
        # 0.30 vs art1, 0.67 vs art2 after stopword removal): the old
        # rep-only comparison left it in a cluster of its own
        arts = [
            {"id": 1, "title": "Cyclone Remal makes landfall in West Bengal"},
            {"id": 2, "title":
                "Cyclone Remal makes landfall in Bengal, NDRF teams on alert"},
            {"id": 3, "title": "Cyclone Remal landfall: NDRF teams on alert"},
        ]
        clusters = cluster_articles(arts, 0.45)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]), [1, 2, 3])

    def test_unrelated_story_stays_separate(self):
        arts = [
            {"id": 1, "title": "Cyclone Remal makes landfall in West Bengal"},
            {"id": 2, "title": "RBI holds repo rate at 6.5 percent"},
        ]
        clusters = cluster_articles(arts, 0.45)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sorted(c[0] for c in clusters), [1, 2])


class TestHeadlineHashtags(unittest.TestCase):
    """P5: a hashtag must be justified by the HEADLINE. Summary-body
    keywords describe background context, not this story."""

    def test_body_only_topic_match_emits_nothing(self):
        # the live-audit false positive: a sentencing story whose body
        # mentions shares/Mumbai must not get #Markets #Mumbai
        pool = suggest_hashtags(
            "Pala MLA sentenced in cheating case",
            "The court heard arguments about shares traded on the "
            "Mumbai market and sealed a verdict.", india_score=0.95)
        self.assertNotIn("#Markets", pool)
        self.assertNotIn("#Mumbai", pool)
        self.assertEqual(service._select_tags(pool), [])

    def test_headline_topic_match_wins(self):
        pool = suggest_hashtags(
            "SEBI tightens disclosure rules for shares",
            "Markets reacted calmly; the Mumbai bourse was flat.",
            india_score=0.95)
        self.assertIn("#SEBI", pool)
        self.assertEqual(service._select_tags(pool)[0], "#SEBI")

    def test_body_only_location_never_second_tag(self):
        pool = suggest_hashtags(
            "Earthquake jolts Delhi, no damage reported",
            "Tremors were also felt by residents in Mumbai.", 0.95)
        tags = service._select_tags(pool)
        self.assertEqual(tags, ["#Earthquake", "#Delhi"])
        self.assertNotIn("#Mumbai", tags)

    def test_zero_tags_when_no_headline_match(self):
        self.assertEqual(service._select_tags(suggest_hashtags(
            "Unusual weather pattern puzzles researchers",
            "Scientists cite shares of data from Mumbai stations.",
            0.95)), [])

    def test_never_forces_india_or_fabricated_trends(self):
        pool = suggest_hashtags(
            "Unusual weather pattern puzzles researchers",
            "Scientists are studying the shift.", 0.2)
        self.assertEqual(pool, [])


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._d.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self._d.cleanup()

    def _seed(self, title, summary, url, india=0.95, imp=0.9,
              source="The Hindu", hours_ago=1, coverage=None):
        """Insert one article. coverage=[(source, hours_ago), ...] adds
        sibling articles of the same story in the same cluster."""
        aid = self.db.insert_article({
            "url": url, "normalized_url": url, "url_hash": "h-%s" % url,
            "title": title, "summary": summary, "source": source,
            "category": "india", "country": "IN", "reliability": 0.95,
            "published_at": _fresh_iso(hours_ago)})
        self.db.update_scores(aid, india, imp, aid, "new")
        for i, (sib_src, sib_h) in enumerate(coverage or []):
            sid = self.db.insert_article({
                "url": "%s-c%d" % (url, i),
                "normalized_url": "%s-c%d" % (url, i),
                "url_hash": "h-%s-c%d" % (url, i),
                "title": title + " — updates", "summary": summary,
                "source": sib_src, "category": "india", "country": "IN",
                "reliability": 0.9, "published_at": _fresh_iso(sib_h)})
            self.db.update_scores(sid, india, 0.75, aid, "new")
        return aid

    def _quake(self, n=1, hours_ago=1, imp=0.9, coverage=()):
        return self._seed(
            "Massive earthquake strikes Delhi, several buildings "
            "collapse %d" % n,
            "A strong earthquake struck Delhi on Tuesday. %d people were "
            "injured. Rescue teams were deployed across the region." % n,
            "https://example.com/quake-%d-%d" % (n, hours_ago),
            imp=imp, hours_ago=hours_ago, coverage=list(coverage))


class TestStoryLevelCompetition(_Base):
    """P2: stories compete as CLUSTERS. A multi-source developing story
    beats a fresh weak single-source article; siblings never enter the
    competition separately."""

    def test_multi_source_story_beats_fresh_weak_single(self):
        # 3 independent outlets covering one quake vs a brand-new
        # single-source story: momentum must decide, not novelty
        strong = self._quake(1, coverage=(("NDTV", 1), ("TOI", 2)))
        self._seed(
            "Minister inaugurates new bridge in Assam",
            "Officials attended the ceremony on Tuesday. Traffic will "
            "open next month.",
            "https://example.com/bridge", imp=0.9)
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        self.assertEqual(s["current"]["article_id"], strong)
        # the developing multi-source story earns the Trending tier
        self.assertEqual(s["current"]["tier"], "Trending")

    def test_weak_single_source_is_weak_tier_not_recommended(self):
        self._seed(
            "Minister inaugurates new bridge in Assam",
            "Officials attended the ceremony on Tuesday.",
            "https://example.com/bridge")
        s = service.state(self.db)
        self.assertIsNone(s["current"])
        weak = [e for e in s["queue"] if e["tier"] == "Weak"]
        self.assertTrue(weak)

    def test_siblings_compete_once_not_separately(self):
        # one story, 3 articles: exactly ONE gate-passing competitor,
        # and the representative (not a sibling) is the recommendation
        a1 = self._quake(1, coverage=(("NDTV", 1), ("TOI", 2)))
        s = service.state(self.db)
        clusters = [e["cluster_id"] for e in s["queue"]]
        self.assertEqual(len(clusters), len(set(clusters)))
        self.assertEqual(s["current"]["article_id"], a1)


class TestStoryTier(unittest.TestCase):
    """P3: tiers are descriptive, computed from the same thresholds the
    gates already use (plus the Trending momentum/freshness bars)."""

    def _bd(self, publish, momentum, importance, freshness):
        return {"publish": publish, "momentum": momentum,
                "importance": importance, "freshness": freshness}

    def test_weak(self):
        self.assertEqual(service._story_tier(
            self._bd(64, 40, 61, 94)), "Weak")

    def test_normal(self):
        self.assertEqual(service._story_tier(
            self._bd(75, 55, 85, 50)), "Normal")

    def test_trending_needs_momentum_and_freshness(self):
        self.assertEqual(service._story_tier(
            self._bd(75, 80, 85, 70)), "Trending")
        # strong momentum but stale -> only Normal
        self.assertEqual(service._story_tier(
            self._bd(75, 80, 85, 40)), "Normal")

    def test_breaking_is_all_three_bars(self):
        self.assertEqual(service._story_tier(
            self._bd(75, 91, 91, 91)), "Breaking")
        # importance one point short: not Breaking, and stale ->
        # not Trending either
        self.assertEqual(service._story_tier(
            self._bd(75, 91, 89, 40)), "Normal")


class TestXRPTiebreak(_Base):
    """P4: XRP is advisory. Within the hysteresis score margin a
    challenger wins ONLY with clearly higher reach potential; XRP never
    rescues a below-bar story."""

    def test_higher_xrp_challenger_displaces_incumbent(self):
        incumbent = self._quake(1, hours_ago=24, imp=0.9,
                                coverage=(("NDTV", 24),))
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], incumbent)
        # fresh challenger, slightly higher publish (inside the 10-pt
        # margin), clearly higher advisory reach potential
        self._quake(2, hours_ago=1, imp=0.82, coverage=(("NDTV", 1),))
        s = service.state(self.db)
        self.assertIsNotNone(s["current"])
        self.assertNotEqual(s["current"]["article_id"], incumbent)

    def test_similar_xrp_challenger_keeps_incumbent(self):
        incumbent = self._quake(1, hours_ago=1, coverage=(("NDTV", 1),))
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], incumbent)
        # equally fresh challenger with a tiny publish edge: XRP gap is
        # small, so the sticky recommendation survives
        self._quake(2, hours_ago=2, imp=0.9, coverage=(("NDTV", 2),))
        s = service.state(self.db)
        self.assertEqual(s["current"]["article_id"], incumbent)

    def test_no_flip_flop_across_refreshes(self):
        a = self._quake(1, hours_ago=1, coverage=(("NDTV", 1),))
        self._quake(2, hours_ago=2, imp=0.9, coverage=(("NDTV", 2),))
        first = service.state(self.db)["current"]
        ids = {first["article_id"]}
        for _ in range(3):
            cur = service.state(self.db)["current"]
            ids.add(cur["article_id"])
        self.assertEqual(len(ids), 1)
        self.assertIn(a, ids)

    def test_xrp_never_gates_a_below_bar_story(self):
        # maximally 'reachable' by XRP standards but a single weak
        # source: still no recommendation
        self._seed(
            "Massive earthquake strikes Delhi, several buildings "
            "collapse",
            "A strong earthquake struck Delhi. Rescue teams deployed.",
            "https://example.com/solo", imp=0.9)
        s = service.state(self.db)
        self.assertIsNone(s["current"])


class TestSourceAwareDedup(_Base):
    """Cross-outlet coverage must survive dedup: a near-identical title
    from a DIFFERENT source is confirmation evidence for clustering and
    momentum — only the same source re-publishing near-identical copy
    is a duplicate. URL-hash dedup is unchanged."""

    TITLE_A = "Cyclone Remal makes landfall in West Bengal, alerts issued"
    TITLE_B = "Cyclone Remal makes landfall in West Bengal"   # jaccard 0.75
    PUB = "Tue, 01 Sep 2026 17:00:00 +0000"

    def _entry(self, title, url, source):
        return {"title": title, "summary": "Severe weather hit the coast.",
                "url": url, "source": source, "category": "india",
                "source_country": "IN", "reliability": 0.95,
                "published_at": self.PUB}

    def _collect(self, feeds):
        """feeds: {source_name: [entries]} -> run the real collector
        with fetch_feed/enabled_sources stubbed (no network)."""
        from app.news import collector
        saved_feed, saved_srcs = (collector.fetch_feed,
                                  collector.sources_mod.enabled_sources)
        collector.fetch_feed = lambda src, timeout, user_agent: \
            feeds.get(src["name"], [])
        collector.sources_mod.enabled_sources = \
            lambda db: [{"name": n} for n in feeds]
        try:
            return collector.collect_and_process(self.db)
        finally:
            collector.fetch_feed = saved_feed
            collector.sources_mod.enabled_sources = saved_srcs

    def test_same_source_near_identical_title_deduplicated(self):
        stats = self._collect({"The Hindu": [
            self._entry(self.TITLE_A, "https://e.com/a", "The Hindu"),
            self._entry(self.TITLE_B, "https://e.com/b", "The Hindu")]})
        self.assertEqual(stats["discovered"], 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_different_source_near_identical_title_retained(self):
        stats = self._collect({
            "The Hindu": [self._entry(self.TITLE_A,
                                      "https://e.com/a", "The Hindu")],
            "NDTV": [self._entry(self.TITLE_B,
                                 "https://e.com/b", "NDTV")]})
        self.assertEqual(stats["discovered"], 2)
        self.assertEqual(stats["duplicates"], 0)

    def test_url_duplicate_still_deduplicated(self):
        stats = self._collect({"The Hindu": [
            self._entry(self.TITLE_A, "https://e.com/a", "The Hindu"),
            self._entry("Completely different headline about markets",
                        "https://e.com/a", "The Hindu")]})
        self.assertEqual(stats["discovered"], 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_cross_source_articles_share_one_cluster(self):
        self._collect({
            "The Hindu": [self._entry(self.TITLE_A,
                                      "https://e.com/a", "The Hindu")],
            "NDTV": [self._entry(self.TITLE_B,
                                 "https://e.com/b", "NDTV")]})
        clusters = self.db.query(
            "SELECT story_cluster_id, COUNT(*) c FROM articles "
            "GROUP BY story_cluster_id")
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["c"], 2)

    def test_clustering_threshold_unchanged(self):
        import inspect
        self.assertEqual(CONFIG.CLUSTER_TITLE_THRESHOLD, 0.45)
        self.assertEqual(inspect.signature(
            cluster_articles).parameters["threshold"].default, 0.45)

    def test_momentum_counts_sources_not_articles(self):
        """Momentum behavior unchanged: same source twice = one source
        (no confirmation bonus); two sources = the confirmation bump."""
        from app.news.ranking import momentum_score
        row = {"source": "The Hindu",
               "published_at": _fresh_iso(1)}
        one_source = momentum_score([row, dict(row)])[0]
        two_sources = momentum_score(
            [row, dict(row, source="NDTV")])[0]
        self.assertEqual(one_source, 54.0)    # 40 + accel 10 + newest 4
        self.assertEqual(two_sources, 66.5)   # +12.5 second outlet
        # invariant: same article count, only the source differs ->
        # exactly the second-outlet bonus (acceleration identical)
        self.assertEqual(one_source + 12.5, two_sources)


if __name__ == "__main__":
    unittest.main()
