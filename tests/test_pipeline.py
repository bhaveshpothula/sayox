"""Tests for normalization, dedup, clustering, scoring, tweets, db."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Database
from app.news.normalizer import clean_text, jaccard, normalize_url, tokens, url_hash
from app.news.deduplicator import find_similar_title
from app.news.clusterer import cluster_articles
from app.news.classifier import score_india_relevance
from app.news.importance import score_importance
from app.tweet.generator import (choose_label, clean_headline,
                                 contains_filler, context_sentences,
                                 effective_length, generate_tweet,
                                 has_valid_source_url, pick_hashtags,
                                 truncate_at_word)
from app.tweet.validator import check_rate_limits, validate_tweet
from app.news.collector import select_stories, _is_stale
from app.ai.budget import AIBudget
from app.news.rss import fetch_feed  # noqa: F401  (import check)


class TestNormalizer(unittest.TestCase):
    def test_normalize_url_strips_tracking(self):
        a = normalize_url("https://Example.com/news/story/?utm_source=x&id=5")
        b = normalize_url("http://example.com/news/story/?id=5")
        self.assertEqual(url_hash(a), url_hash(b))

    def test_clean_text(self):
        self.assertEqual(clean_text("<p>Hello &amp;   world</p>"), "Hello & world")

    def test_jaccard(self):
        self.assertGreater(jaccard(tokens("rbi announces repo rate cut"),
                                   tokens("RBI announces major repo rate cut!")), 0.7)


class TestDedup(unittest.TestCase):
    def test_similar_title_found(self):
        existing = ["RBI announces major repo rate decision",
                    "India wins cricket world cup final"]
        self.assertIsNotNone(find_similar_title(
            "RBI announces major repo rate decision today", existing, 0.75))
        self.assertIsNone(find_similar_title(
            "New semiconductor plant approved in Gujarat", existing, 0.75))


class TestClustering(unittest.TestCase):
    def test_cluster(self):
        arts = [
            {"id": 1, "title": "RBI announces repo rate cut of 25 bps"},
            {"id": 2, "title": "RBI announces repo rate cut, home loans cheaper"},
            {"id": 3, "title": "ISRO launches new navigation satellite"},
        ]
        clusters = cluster_articles(arts, 0.45)
        self.assertEqual(len(clusters), 2)
        merged = [c for c in clusters if len(c) == 2]
        self.assertEqual(merged, [[1, 2]])


class TestIndiaScore(unittest.TestCase):
    def test_india_high(self):
        s = score_india_relevance("RBI cuts repo rate, home loans cheaper",
                                  "Mumbai: The Reserve Bank of India...", "IN")
        self.assertGreaterEqual(s, 0.5)

    def test_global_low(self):
        s = score_india_relevance("Tesla stock jumps after earnings",
                                  "Wall Street rallied on Thursday", "GLOBAL")
        self.assertLess(s, 0.35)

    def test_global_india_mention(self):
        s = score_india_relevance("India wins cricket world cup",
                                  "Team India celebrated", "GLOBAL")
        self.assertGreaterEqual(s, 0.35)


class TestImportance(unittest.TestCase):
    def test_breaking_high(self):
        now = format_datetime(datetime.now(timezone.utc))
        s = score_importance("Breaking: RBI announces major repo rate cut",
                             "big decision", 0.95, now)
        self.assertGreaterEqual(s, 0.5)

    def test_opinion_low(self):
        s = score_importance("Opinion: the best recipes of the year",
                             "lifestyle", 0.8, None)
        self.assertLess(s, 0.45)


class TestTweetGen(unittest.TestCase):
    URL = "https://example.com/news/story"

    def _article(self, **kw):
        base = {"title": "RBI keeps repo rate unchanged at 6.5 percent",
                "summary": "The Monetary Policy Committee voted to hold rates. Markets were flat.",
                "source": "The Hindu", "normalized_url": self.URL,
                "url": self.URL}
        base.update(kw)
        return base

    def _tweet(self, **kw):
        imp = kw.pop("imp", 0.6)
        ind = kw.pop("ind", 0.8)
        return generate_tweet(self._article(**kw), ind, imp)

    # --- labels ---

    def test_normal_story_no_label(self):
        t = self._tweet()
        self.assertTrue(t.splitlines()[0].startswith("RBI keeps"))
        for banned in ("BREAKING", "Developing", "Update",
                       "DECISION", "ANNOUNCEMENT", "REPORT", "RESULT",
                       "LAUNCH", "DISCOVERY", "ALERT", "NEWS"):
            self.assertNotIn(banned + ":", t)

    def test_banned_labels_never_generated(self):
        for kw in ("DECISION", "ANNOUNCEMENT", "REPORT", "RESULT",
                   "LAUNCH", "DISCOVERY", "ALERT", "NEWS"):
            t = generate_tweet(self._article(
                title="Ministry %s on new policy framework announced today" % kw,
                summary="Officials provided details."), 0.8, 0.7)
            self.assertNotIn("%s:" % kw, t)

    def test_breaking_only_when_earned(self):
        # source says breaking + major event + high importance -> BREAKING
        t = self._tweet(title="Breaking: massive earthquake strikes Delhi, several killed",
                        summary="Rescue teams deployed.", imp=0.8)
        self.assertTrue(t.startswith("BREAKING: "))
        # high importance alone is NOT breaking
        t2 = self._tweet(title="Parliament passes new education bill",
                         summary="Lawmakers voted today.", imp=0.95)
        self.assertFalse(t2.startswith("BREAKING"))
        # "breaking" word without a major event is NOT breaking
        t3 = self._tweet(title="Breaking: fashion week opens in Mumbai",
                         summary="Designers showcased collections.", imp=0.8)
        self.assertFalse(t3.startswith("BREAKING"))

    def test_developing_only_for_unfolding(self):
        t = self._tweet(title="Rescue operations continue as flood waters rise",
                        summary="This is a developing story with live updates.")
        self.assertTrue(t.startswith("Developing: "))
        t2 = self._tweet()
        self.assertFalse(t2.startswith("Developing"))

    def test_update_only_for_followups(self):
        t = self._tweet(title="Update: RBI clarifies repo rate stance after market confusion",
                        summary="The central bank issued a statement.", imp=0.7)
        self.assertTrue(t.startswith("Update: "))
        t2 = self._tweet(title="RBI announces quarterly review schedule")
        self.assertFalse(t2.startswith("Update"))

    # --- flags / hashtags ---

    def test_no_indian_flag(self):
        t = self._tweet()
        self.assertNotIn("\U0001F1EE\U0001F1F3", t)  # 🇮🇳
        self.assertNotIn("\U0001F30D", t)             # 🌎

    def test_no_mandatory_side_hashtags(self):
        t = self._tweet()
        self.assertNotIn("#India", t)
        self.assertNotIn("#World", t)

    def test_hashtags_story_based(self):
        t = self._tweet()
        self.assertIn("#RBI", t)  # story is about RBI

    def test_hashtags_max_two(self):
        t = self._tweet(title="ISRO satellite launch for monsoon forecasting amid flood risk")
        tags = [w for w in t.split() if w.startswith("#")]
        self.assertLessEqual(len(tags), 2)

    def test_zero_hashtags_allowed(self):
        t = self._tweet(title="Local council approves new library hours",
                        summary="Members voted after debate.")
        tags = [w for w in t.split() if w.startswith("#")]
        self.assertEqual(len(tags), 0)

    def test_no_generic_hashtags(self):
        t = self._tweet(title="Major flood developments reported across the region")
        for tag in ("#News", "#BreakingNews", "#Update"):
            self.assertNotIn(tag, t)

    # --- URL policy: absent publicly, required internally ---

    def test_url_absent_from_public_tweet(self):
        for ind, imp in [(0.8, 0.9), (0.1, 0.5)]:
            t = self._tweet(ind=ind, imp=imp)
            self.assertIsNotNone(t)
            self.assertNotIn(self.URL, t)
            self.assertNotIn("http", t)

    def test_stored_url_required_internally(self):
        self.assertTrue(has_valid_source_url(self._article()))
        self.assertFalse(has_valid_source_url(
            self._article(normalized_url="", url="")))
        self.assertFalse(has_valid_source_url(
            self._article(normalized_url=None, url=None)))
        self.assertFalse(has_valid_source_url(
            self._article(normalized_url="not-a-url", url="not-a-url")))
        # no tweet is generated without an internal URL
        self.assertIsNone(self._tweet(normalized_url="", url=""))

    def test_source_name_present(self):
        t = self._tweet()
        self.assertIn("Source: The Hindu", t)

    # --- length ---

    def test_long_headline_fits_limit(self):
        t = self._tweet(title="RBI announces " + "very long " * 40)
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t), 280)

    def test_no_awkward_headline_truncation(self):
        """A hard cut must never end on a dangling word or bare number."""
        t = self._tweet(title="Cyclone batters coastal town as river rises "
                              "sharply: 2 dead after 200mm of rain in a "
                              "single day across the district and more "
                              "rainfall is forecast for the coming weekend "
                              "in several states",
                        summary="")
        self.assertIsNotNone(t)
        first_line = t.splitlines()[0]
        if first_line.rstrip("…").endswith(tuple(" .,;:-")):
            self.fail("dangling punctuation: %r" % first_line)
        last_word = first_line.rstrip("…").split()[-1].lower()
        self.assertNotIn(last_word, ("after", "of", "in", "on", "for", "to",
                                     "and", "the", "a", "an", "dies"))

    def test_truncate_at_word_strips_dangling(self):
        out = truncate_at_word("Cyclone leaves 2 dead after 200mm of rain", 24)
        self.assertTrue(out.endswith("…"))
        stripped = out.rstrip("…").split()[-1].lower()
        self.assertNotIn(stripped, ("after", "of", "in", "on", "for", "and"))
        self.assertFalse(stripped.isdigit())

    def test_truncate_at_word_short_input_untouched(self):
        self.assertEqual(truncate_at_word("Short headline here", 50),
                         "Short headline here")

    def test_ndtv_headline_no_awkward_truncation(self):
        """Regression: the exact NDTV headline that used to cut to
        '...Dies After 2' must now end on a clean word boundary."""
        title = ("Assaulted, Tied To Tree, Burned: Punjab Influencer "
                 "Dies After 2")
        out = truncate_at_word(title + " Days In Hospital", 40)
        stripped = out.rstrip("…").split()
        self.assertTrue(out.endswith("…") or out == title + " Days In Hospital")
        # never a dangling number/preposition like 'After 2' or 'Dies After'
        self.assertFalse(stripped[-1].isdigit())
        self.assertNotIn(stripped[-1].lower(),
                         ("after", "of", "in", "on", "for", "to", "and",
                          "the", "a", "an", "dies"))

    def test_ndtv_headline_repaired_from_summary(self):
        """Preferred output: the feed-truncated '...Dies After 2' is
        completed VERBATIM from the article's own summary and the
        decorative pre-colon clause is dropped."""
        t = self._tweet(title="Assaulted, Tied To Tree, Burned: Punjab "
                              "Influencer Dies After 2",
                        summary="The influencer died in hospital after a "
                                "2-month battle with severe burn injuries. "
                                "Police have registered a case.")
        first_line = t.splitlines()[0]
        self.assertIn("Punjab Influencer Dies After 2-month battle", first_line)
        self.assertNotIn("Assaulted, Tied To Tree, Burned:", first_line)
        # the completion is verbatim source text, never invented
        self.assertIn("2-month battle",
                      "died in hospital after a 2-month battle")
        # never a dangling fragment
        last = first_line.rstrip("…").split()[-1].lower()
        self.assertFalse(last.isdigit())
        self.assertNotIn(last, ("after", "of", "in", "on", "for", "to"))

    def test_ndtv_headline_production_path(self):
        """The EXACT production data flow (main.py passes cluster sibling
        texts): the NDTV description lacks the timeframe — common for NDTV
        feeds, where description == truncated title — so the repair must
        recover '2-month battle' from the same-story cluster siblings."""
        article = self._article(
            title="Assaulted, Tied To Tree, Burned: Punjab Influencer "
                  "Dies After 2",
            # NDTV-style description: no timeframe phrase at all
            summary="Assaulted, Tied To Tree, Burned: Punjab Influencer "
                    "Dies After 2")
        cluster_texts = [
            "Punjab influencer, 26, dies after 2-month battle in hospital "
            "The young content creator had suffered severe burn injuries.",
            "Police register case after influencer's death in Punjab",
        ]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=cluster_texts)
        self.assertIsNotNone(t)
        first_line = t.splitlines()[0]
        self.assertEqual(first_line, "Punjab Influencer Dies After 2-month battle")
        self.assertNotIn("Assaulted", first_line)
        self.assertNotIn("http", t)
        self.assertIn("Source: The Hindu", t)
        self.assertLessEqual(len(t), 280)

    def test_repair_never_fabricates(self):
        """No timeframe anywhere (summary or siblings) — the dangling
        fragment is dropped, nothing invented."""
        article = self._article(
            title="Assaulted, Tied To Tree, Burned: Punjab Influencer "
                  "Dies After 2",
            summary="Police have registered a case and are investigating.")
        t = generate_tweet(article, 0.8, 0.9,
                           cluster_texts=["Police register case after "
                                          "influencer's death in Punjab"])
        first_line = t.splitlines()[0]
        self.assertFalse(first_line.rstrip("…").endswith("After 2"))
        self.assertNotIn("battle", first_line.lower())

    def test_ndtv_pretruncated_title_never_dangles(self):
        """The EXACT dry-run bug: the feed itself delivers a pre-truncated
        title ending '...Dies After 2'. The production generate_tweet path
        must never publish that dangling fragment."""
        t = self._tweet(title="Assaulted, Tied To Tree, Burned: Punjab "
                              "Influencer Dies After 2",
                        summary="Police have registered a case and are "
                                "investigating the attack.")
        self.assertIsNotNone(t)
        first_line = t.splitlines()[0]
        self.assertFalse(first_line.rstrip("… ").endswith("After 2"),
                         "headline still ends with 'After 2': %r" % first_line)
        last = first_line.rstrip("…").split()[-1]
        self.assertFalse(last.isdigit())
        self.assertNotIn(last.lower(),
                         ("after", "of", "in", "on", "for", "to", "and",
                          "the", "a", "an"))

    def test_ndtv_full_headline_shortened_naturally(self):
        """The full NDTV headline, when too long, is shortened by dropping
        the pre-colon clause — preserving the core news."""
        t = self._tweet(title="Assaulted, Tied To Tree, Burned: Punjab "
                              "Influencer Dies After 2 Month Battle",
                        summary="")
        first_line = t.splitlines()[0]
        self.assertIn("Punjab Influencer Dies After 2 Month Battle",
                      first_line)
        self.assertFalse(first_line.rstrip("… ").endswith("After 2"))

    def test_full_tweet_shrink_prefers_post_colon_clause(self):
        """Even forced below the natural length, the headline never ends
        on a dangling fragment."""
        t = self._tweet(
            title=("Assaulted, Tied To Tree, Burned: Punjab Influencer "
                   "Dies After 2 Days In Hospital As Police Probe The "
                   "Attack That Shocked The State And Sparked Outrage "
                   "Across Several Cities Nationwide Today"),
            summary="")
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t), 280)
        first_line = t.splitlines()[0]
        last = first_line.rstrip("…").split()[-1].lower()
        self.assertFalse(first_line.rstrip("…").split()[-1].isdigit())
        self.assertNotIn(last, ("after", "of", "in", "on", "for", "to",
                                "and", "the", "a", "an"))

    def test_long_context_reduced_to_fit(self):
        t = self._tweet(title="RBI policy",
                        summary=("Sentence one that is fairly long here. " * 12))
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t), 280)

    def test_context_sentences_included(self):
        t = self._tweet()
        lines = [l for l in t.splitlines() if l.strip()]
        # headline + context paragraph + Source line
        self.assertGreaterEqual(len(lines), 3)
        self.assertIn("The Monetary Policy Committee voted to hold rates.", t)
        self.assertIn("Markets were flat.", t)

    def test_weak_story_not_padded(self):
        """A summary-less story gets headline + source only — no filler."""
        t = self._tweet(summary="")
        self.assertIn("Source: The Hindu", t)
        self.assertNotIn("Stay tuned", t)
        self.assertNotIn("More details", t)
        self.assertNotIn("\n\n\n", t)

    # --- bullet-point format ---

    def _bullets(self, t):
        return [l[2:].strip() for l in t.splitlines()
                if l.startswith("• ")]

    def test_three_point_tweet(self):
        t = self._tweet(summary=(
            "The Monetary Policy Committee voted to hold the repo rate. "
            "The decision was unanimous after a long debate today. "
            "Governor said inflation remains within the target band."))
        bullets = self._bullets(t)
        self.assertEqual(len(bullets), 3)
        self.assertTrue(t.splitlines()[0].startswith("RBI keeps"))
        self.assertIn("Source: The Hindu", t)

    def test_four_point_tweet(self):
        t = self._tweet(summary=(
            "The MPC voted to hold the repo rate steady. "
            "The decision was unanimous after a long debate. "
            "Inflation remains within the target band now. "
            "Markets were flat after the announcement."))
        self.assertEqual(len(self._bullets(t)), 4)
        for b in self._bullets(t):
            self.assertFalse(b.endswith("…"))

    def test_five_point_tweet(self):
        t = self._tweet(summary=(
            "The MPC voted to hold the repo rate. "
            "The decision was unanimous today. "
            "Inflation remains within the target band. "
            "Markets were flat after the news. "
            "Next policy meeting is in October."))
        self.assertEqual(len(self._bullets(t)), 5)
        for b in self._bullets(t):
            self.assertFalse(b.endswith("…"))
        self.assertLessEqual(len(t), 280)

    def test_point_count_never_exceeds_five(self):
        t = self._tweet(summary=" ".join(
            "Detail sentence number %d was published by officials today."
            % i for i in range(1, 9)))
        self.assertLessEqual(len(self._bullets(t)), 5)

    def test_format_headline_points_source(self):
        t = self._tweet()
        lines = [l for l in t.splitlines() if l.strip()]
        self.assertTrue(lines[0].startswith("RBI keeps"))          # headline
        self.assertTrue(all(l.startswith("• ") for l in
                            lines[1:len(self._bullets(t)) + 1]))   # points
        self.assertIn("Source: The Hindu", lines)                  # source
        self.assertNotIn("http", t)                                # no URL

    def test_no_filler_points(self):
        t = self._tweet(summary=(
            "The Monetary Policy Committee voted to hold the repo rate. "
            "More details are awaited. This comes amid other news. "
            "The situation is developing."))
        bullets = self._bullets(t)
        self.assertEqual(len(bullets), 1)
        for filler in ("More details are awaited", "This comes amid",
                       "situation is developing"):
            self.assertNotIn(filler, t)

    def test_thin_source_not_padded(self):
        """One-sentence summary -> exactly one point; nothing invented."""
        t = self._tweet(summary="Markets were flat after the announcement.")
        self.assertEqual(len(self._bullets(t)), 1)
        for filler in ("Stay tuned", "More details", "Watch this space"):
            self.assertNotIn(filler, t)

    def test_points_are_source_verbatim(self):
        summary = ("The Monetary Policy Committee voted to hold rates. "
                   "Markets were flat.")
        t = self._tweet(summary=summary)
        for b in self._bullets(t):
            self.assertIn(b.rstrip("…"), summary)

    def test_long_point_not_arbitrarily_capped(self):
        """No arbitrary per-point cap: a 137-char source sentence is kept
        COMPLETE and unmodified (verbatim, terminal period included)
        whenever it fits the actual remaining tweet budget."""
        long_point = ("The Monetary Policy Committee voted to keep the "
                      "repo rate unchanged at six point five percent for "
                      "the eighth consecutive meeting held in Mumbai today")
        t = self._tweet(summary=long_point + ". Markets were flat.")
        bullets = self._bullets(t)
        kept = [b for b in bullets if b.startswith(long_point)]
        self.assertEqual(len(kept), 1,
                         "long sentence mangled or dropped: %r" % bullets)
        # kept verbatim — nothing between the sentence and its period
        self.assertEqual(kept[0], long_point + ".")

    def test_unfittable_sentence_dropped_whole(self):
        """NO per-point cap: a source sentence that cannot fit COMPLETE
        in the remaining budget is dropped entirely — never truncated,
        never ellipsized."""
        huge = ("The committee " + "very detailed deliberation " * 20 +
                "concluded the review")
        t = self._tweet(summary=huge + ". Markets were flat today.")
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t), 280)
        bullets = self._bullets(t)
        for b in bullets:
            self.assertFalse(b.endswith("…"))
            self.assertNotIn("very detailed deliberation", b)
        # the short complete point still made it
        self.assertTrue(any("Markets were flat" in b for b in bullets))

    def test_complete_tweet_never_ellipsized_point(self):
        """No bullet anywhere in the tweet may end with '…' or a dangling
        preposition/conjunction/article."""
        summaries = [
            "Officials confirmed the first detail of the decision today. "
            "A second important detail about the case emerged later. "
            "Investigations are continuing into the full matter now.",
            "One long sentence describing the entire situation in great "
            "detail with many facts and figures included here.",
            "",
        ]
        for s in summaries:
            t = self._tweet(summary=s)
            self.assertIsNotNone(t)
            self.assertLessEqual(len(t), 280)
            for b in self._bullets(t):
                self.assertFalse(b.endswith("…"), b)
                last = b.rstrip("…").split()[-1].lower()
                self.assertNotIn(last, ("after", "of", "in", "on", "for",
                                        "to", "and", "the", "a", "an",
                                        "with", "by", "from"))

    # --- dry-run bug regressions ---

    def test_no_ellipsis_anywhere_in_tweet(self):
        """Zero '…' and zero '...' in the final public tweet — including
        ellipses present in the raw source text."""
        t = self._tweet(title="RBI cuts repo rate… sharply today",
                        summary="The decision came after a long "
                                "meeting… Officials said more measures "
                                "are possible.")
        self.assertIsNotNone(t)
        self.assertNotIn("…", t)
        self.assertNotIn("...", t)

    def test_leading_ellipsis_fragment_rejected(self):
        """'... Short-term visa stay and things such as the Palm scheme'
        must NEVER appear: a fragment the SOURCE begins with an ellipsis
        (or slash-continuation) is dropped ENTIRELY — the remainder is
        never published as a bullet."""
        cases = [
            ("... Short-term visa stay and things such as the Palm "
             "scheme.", "Short-term visa stay"),
            ("… short-term visa stay and work rights.", "short-term visa stay"),
            (".../continued coverage of the visa changes.", "continued coverage"),
        ]
        for fragment, banned in cases:
            t = self._tweet(summary=fragment + " The minister announced "
                                              "the changes officially.")
            bullets = self._bullets(t)
            # the good complete sentence survived verbatim
            self.assertTrue(any("The minister announced" in b for b in bullets),
                            "good point lost: %r" % bullets)
            # the fragment's remainder is COMPLETELY absent
            self.assertNotIn(banned, t)

    def test_split_sentences_keeps_ellipsis_attached(self):
        """Production-path regression: the '.' inside a leading '...' must
        NEVER act as a sentence boundary — otherwise the ellipsis is
        consumed by splitting and the fragment's remainder comes back as
        a clean-looking 'complete' sentence. The marker must stay
        attached to the fragment so build_points can reject it whole."""
        from app.tweet.generator import (_ELLIPSIS_TOKEN,
                                         _split_sentences, build_points)
        s = "... Short-term visa stay and things such as the Palm scheme."
        parts = _split_sentences(s)
        # the fragment is still ONE piece, still marked as a continuation
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].startswith(_ELLIPSIS_TOKEN), parts)
        # and build_points drops it completely
        self.assertEqual(build_points(s), [])
        self.assertEqual(build_points(s + " Ministers announced the "
                                          "changes officially."),
                         ["Ministers announced the changes officially."])

    def test_incomplete_fragment_rejected(self):
        """A source fragment without terminal punctuation is not a
        complete thought — dropped, never published as a bullet."""
        t = self._tweet(summary="Talks between the two sides over the "
                                "border issue. More meetings expected "
                                "soon. A fragment without any ending")
        for b in self._bullets(t):
            self.assertTrue(b.endswith((".", "!", "?")), b)
            self.assertNotIn("A fragment without any ending", b)

    def test_headline_never_repeated_as_bullet_label(self):
        """Nepal-style bug: a summary sentence repeating the story label
        from the headline ('Nepal Flash Flood LIVE Updates: ...') must
        not be published as a bullet restating the headline."""
        headline = ("Nepal Flash Flood LIVE Updates: 734 Bodies Recovered, "
                    "275 Indians Among Nearly 2,500 Still Missing")
        t = self._tweet(
            title=headline,
            summary="Nepal Flash Flood LIVE Updates: A glacier collapse "
                    "led to a sudden massive flood in the Bhote Koshi "
                    "River. Rescue teams recovered more bodies today.")
        for b in self._bullets(t):
            # the label must not survive as the start of a bullet
            self.assertFalse(b.startswith("Nepal Flash Flood LIVE"),
                             b)
            # and the bullet must add information beyond the headline
            hwords = set(w for w in headline.lower().split()
                         if len(w) > 3)
            bwords = set(w for w in b.lower().split() if len(w) > 3)
            if bwords:
                self.assertLess(len(bwords & hwords) / len(bwords), 0.6, b)
        # the complementary fact survived instead
        self.assertTrue(any("glacier collapse" in b for b in
                            self._bullets(t)))

    def test_hashtags_never_steal_news_space(self):
        """Hashtags are dropped rather than forcing a news point out."""
        # long-ish summary about RBI (which earns #RBI) near the limit
        summary = ("The Monetary Policy Committee voted to hold the "
                   "repo rate at its current level after reviewing "
                   "inflation data. Governor said inflation remains "
                   "within the tolerance band of the framework today. "
                   "The next policy meeting is scheduled for October "
                   "and will review the stance again then carefully.")
        t = self._tweet(summary=summary)
        self.assertLessEqual(len(t), 280)
        # news content was not sacrificed: at least two points survive
        self.assertGreaterEqual(len(self._bullets(t)), 2)

    def test_lead_markers_stripped_from_points(self):
        t = self._tweet(summary=(
            "However, the Monetary Policy Committee voted to hold rates. "
            "Meanwhile, markets were flat after the announcement."))
        bullets = self._bullets(t)
        for b in bullets:
            self.assertFalse(b.lower().startswith(("however", "meanwhile")))

    def test_points_tightened_not_dropped(self):
        """Completeness over character maximization: when points overflow
        280, points are DROPPED whole — none is ever truncated with '…'."""
        summary = " ".join(
            "Officials confirmed detail number %d of the decision today."
            % i for i in range(1, 6))
        t = self._tweet(summary=summary)
        self.assertLessEqual(len(t), 280)
        bullets = self._bullets(t)
        self.assertGreaterEqual(len(bullets), 1)
        self.assertLess(len(bullets), 5)   # some had to give
        for b in bullets:
            self.assertFalse(b.endswith("…"), "truncated point: %r" % b)
        # every surviving bullet is a complete verbatim source sentence
        for b in bullets:
            self.assertIn(b, summary)

    def test_headline_never_repeated_as_point(self):
        t = self._tweet(summary="RBI keeps repo rate unchanged at 6.5 "
                                "percent. Markets were flat.")
        for b in self._bullets(t):
            toks = set(w for w in b.lower().split() if len(w) > 3)
            headline = t.splitlines()[0]
            htoks = set(w for w in headline.lower().split() if len(w) > 3)
            if toks:
                self.assertLess(len(toks & htoks) / len(toks), 0.8,
                                "%r repeats the headline" % b)

    def test_malformed_title_facts_kept_as_point(self):
        """The pre-colon facts of a truncated title are not lost — they
        become the first bullet."""
        t = self._tweet(title="Assaulted, Tied To Tree, Burned: Punjab "
                              "Influencer Dies In Hospital After 2",
                        summary="Police are conducting raids to trace and "
                                "arrest the two accused.",
                        imp=0.9)
        first_line = t.splitlines()[0]
        bullets = self._bullets(t)
        self.assertIn("Punjab Influencer Dies", first_line)
        self.assertGreaterEqual(len(bullets), 2)
        self.assertTrue(any("Assaulted" in b for b in bullets),
                        "pre-colon facts lost: %r" % bullets)

    def test_sibling_coverage_fills_thin_story(self):
        """A one-fact article is topped up with same-story sibling facts
        (other outlets' verbatim coverage), never invented content."""
        article = self._article(
            title="Assaulted, Tied To Tree, Burned: Punjab Influencer "
                  "Dies After 2",
            summary="Police are conducting raids to trace and arrest the "
                    "two accused.")
        siblings = [
            "Punjab influencer dies after 2-month battle in hospital. "
            "The 26-year-old content creator had severe burn injuries.",
            "Police register case against two accused in the attack.",
        ]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=siblings)
        bullets = self._bullets(t)
        self.assertGreaterEqual(len(bullets), 3)
        joined = " ".join(bullets)
        # every bullet is grounded in title/summary/siblings — verbatim
        grounded = (article["summary"] + " " +
                    " ".join(siblings) + " " + article["title"])
        for b in bullets:
            self.assertIn(b.rstrip("…").rstrip("."), grounded,
                          "invented point: %r" % b)

    # --- content richness via cluster siblings ---

    def _thin_article(self, summary):
        return self._article(
            title="Cyclone Remal weakens after landfall",
            summary=summary)

    _SIB_FACTS = [
        "The cyclone hit the coast late last night.",
        "Winds of 110 kmph were recorded.",
        "Response teams were deployed today.",
        "Train services remained suspended.",
        "Fishermen were advised to stay ashore.",
    ]

    def test_three_facts_via_siblings(self):
        article = self._thin_article("One person died in the district "
                                     "today.")
        siblings = [" ".join(self._SIB_FACTS[:2])]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=siblings)
        self.assertEqual(len(self._bullets(t)), 3)
        self.assertLessEqual(len(t), 280)

    def test_four_facts_via_siblings(self):
        article = self._thin_article("One person died in the district "
                                     "today.")
        siblings = [" ".join(self._SIB_FACTS[:3])]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=siblings)
        self.assertEqual(len(self._bullets(t)), 4)
        self.assertLessEqual(len(t), 280)

    def test_five_facts_via_siblings(self):
        article = self._thin_article("One person died in the district "
                                     "today.")
        siblings = [" ".join(self._SIB_FACTS[:4])]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=siblings)
        self.assertEqual(len(self._bullets(t)), 5)
        self.assertLessEqual(len(t), 280)

    def test_thin_cluster_keeps_fewer_bullets(self):
        """Only 2 independent facts across the whole cluster — publish 2,
        never fabricate a third."""
        article = self._thin_article("One person died in the district "
                                     "today.")
        siblings = ["The storm brought heavy rain to the region."]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=siblings)
        self.assertEqual(len(self._bullets(t)), 2)
        for b in self._bullets(t):
            self.assertNotIn("developing", b.lower())
            self.assertNotIn("stay tuned", b.lower())

    def test_no_duplicate_bullets(self):
        article = self._thin_article("One person died in the district "
                                     "today.")
        siblings = ["One person died in the district today. "
                    "The storm brought heavy rain to the region."]
        t = generate_tweet(article, 0.8, 0.9, cluster_texts=siblings)
        bullets = self._bullets(t)
        self.assertEqual(len(bullets), len(set(bullets)))
        self.assertEqual(len(bullets), 2)

    def test_developing_label_selective(self):
        """DEVELOPING only when the source says the event is actively
        unfolding — not for ordinary high-importance news."""
        t = self._tweet(title="Cyclone Remal weakens after landfall",
                        summary="One person died in the district today.",
                        imp=0.95)
        self.assertFalse(t.startswith("Developing"))
        t2 = self._tweet(title="Rescue operations continue as flood "
                               "waters rise",
                         summary="This is a developing story with live "
                                 "updates.", imp=0.7)
        self.assertTrue(t2.startswith("Developing: "))

    def test_live_updates_label_dropped_from_headline(self):
        """'Nepal Flash Flood LIVE Updates: 734 Bodies Recovered…' — the
        decorative live-blog label is dropped and the main development
        becomes the headline; no bullet repeats the headline."""
        title = ("Nepal Flash Flood LIVE Updates: 734 Bodies Recovered, "
                 "275 Indians Among Nearly 2,500 Still Missing")
        t = self._tweet(
            title=title,
            summary="Nepal Flash Flood LIVE Updates: A glacier collapse "
                    "led to a sudden massive flood in the Bhote Koshi "
                    "River. Rescue teams are continuing operations "
                    "across the affected areas.")
        first_line = t.splitlines()[0]
        # the optional existing DEVELOPING label may prefix the headline,
        # but the live-blog label itself must be gone
        self.assertIn("734 Bodies Recovered", first_line)
        self.assertNotIn("LIVE Updates", first_line)
        for b in self._bullets(t):
            self.assertNotIn("734 Bodies Recovered", b)
            self.assertNotIn("LIVE Updates", b)
            self.assertTrue(b.endswith((".", "!", "?")), b)
        self.assertGreaterEqual(len(self._bullets(t)), 2)
        self.assertLessEqual(len(t), 280)

    # --- editorial quality: realistic news stories ---

    def _assert_editorial(self, t):
        """Shared editorial checks: complete punctuated bullets, no
        ellipsis, no truncation, no headline duplication, <=280."""
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t), 280)
        self.assertNotIn("…", t)
        self.assertNotIn("...", t)
        headline = t.splitlines()[0]
        hwords = set(w for w in headline.lower().split() if len(w) > 3)
        for b in self._bullets(t):
            self.assertTrue(b.endswith((".", "!", "?")), b)
            bwords = set(w for w in b.lower().split() if len(w) > 3)
            if bwords:
                # each bullet adds information beyond the headline
                self.assertLess(len(bwords & hwords) / len(bwords), 0.6,
                                "bullet restates headline: %r" % b)
        bullets = self._bullets(t)
        self.assertEqual(len(bullets), len(set(bullets)))
        return bullets

    def test_realistic_3_point_briefing(self):
        t = self._tweet(
            title="Cyclone Remal weakens after landfall on the Bengal "
                  "coast",
            summary=("The cyclone made landfall between Sagar Island "
                     "and Khepupara with winds of 110 kmph. One person "
                     "died in the coastal district of South 24 "
                     "Parganas. NDRF teams were deployed across the "
                     "region. This is a developing story."))
        bullets = self._assert_editorial(t)
        self.assertEqual(len(bullets), 3)
        self.assertTrue(any("110 kmph" in b for b in bullets))
        self.assertTrue(any("NDRF" in b for b in bullets))

    def test_realistic_4_point_briefing(self):
        t = self._tweet(
            title="Cyclone Remal weakens after landfall on the coast",
            summary=("The cyclone made landfall with winds of 110 kmph "
                     "near Sagar Island. One person died in the South "
                     "24 Parganas district. NDRF teams were deployed "
                     "across the region. Train services were suspended "
                     "until further notice."))
        bullets = self._assert_editorial(t)
        self.assertEqual(len(bullets), 4)

    def test_realistic_5_point_briefing(self):
        t = self._tweet(
            title="Cyclone Remal hits the coast",
            summary=("The cyclone made landfall with winds of 110 kmph. "
                     "One person died in the district. NDRF teams were "
                     "deployed. Train services were suspended. "
                     "Fishermen were advised to stay ashore."))
        bullets = self._assert_editorial(t)
        self.assertEqual(len(bullets), 5)

    def test_final_bullet_is_latest_development(self):
        """The latest development survives selection — the briefing ends
        on the newest fact, not a random early sentence."""
        t = self._tweet(
            title="Cyclone Remal weakens after landfall on the coast",
            summary=("The cyclone made landfall with winds of 110 kmph "
                     "near Sagar Island. One person died in the coastal "
                     "district. NDRF teams were deployed across the "
                     "region. Train services were suspended until "
                     "further notice."))
        bullets = self._assert_editorial(t)
        self.assertEqual(bullets[-1], "Train services were suspended "
                                      "until further notice.")

    def test_no_filler_bullets_realistic(self):
        t = self._tweet(
            title="Cyclone Remal weakens after landfall on the coast",
            summary=("The cyclone made landfall with winds of 110 kmph. "
                     "More details are awaited. Authorities are "
                     "monitoring the situation. The situation remains "
                     "developing. One person died in the district."))
        bullets = self._assert_editorial(t)
        for b in bullets:
            for filler in ("More details are awaited",
                           "monitoring the situation",
                           "remains developing"):
                self.assertNotIn(filler, b)
        # only the two real facts remain
        self.assertEqual(len(bullets), 2)

    # --- editorial principle: strength is judged by information value,
    # not by the presence of numbers, names or fixed action verbs ---

    def test_consequence_without_numbers_is_strong(self):
        """A significant consequence with no number or named entity is a
        strong news point: it must earn a 4th bullet slot like any
        statistic would."""
        t = self._tweet(
            title="Bridge collapses into swollen river in Arunachal",
            summary=("The bridge gave way on Monday evening. Three "
                     "vehicles fell into the river. Rescue teams "
                     "reached the site at dawn. The collapse cut off "
                     "several villages from the main highway."))
        bullets = self._assert_editorial(t)
        self.assertEqual(len(bullets), 4)
        self.assertTrue(any("cut off" in b for b in bullets))

    def test_response_and_status_change_are_strong(self):
        """Responses and status changes count as strong points even
        without figures — 'aid was restored' is news."""
        t = self._tweet(
            title="Flood fury subsides in Assam districts",
            summary=("Water levels receded across the two districts "
                     "on Tuesday. Road connectivity was restored to "
                     "the worst-hit areas. Relief camps were set up "
                     "for the displaced families. Power supply was "
                     "resumed late in the evening."))
        bullets = self._assert_editorial(t)
        self.assertEqual(len(bullets), 4)
        self.assertTrue(any("restored" in b for b in bullets))

    def test_numbers_still_rank_first(self):
        """Signals order the bullets; they don't gate them — concrete
        figures lead the briefing, softer context follows."""
        t = self._tweet(
            title="Monsoon wreaks havoc across the western coast",
            summary=("Schools were shut in the affected areas. At "
                     "least 12 people died in the overnight "
                     "landslides. Train services remained disrupted "
                     "through the region."))
        bullets = self._assert_editorial(t)
        self.assertIn("12", bullets[0])

    def test_breaking_allowed_when_genuinely_urgent(self):
        t = self._tweet(title="BREAKING: massive earthquake strikes Delhi, "
                              "several buildings collapse",
                        summary="Just in: rescue teams deployed to the "
                                "region. Officials confirmed casualties.",
                        imp=0.85)
        self.assertTrue(t.startswith("BREAKING: "))
        self.assertGreaterEqual(len(self._bullets(t)), 1)

    def test_high_importance_normal_story_no_breaking(self):
        for title in ("Parliament passes new tax bill after debate",
                      "Markets rally on strong quarterly earnings",
                      "New metro line opens to commuters this week"):
            t = self._tweet(title=title, imp=0.95)
            self.assertFalse(t.startswith("BREAKING"))

    # --- wording integrity ---

    def test_allegation_preserved(self):
        t = self._tweet(title="MP allegedly linked to bribery case",
                        summary="Police reportedly questioned the leader.")
        self.assertIn("allegedly", t)
        self.assertIn("reportedly", t)

    def test_no_fabricated_facts(self):
        t = self._tweet()
        self.assertIn("repo rate", t.lower())
        self.assertIn("monetary policy committee", t.lower())
        self.assertFalse(contains_filler(t))

    def test_no_bot_filler(self):
        self.assertFalse(contains_filler("RBI cuts rates today"))
        self.assertTrue(contains_filler("RBI cuts rates. Stay tuned!"))

    def test_no_blank_summary(self):
        t = self._tweet(summary="")
        self.assertIsNotNone(t)
        self.assertNotIn("\n\n\n", t)

    def test_headline_prefix_cleaned(self):
        self.assertEqual(clean_headline("BREAKING: RBI cuts rates"), "RBI cuts rates")
        t = self._tweet(title="Just In: RBI cuts repo rate by 25 bps", imp=0.5)
        self.assertFalse(t.startswith("Just In:"))

    def test_initials_do_not_break_context(self):
        """Regression: 'D.K.' must not split the context sentence."""
        s = ("Chief Minister D.K. Shivakumar made the announcement in "
             "Hubballi. Later he left.")
        result = context_sentences(s, max_chars=300)
        self.assertTrue(result.startswith(
            "Chief Minister D.K. Shivakumar made the announcement in Hubballi."))
        self.assertFalse(result.endswith("Chief Minister D.K."))

    def test_context_caps_total_length(self):
        s = ("One fairly long opening sentence about policy. "
             "A second sentence adding detail. A third sentence that "
             "should not fit within the cap.")
        result = context_sentences(s, max_chars=60)
        self.assertLessEqual(len(result), 60)

    def test_trailing_initials_summary_kept_whole(self):
        s = "The announcement was made by Chief Minister D.K."
        self.assertEqual(context_sentences(s), s)

    def test_common_abbreviations_do_not_split(self):
        for abbrev in ("Dr.", "Mr.", "vs.", "U.S.", "A.K.", "e.g."):
            s = "The talks between %s Rao and officials continued today. Then it ended." % abbrev
            result = context_sentences(s, max_chars=200)
            self.assertTrue(result.startswith("The talks between %s Rao" % abbrev),
                            "split too early at %r -> %r" % (abbrev, result))

    def test_normal_capitalized_words_still_split(self):
        s = "The launch was handled by ISRO. The mission succeeded."
        self.assertEqual(context_sentences(s, max_chars=200),
                         "The launch was handled by ISRO. The mission succeeded.")

    def test_choose_label_majority_none(self):
        labels = [choose_label(
            "Story %d about routine policy meeting held in Delhi" % i,
            "Officials met today.", 0.55) for i in range(10)]
        self.assertEqual(labels, [None] * 10)

    def test_breaking_not_on_every_candidate(self):
        """A batch of normal high-importance stories — none get BREAKING."""
        stories = [
            ("Parliament passes new tax bill after debate", "Lawmakers voted."),
            ("Markets rally on strong quarterly earnings", "Indices rose 2%."),
            ("New metro line opens to commuters", "Services began Monday."),
            ("Supreme Court reserves verdict on appeal", "Hearing concluded."),
        ]
        for title, summary in stories:
            t = generate_tweet(self._article(title=title, summary=summary),
                               0.8, 0.9)
            self.assertFalse(t.startswith("BREAKING"),
                             "%r must not be BREAKING" % title)

    def test_genuinely_breaking_gets_label(self):
        """Source-declared major breaking event with high importance does."""
        t = self._tweet(title="BREAKING: massive earthquake strikes Delhi, "
                              "several buildings collapse",
                        summary="Just in: rescue teams deployed.", imp=0.85)
        self.assertTrue(t.startswith("BREAKING: "))

    def test_no_fabricated_breaking(self):
        """High importance alone, or 'breaking' on trivial news — no label."""
        # no source wording at all
        t = self._tweet(title="RBI releases annual report on payments",
                        summary="The report covers trends.", imp=0.95)
        self.assertFalse(t.startswith("BREAKING"))
        # source says breaking but the event is not major
        t2 = self._tweet(title="Breaking: fashion week opens in Mumbai",
                         summary="Designers showcased collections.", imp=0.9)
        self.assertFalse(t2.startswith("BREAKING"))
        # major event but no source urgency wording
        t3 = self._tweet(title="Earthquake struck the region last week, "
                               "report says",
                         summary="A study reviewed the damage.", imp=0.95)
        self.assertFalse(t3.startswith("BREAKING"))

    def test_urgency_words_never_trigger_breaking(self):
        """'live'/'latest'/'updates' in the headline never mean BREAKING."""
        for title in ("LIVE: parliament budget session latest updates",
                      "Latest updates on the monsoon session of parliament",
                      "Earthquake zone live updates and coverage today"):
            t = self._tweet(title=title, summary="Coverage continues.", imp=0.9)
            self.assertFalse(t.startswith("BREAKING"),
                             "%r must not be BREAKING" % title)

    def test_breaking_word_in_summary_only_not_in_headline(self):
        """A stray 'breaking' in the summary never labels the tweet."""
        t = self._tweet(title="Anniversary of the earthquake marked by city",
                        summary="Officials recalled the breaking coverage "
                                "from that day.", imp=0.9)
        self.assertFalse(t.startswith("BREAKING"))


