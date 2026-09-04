"""X Reach Potential (advisory) tests: XRP calculation, shareability,
save value, semantic clarity, saturation, story-conditioned variant
selection, and the anti-sensation fact floor. Fully offline.

XRP is ADVISORY ONLY: it must never gate a recommendation, never
replace publish_score, and never be presented as an X algorithm score
or an impressions prediction."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import CONFIG
from app.dashboard import service
from app.database import Database
from app.news.ranking import (semantic_clarity_score, save_value_score,
                              shareability_score, x_reach_potential)


def _fresh_iso(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _article(title, summary, imp=0.9, published=None):
    return {"title": title, "summary": summary, "source": "The Hindu",
            "importance_score": imp, "india_relevance_score": 0.95,
            "reliability_score": 0.95,
            "published_at": published or _fresh_iso(1),
            "story_cluster_id": 1}


class _Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._d.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self._d.cleanup()

    def _seed(self, title, summary, url, india=0.95, imp=0.9,
              source="The Hindu", coverage=False):
        aid = self.db.insert_article({
            "url": url, "normalized_url": url, "url_hash": "h-%s" % url,
            "title": title, "summary": summary, "source": source,
            "category": "india", "country": "IN", "reliability": 0.95,
            "published_at": _fresh_iso(1)})
        self.db.update_scores(aid, india, imp, aid, "new")
        if coverage:
            sid = self.db.insert_article({
                "url": url + "-coverage", "normalized_url": url + "-coverage",
                "url_hash": "h-%s-coverage" % url,
                "title": title + " — latest updates", "summary": summary,
                "source": "NDTV", "category": "india", "country": "IN",
                "reliability": 0.9, "published_at": _fresh_iso(1)})
            self.db.update_scores(sid, india, 0.75, aid, "new")
        return aid

    def _landslide(self, n=1):
        return self._seed(
            "Kerala landslide in Kozhikode kills %d, NDRF deploys teams" % n,
            "A landslide triggered by heavy rainfall struck Kozhikode in "
            "Kerala on Tuesday. %d people died. NDRF teams were deployed. "
            "Rail connectivity was restored by evening." % n,
            "https://example.com/landslide-%d" % n, coverage=True)

    def _backdate_post(self, article_id, hours_ago):
        old = (datetime.now(timezone.utc) -
               timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.execute(
            "UPDATE tweets SET created_at=? WHERE article_id=? "
            "AND status='posted'", (old, article_id))


class TestXRPCalculation(_Base):
    def test_composite_in_range_and_labeled_as_editorial_estimate(self):
        xrp = x_reach_potential(_article(
            "Kerala landslide in Kozhikode kills 4, NDRF deploys teams",
            "A landslide struck Kozhikode in Kerala on Tuesday. 4 people "
            "died. NDRF teams were deployed."),
            cluster_rows=[_article("t", "s")])
        self.assertEqual(xrp["label"],
                         "Reach potential — editorial estimate")
        self.assertGreaterEqual(xrp["score"], 0.0)
        self.assertLessEqual(xrp["score"], 100.0)
        for key in ("recency", "momentum", "importance", "relevance",
                    "shareability", "discussion", "save_value", "media",
                    "source_quality"):
            self.assertIn(key, xrp["signals"])
        self.assertIsInstance(xrp["reasons"], list)

    def test_fresh_story_beats_stale_story(self):
        """Steep advisory decay: same story, 30h old loses heavily."""
        fresh = _article("Quake strikes Delhi region", "A strong "
                         "earthquake struck Delhi. Buildings were damaged.")
        stale = dict(fresh, published_at=_fresh_iso(30))
        base_rows = [dict(fresh)]
        self.assertGreater(
            x_reach_potential(fresh, cluster_rows=base_rows)["score"],
            x_reach_potential(stale, cluster_rows=base_rows)["score"])

    def test_multi_outlet_beats_single_outlet(self):
        a = _article("Quake strikes Delhi region",
                     "A strong earthquake struck Delhi.")
        one = x_reach_potential(a, cluster_rows=[a])
        rows = [a] + [dict(a, source=s) for s in ("NDTV", "TOI")]
        many = x_reach_potential(a, cluster_rows=rows)
        self.assertGreater(many["score"], one["score"])

    def test_saturation_lowers_score_and_is_explained(self):
        a = _article("Quake strikes Delhi region",
                     "A strong earthquake struck Delhi.")
        clean = x_reach_potential(a, cluster_rows=[a])
        saturated = x_reach_potential(a, cluster_rows=[a],
                                      posted_overlap=2)
        self.assertGreater(clean["score"], saturated["score"])
        self.assertEqual(saturated["saturation_penalty"], 30.0)
        self.assertTrue(any("already posted this topic" in r
                            for r in saturated["reasons"]))

    def test_premium_is_a_note_only_never_a_multiplier(self):
        a = _article("Quake strikes Delhi region",
                     "A strong earthquake struck Delhi.")
        unknown = x_reach_potential(a, cluster_rows=[a],
                                    account_premium="unknown")
        premium = x_reach_potential(a, cluster_rows=[a],
                                    account_premium="premium")
        self.assertEqual(unknown["score"], premium["score"])
        self.assertTrue(any("Premium" in r for r in premium["reasons"]))
        self.assertFalse(any("Premium" in r for r in unknown["reasons"]))


class TestShareability(unittest.TestCase):
    def test_concrete_record_story_beats_bland_monitoring_story(self):
        strong = shareability_score(
            "India posts record GDP growth of 8.2% in Q1",
            "The economy grew at its fastest pace in 15 years.")
        bland = shareability_score(
            "Officials said the situation is being monitored",
            "A statement was issued after a review meeting.")
        self.assertGreaterEqual(strong, 60.0)
        self.assertLess(bland, strong)

    def test_weak_intro_penalized(self):
        clean = shareability_score(
            "RBI cuts repo rate by 50 basis points",
            "The Monetary Policy Committee voted to cut the rate.")
        weak = shareability_score(
            "According to reports RBI cuts repo rate by 50 basis points",
            "The Monetary Policy Committee voted to cut the rate.")
        self.assertGreater(clean, weak)


class TestSaveValue(unittest.TestCase):
    def test_reference_facts_beat_event_narrative(self):
        rates = save_value_score(
            "RBI holds repo rate at 6.5% in June policy review",
            "The repo rate stays at 6.5%. New rules apply from July 1.")
        event = save_value_score(
            "Cyclone Remal kills 3 as it intensifies over Bengal coast",
            "Three villages were cut off by flooding.")
        self.assertGreaterEqual(rates, 60.0)
        self.assertLess(event, rates)

    def test_data_density_counts(self):
        dense = save_value_score(
            "New tax slabs announced: 5%, 10%, 15% from April",
            "The revised slabs apply from the new financial year.")
        sparse = save_value_score(
            "New tax slabs announced for the new financial year",
            "Officials said the details would be published later.")
        self.assertGreater(dense, sparse)


class TestSemanticClarity(unittest.TestCase):
    def test_entity_headline_beats_generic_headline(self):
        clear = semantic_clarity_score(
            "Cyclone Remal hits Bengal coast, 3 dead",
            "Cyclone Remal intensified over the Bay of Bengal.")
        vague = semantic_clarity_score(
            "Government announces new measures for the people",
            "Officials said more details would follow.")
        self.assertGreaterEqual(clear, 60.0)
        self.assertLess(vague, clear)


class TestSaturation(_Base):
    def test_own_recent_posts_penalize_similar_story_but_never_gate(self):
        """A story on a topic SAYOX already posted (inside the 48h
        saturation window, outside the 3h topic cooldown) still gets
        RECOMMENDED — XRP is advisory — but carries a saturation penalty
        and an honest reason. A different topic gets neither."""
        a1 = self._landslide(1)
        service.state(self.db)
        service.mark_posted(self.db, a1)
        # 20h ago: global cooldown (60m) and topic cooldown (180m) both
        # expired, still inside the 48h saturation window
        self._backdate_post(a1, 20)
        self._landslide(2)          # same topic, new story
        self._seed(
            "RBI holds repo rate at 6.5% in June policy review",
            "The Monetary Policy Committee voted to keep the repo rate "
            "unchanged at 6.5%. Inflation eased to 4.8%.",
            "https://example.com/rbi", coverage=True)
        s = service.state(self.db)
        # advisory: a similar story is still perfectly recommendable
        self.assertIsNotNone(s["current"])
        entries = [s["current"]] + s["queue"] + s["up_next"]
        landslide = [e for e in entries
                     if e and "landslide" in (e["headline"] or "").lower()]
        rbi = [e for e in entries
               if e and "repo rate" in (e["headline"] or "").lower()]
        self.assertTrue(landslide)
        self.assertTrue(rbi)
        for e in landslide:
            self.assertGreaterEqual(e["reach_potential"]
                                    ["saturation_penalty"], 15.0)
            self.assertTrue(any("already posted this topic" in r
                                for r in e["reach_potential"]["reasons"]))
        for e in rbi:
            self.assertEqual(e["reach_potential"]["saturation_penalty"], 0.0)

    def test_state_carries_reach_potential_with_honest_label(self):
        self._landslide(1)
        s = service.state(self.db)
        rp = s["current"]["reach_potential"]
        self.assertEqual(rp["label"],
                         "Reach potential — editorial estimate")
        self.assertNotIn("X algorithm", rp["label"])
        self.assertNotIn("impressions", rp["label"].lower())
        self.assertGreaterEqual(rp["score"], 0.0)
        self.assertLessEqual(rp["score"], 100.0)
        self.assertIsInstance(rp["reasons"], list)


class TestVariantSelection(_Base):
    """Story-conditioned variant selection: the story's own advisory
    signals favour a variant ROLE; information always pays; ties are
    deterministic."""

    def test_fact_floor(self):
        self.assertEqual(service._fact_floor(4), 2)
        self.assertEqual(service._fact_floor(5), 3)
        self.assertEqual(service._fact_floor(1), 1)
        self.assertEqual(service._fact_floor(0), 1)

    def test_shareable_story_favours_punchy(self):
        punchy = {"style": "punchy", "hook": 50, "facts": 2}
        briefing = {"style": "briefing", "hook": 50, "facts": 4}
        # shareability dominates -> punchy's role bonus wins
        self.assertGreater(
            service._variant_objective(punchy, 70, 30, 4),
            service._variant_objective(briefing, 70, 30, 4))
        # nothing dominant -> facts win, briefing keeps the story
        self.assertGreater(
            service._variant_objective(briefing, 30, 30, 4),
            service._variant_objective(punchy, 30, 30, 4))

    def test_save_heavy_story_favours_briefing(self):
        punchy = {"style": "punchy", "hook": 50, "facts": 2}
        briefing = {"style": "briefing", "hook": 50, "facts": 4}
        self.assertGreater(
            service._variant_objective(briefing, 30, 70, 4),
            service._variant_objective(punchy, 30, 70, 4))

    def test_single_fact_story_allows_flash(self):
        flash = {"style": "flash", "hook": 60, "facts": 1}
        briefing = {"style": "briefing", "hook": 60, "facts": 1}
        self.assertGreater(
            service._variant_objective(flash, 70, 30, 1),
            service._variant_objective(briefing, 70, 30, 1))

    def test_chosen_variant_is_one_of_the_options_and_deterministic(self):
        self._landslide(1)
        art = dict(self.db.article_by_id(self.db.query_one(
            "SELECT id FROM articles ORDER BY id LIMIT 1")["id"]))
        t1, tags1, opts1 = service._build_tweet(self.db, art)
        t2, tags2, opts2 = service._build_tweet(self.db, art)
        self.assertIsNotNone(t1)
        self.assertEqual(t1, t2)                      # deterministic
        self.assertEqual(tags1, tags2)
        self.assertIn(t1, [o["text"] for o in opts1])

    def test_anti_sensation_floor_blocks_fact_deletion(self):
        """If punchy is chosen for a multi-fact story, it must still
        carry at least half the story's facts — it can never win by
        deleting information."""
        self._seed(
            "Flood alert issued as river rises above danger mark",
            "The flood control department opened 45 relief camps on "
            "Tuesday. The meteorological office issued a red alert for "
            "the district. Rail services were suspended until Thursday "
            "morning. The district collector said 12,000 people had been "
            "relocated. Two national highway stretches remained closed "
            "overnight.",
            "https://example.com/flood", coverage=True)
        s = service.state(self.db)
        for entry in [s["current"]] + s["queue"]:
            if not entry:
                continue
            chosen = [o for o in entry["options"]
                      if o["text"] == entry["tweet"]]
            self.assertEqual(len(chosen), 1)
            chosen = chosen[0]
            max_facts = max(o["facts"] for o in entry["options"])
            if chosen["style"] == "punchy":
                self.assertGreaterEqual(
                    chosen["facts"], service._fact_floor(max_facts))


if __name__ == "__main__":
    unittest.main()