class TestValidator(unittest.TestCase):
    def test_limits(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            ok, _ = check_rate_limits(db, 5, 50)
            self.assertTrue(ok)
            db.insert_tweet(1, "x", "deterministic", "posted", tweet_id="1")
            db.insert_tweet(2, "x", "deterministic", "posted", tweet_id="2")
            # daily limit of 2 now reached
            ok, reason = check_rate_limits(db, 5, 2)
            self.assertFalse(ok)
            self.assertEqual(reason, "daily_limit")

    def test_url_in_public_tweet_rejected(self):
        text = "Some valid tweet text here https://example.com/a"
        ok, reason = validate_tweet(text, 280)
        self.assertFalse(ok)
        self.assertEqual(reason, "url_in_public_tweet")
        ok, reason = validate_tweet("A clean tweet without any link at all", 280)
        self.assertTrue(ok, reason)

    def test_hashtag_cap(self):
        ok, reason = validate_tweet("text #a #b #c here", 280)
        self.assertFalse(ok)
        self.assertEqual(reason, "hashtag_spam")


class TestDatabase(unittest.TestCase):
    def test_insert_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            a = {"url": "https://x.com/1", "normalized_url": "https://x.com/1",
                 "url_hash": "h1", "title": "t", "source": "s"}
            self.assertIsNotNone(db.insert_article(a))
            self.assertIsNone(db.insert_article(a))
            db.record_ai_usage("p", "m", 10, "test")
            self.assertEqual(db.ai_tokens_today(), 10)


class TestAIBudget(unittest.TestCase):
    def test_budget(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            b = AIBudget(db, daily_limit=20)
            b.consume(15, "test")
            self.assertEqual(b.remaining(), 5)
            with self.assertRaises(Exception):
                b.consume(10, "test")


class TestSelection5050(unittest.TestCase):
    def _mkdb(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Database(os.path.join(d.name, "t.db"))

    def _add(self, db, i, title, india, imp):
        aid = db.insert_article({
            "url": "https://x.com/%d" % i, "normalized_url": "https://x.com/%d" % i,
            "url_hash": "h%d" % i, "title": title, "source": "s",
            "country": "IN" if india >= 0.5 else "GLOBAL",
            "reliability": 0.9})
        db.update_scores(aid, india, imp, aid, "new")

    def test_balanced_mix_when_both_pools_strong(self):
        db = self._mkdb()
        for i in range(4):
            self._add(db, i, "India story %d about RBI policy rates" % i, 0.8, 0.7)
        for i in range(4, 8):
            self._add(db, i, "Global story %d about world markets" % i, 0.1, 0.7)
        sel = select_stories(db, max_stories=4)
        n_india = sum(1 for s in sel if s["india_relevance_score"] >= 0.35)
        self.assertEqual(n_india, 2)
        self.assertEqual(len(sel), 4)

    def test_fills_with_global_when_india_pool_weak(self):
        db = self._mkdb()
        for i in range(2):
            self._add(db, i, "India story %d about RBI rates" % i, 0.8, 0.7)
        for i in range(2, 6):
            self._add(db, i, "Global story %d about markets" % i, 0.1, 0.75)
        sel = select_stories(db, max_stories=4)
        self.assertEqual(len(sel), 4)
        n_india = sum(1 for s in sel if s["india_relevance_score"] >= 0.35)
        self.assertEqual(n_india, 2)  # only the 2 quality Indian stories

    def test_never_selects_weak_stories(self):
        db = self._mkdb()
        self._add(db, 1, "Weak Indian story about lifestyle recipes", 0.8, 0.2)
        self._add(db, 2, "Weak global story opinion piece", 0.1, 0.2)
        sel = select_stories(db, max_stories=4)
        self.assertEqual(len(sel), 0)

    def test_breaking_news_overrides_balance(self):
        db = self._mkdb()
        self._add(db, 1, "India story about RBI rates cut", 0.8, 0.9)
        self._add(db, 2, "Global breaking massive earthquake story", 0.1, 0.95)
        self._add(db, 3, "India story about parliament budget", 0.8, 0.7)
        sel = select_stories(db, max_stories=2)
        # the top-importance global story must not be excluded
        titles = [s["title"] for s in sel]
        self.assertIn("Global breaking massive earthquake story", titles)


class TestStoryIdentity(unittest.TestCase):
    """Duplicate-story prevention must survive cluster-ID churn: a story
    that is re-clustered (new sibling coverage) in a later cycle must
    still be recognized as already tweeted — while genuinely new stories
    remain selectable."""

    def _mkdb(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Database(os.path.join(d.name, "t.db"))

    def _add(self, db, i, title, imp=0.7, status="new"):
        aid = db.insert_article({
            "url": "https://x.com/%d" % i,
            "normalized_url": "https://x.com/%d" % i,
            "url_hash": "h%d" % i, "title": title, "source": "s",
            "country": "IN", "reliability": 0.9})
        db.update_scores(aid, 0.8, imp, i, status)
        return aid

    def test_story_not_retweeted_after_cluster_id_change(self):
        db = self._mkdb()
        # cycle 1: the story is tweeted under cluster id 1
        aid = self._add(db, 1, "Cyclone Remal makes landfall near Sagar "
                               "Island with heavy rain")
        db.update_scores(aid, 0.8, 0.9, 1, "tweeted")
        db.insert_tweet(aid, "tweet text", "deterministic", "posted",
                        tweet_id="100")
        # cycle 2: a new sibling arrived and re-clustering assigned a
        # DIFFERENT cluster id (2) whose representative is the sibling
        self._add(db, 2, "Cyclone Remal makes landfall near Sagar Island")
        sel = select_stories(db)
        # the same story must not be selected again despite the new id
        self.assertEqual(len(sel), 0)

    def test_different_story_still_selected(self):
        db = self._mkdb()
        aid = self._add(db, 1, "Cyclone Remal makes landfall near Sagar "
                               "Island with heavy rain")
        db.update_scores(aid, 0.8, 0.9, 1, "tweeted")
        db.insert_tweet(aid, "tweet text", "deterministic", "posted",
                        tweet_id="100")
        # a genuinely different story joins the feed next cycle
        self._add(db, 2, "Parliament passes new education bill")
        sel = select_stories(db)
        self.assertEqual(len(sel), 1)
        self.assertIn("Parliament", sel[0]["title"])

    def test_failed_post_does_not_block_story(self):
        db = self._mkdb()
        aid = self._add(db, 1, "Cyclone Remal makes landfall near Sagar "
                               "Island with heavy rain")
        # a previous cycle FAILED to post this story (status stays new)
        db.insert_tweet(aid, "tweet text", "deterministic", "failed",
                        error="network_error")
        sel = select_stories(db)
        self.assertEqual(len(sel), 1)   # still retryable


class TestStaleEntryGuard(unittest.TestCase):
    """Old feed entries must never be ingested as fresh news; entries
    without a usable date are always kept."""

    def _age(self, hours):
        dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        return format_datetime(dt)

    def test_fresh_entry_kept(self):
        self.assertFalse(_is_stale(self._age(1), 48))
        self.assertFalse(_is_stale(self._age(47), 48))

    def test_boundary_exact_cutoff_kept(self):
        # exactly at the limit is still fresh (strictly older is stale)
        self.assertFalse(_is_stale(self._age(48 - 0.01), 48))

    def test_stale_entry_dropped(self):
        self.assertTrue(_is_stale(self._age(48.5), 48))
        self.assertTrue(_is_stale(self._age(24 * 30), 48))

    def test_missing_or_bad_date_never_stale(self):
        self.assertFalse(_is_stale(None, 48))
        self.assertFalse(_is_stale("", 48))
        self.assertFalse(_is_stale("not-a-date", 48))


class TestIndiaRelevanceWordBoundaries(unittest.TestCase):
    def test_word_ending_in_ed_is_not_the_directorate(self):
        # regression: substring "ed " matched inside "arrested"
        s = score_india_relevance("Suspect arrested after Paris robbery",
                                  "Police walked back earlier claims",
                                  "GLOBAL")
        self.assertEqual(s, 0.0)

    def test_ed_directorate_still_matches(self):
        s = score_india_relevance("ED raids multiple locations in Mumbai",
                                  "", "GLOBAL")
        self.assertGreaterEqual(s, 0.30)   # ed topic + mumbai strong

    def test_valid_indian_references_preserved(self):
        s = score_india_relevance("Indian rupee strengthens",
                                  "Reserve Bank of India commentary",
                                  "GLOBAL")
        # indian + rupee + reserve bank + india
        self.assertGreaterEqual(s, 0.65)

    def test_state_names_match_as_words(self):
        s = score_india_relevance("Flood alert issued across Kerala",
                                  "", "GLOBAL")
        self.assertGreaterEqual(s, 0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
