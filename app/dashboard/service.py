"""Dashboard service layer: scan -> rank -> prepare the publishing
queue, and apply user actions (copy / mark posted / skip).

The dashboard NEVER posts to X. The X button only opens the browser;
the ✓ button only records that the human posted manually. Automatic
behavior is strictly COLLECT -> RANK -> PREPARE.

The recommendation follows the selective-editor workflow: collect ->
dedupe -> cluster -> score (freshness, momentum, importance, hook,
conversation, source quality) -> publish score -> topic cooldown ->
global post cooldown -> select only the best story -> 3 tweet options
-> story-conditioned best variant -> 0-2 relevant hashtags -> <=280
validation. Quality and reach potential over posting volume: "no story
worth posting right now" is an acceptable answer.
"""
from datetime import datetime, timedelta, timezone

from app.config import CONFIG
from app.database import utcnow_iso
from app.logger import get_logger
from app.news.collector import collect_and_process
from app.news.ranking import (_parse_dt, publish_score, reach_score,
                              save_value_score, shareability_score,
                              why_publish, why_this,
                              x_reach_potential)
from app.tweet.generator import (choose_label, has_valid_source_url,
                                 tokens_lite, tweet_options)
from app.tweet.hashtags import (
    LOCATION_TAGS, suggest_hashtags, with_hashtags)
from app.tweet.validator import validate_tweet

log = get_logger("dashboard", log_dir=CONFIG.DATA_DIR)

# how many ranked candidates to keep prepared in the queue
QUEUE_SIZE = 6
# statuses that make an article a live candidate for the dashboard
_CANDIDATE_STATUSES = ("new", "ready", "copied")

# curated skip reasons offered by the UI (free text is also accepted)
SKIP_REASONS = ("Not important", "Duplicate", "Not suitable for X",
                "Already covered", "Other")

# how often the UI re-reads state from the DB (display only — never RSS)
AUTO_REFRESH_SECONDS = 300


def _cluster_rows(db, article):
    """Rows of the article's same-story cluster (itself included) — the
    coverage that feeds the reach 'trending' signal and the publish
    score's news-momentum signal."""
    a = dict(article)
    cid = a.get("story_cluster_id")
    if not cid:
        return [a]
    rows = db.query(
        "SELECT * FROM articles WHERE story_cluster_id=?", (cid,))
    return [dict(r) for r in rows] or [a]


def _select_tags(tags):
    """Hashtag policy: 0-2 highly relevant hashtags. The first tag is
    the strongest specific topical/entity match; a SECOND tag is allowed
    only when it names the story's own location (e.g. #Earthquake #Delhi).
    Generic filler (#India, #News, #BreakingNews) is never used to fill
    the quota — zero or one tag is a perfectly fine outcome. No trend
    is ever claimed: tags come only from the story's own text."""
    specific = [t for t in (tags or []) if t != "#India"]
    if not specific:
        return []
    chosen = [specific[0]]
    for t in specific[1:]:
        if len(chosen) >= CONFIG.MAX_HASHTAGS:
            break
        if t in LOCATION_TAGS and t not in chosen:
            chosen.append(t)
    return chosen


def _fact_floor(max_facts):
    """Anti-sensation floor: the minimum number of facts a 'punchy'
    variant must retain to be selectable. A short variant may only win
    by being tighter — never by deleting half the story's facts."""
    return max(1, (max_facts + 1) // 2)


def _variant_objective(option, share, save, max_facts):
    """Story-conditioned variant objective (deterministic, advisory XRP
    signals only): information always pays, and the story's own signals
    favour a variant ROLE — a shareable story may favour punchy, a
    save-worthy dense story briefing, a single-fact story flash. The
    most sensational variant never wins on excitement alone."""
    base = option["hook"] + 2 * min(option["facts"], 6)
    if option["style"] == "punchy" and share >= 60:
        base += 15
    elif option["style"] == "briefing" and save >= 60:
        base += 15
    elif option["style"] == "flash" and max_facts <= 2 and share >= 60:
        base += 10
    return base


def _build_tweet(db, article):
    """Best tweet variant for one candidate article, selected by the
    story-conditioned objective (see _variant_objective), plus 0-2
    highly relevant hashtags. Returns (tweet_text, tags, options) or
    (None, [], [])."""
    if not has_valid_source_url(article):
        return None, [], []
    cluster_texts = []
    if article.get("story_cluster_id"):
        for sib in db.query(
                "SELECT title, summary FROM articles "
                "WHERE story_cluster_id=? AND id<>?",
                (article["story_cluster_id"], article["id"])):
            cluster_texts.append(
                "%s %s" % (sib["title"] or "", sib["summary"] or ""))
    options = tweet_options(
        article, importance_score=article["importance_score"],
        char_limit=CONFIG.TWEET_CHAR_LIMIT, cluster_texts=cluster_texts)
    if not options:
        return None, [], []
    tags = _select_tags(suggest_hashtags(
        article.get("title") or "", article.get("summary") or "",
        article.get("india_relevance_score") or 0.0))
    scored = []
    for opt in options:
        text, used = with_hashtags(opt["text"], tags,
                                   CONFIG.TWEET_CHAR_LIMIT)
        ok, reason = validate_tweet(text, CONFIG.TWEET_CHAR_LIMIT)
        if not ok:
            log.warning("dashboard: invalid tweet variant for article %s "
                        "(%s): %s", article["id"], opt["style"], reason)
            continue
        scored.append(dict(opt, text=text, hashtags=used,
                           char_count=len(text)))
    if not scored:
        return None, [], []
    # story-conditioned selection: all variants stay available to the
    # editor; the chosen one is picked deterministically from the
    # story's own advisory signals, with an anti-sensation fact floor
    # barring punchy from winning by deleting facts
    share = shareability_score(article.get("title") or "",
                               article.get("summary") or "")
    save = save_value_score(article.get("title") or "",
                            article.get("summary") or "")
    max_facts = max((o["facts"] for o in scored), default=0)
    floor = _fact_floor(max_facts)
    pool = [o for o in scored
            if o["style"] != "punchy" or o["facts"] >= floor] or scored
    best = max(pool, key=lambda o: (_variant_objective(o, share, save,
                                                       max_facts),
                                    o["hook"], o["facts"]))
    return best["text"], best["hashtags"], scored


def _candidate_rows(db):
    """Un-posted, un-skipped, scored articles, newest-relevance first.
    Excludes stories similar to already-posted titles (select_stories
    logic applies at tweet time; here we exclude by status only)."""
    placeholders = ",".join("?" * len(_CANDIDATE_STATUSES))
    rows = db.query(
        "SELECT * FROM articles WHERE status IN (%s) "
        "AND india_relevance_score IS NOT NULL "
        "AND processed_at IS NOT NULL "
        "ORDER BY discovered_at DESC LIMIT 200" % placeholders,
        _CANDIDATE_STATUSES)
    return [dict(r) for r in rows]   # plain dicts: .get() works


def _story_tier(breakdown):
    """Descriptive quality tier — NEVER a gate (the authoritative gates
    stay in _passes_gates, unchanged):
      Weak     — fails a quality floor today (never recommended)
      Normal   — passes the two-tier floors (publish / momentum /
                 importance): eligible
      Trending — Normal + genuinely multi-source momentum + fresh
      Breaking — the existing all-three-90-bars rule.
    Cooldowns are deliberately NOT part of a tier: a cooldown says
    'not now', not 'not good enough'."""
    if _is_breaking(breakdown):
        return "Breaking"
    normal = (breakdown["publish"] >= CONFIG.MIN_PUBLISH_SCORE and
              breakdown["momentum"] >= CONFIG.MIN_STORY_MOMENTUM and
              breakdown["importance"] >= CONFIG.MIN_STORY_IMPORTANCE)
    if normal and (breakdown["momentum"] >= CONFIG.TRENDING_MOMENTUM and
                   breakdown["freshness"] >= CONFIG.TRENDING_FRESHNESS):
        return "Trending"
    return "Normal" if normal else "Weak"


def _story_kind(article, breakdown):
    """Editorial classification shown next to the reach score:
    Breaking / Developing / Trending / Standard.

    Reuses the existing deterministic label logic (choose_label — a
    label must be earned by the source's own wording) and the existing
    trending signal (cluster coverage). It is a descriptive badge, not
    a score change and not a prediction of impressions."""
    label = choose_label(article.get("title") or "",
                         article.get("summary") or "",
                         article.get("importance_score") or 0.0)
    if label == "BREAKING":
        return "Breaking"
    if label == "Developing":
        return "Developing"
    if breakdown.get("trending", 0) >= 60:   # 3+ outlets covering it
        return "Trending"
    return "Standard"


def _story_entry(db, article):
    """Full dashboard card payload for one article (or None if it
    cannot produce a valid tweet)."""
    tweet, tags, options = _build_tweet(db, article)
    if tweet is None:
        db.set_article_status(article["id"], "rejected")
        return None
    cluster_rows = _cluster_rows(db, article)
    breakdown = reach_score(article, len(cluster_rows))
    # advisory 'Reach potential — editorial estimate'. Saturation = our
    # own posts about this topic in the lookback window (wider than the
    # authoritative topic cooldown). ADVISORY ONLY — never gates.
    posted_recent = _recent_posted_titles(
        db, CONFIG.SATURATION_WINDOW_HOURS * 60,
        datetime.now(timezone.utc))
    overlap = sum(1 for t in posted_recent
                  if _similar_to_posted(article.get("title") or "", [t]))
    reach_potential = x_reach_potential(
        article, cluster_rows=cluster_rows, posted_overlap=overlap,
        account_premium=CONFIG.ACCOUNT_PREMIUM)
    # descriptive quality tier (never a gate) for queue display
    tier = _story_tier(publish_score(
        article, cluster_rows=cluster_rows,
        now=datetime.now(timezone.utc)))
    return {
        "article_id": article["id"],
        "cluster_id": article.get("story_cluster_id") or article["id"],
        "tier": tier,
        "headline": article["title"],
        "source": article["source"],
        "published_at": article.get("published_at"),
        "url": article.get("normalized_url") or article.get("url"),
        "india": breakdown["india"],
        "importance": breakdown["importance"],
        "trending": breakdown["trending"],
        "freshness": breakdown["freshness"],
        "source_quality": breakdown["source_quality"],
        "reach": breakdown["reach"],
        "why": why_this(breakdown),
        "kind": _story_kind(article, breakdown),
        "reach_potential": reach_potential,
        "tweet": tweet,
        "hashtags": tags,
        "options": options,
        "hook": max((o["hook"] for o in options), default=0.0),
        "char_count": len(tweet),
        "char_limit": CONFIG.TWEET_CHAR_LIMIT,
        "status": article["status"] if article["status"] != "new"
                  else "ready",
    }


def _cluster_reps(db):
    """One entry per STORY (cluster), ranked by reach: the highest-reach
    representative article is kept (ties -> lower article id), cluster
    siblings never compete separately (the article rows themselves are
    untouched — this is presentation only). Returns ALL reps, uncapped;
    the queue caps at QUEUE_SIZE, recommendation evaluates every story.
    Pure display read apart from one hygiene write: an article that
    cannot produce any valid tweet is marked 'rejected' (it is dead
    weight in every future pass too). No other row is touched — only an
    explicit user action (copy / mark posted / skip / unskip) changes
    an article."""
    best_by_cluster = {}
    for a in _candidate_rows(db):
        entry = _story_entry(db, a)
        if entry is None:
            continue
        cid = entry["cluster_id"]
        cur = best_by_cluster.get(cid)
        # highest reach represents the story; ties -> lower article id
        if cur is None or (entry["reach"], -entry["article_id"]) > \
                (cur["reach"], -cur["article_id"]):
            best_by_cluster[cid] = entry
    return sorted(best_by_cluster.values(),
                  key=lambda e: (-e["reach"], e["article_id"]))


def refresh_queue(db):
    """(Re)build the ranked publishing queue from candidates: one entry
    per story cluster (see _cluster_reps), capped at QUEUE_SIZE.
    Returns the ranked list of entries."""
    return _cluster_reps(db)[:QUEUE_SIZE]


def scan(db):
    """One collection pass (RSS -> dedup -> classify -> cluster).
    Never posts, never calls the X API."""
    stats = collect_and_process(db)
    log.info("dashboard scan: %s", stats)
    return stats


# --- cooldowns + the selective recommendation -------------------------------

def _last_posted_at(db):
    row = db.query_one(
        "SELECT MAX(created_at) m FROM tweets WHERE status='posted'")
    return _parse_dt(row["m"]) if row and row["m"] else None


def _last_posted_info(db):
    """The most recent manually posted story, for the cooldown panel."""
    row = db.query_one(
        "SELECT t.created_at, t.tweet_text, a.title "
        "FROM tweets t LEFT JOIN articles a ON a.id=t.article_id "
        "WHERE t.status='posted' "
        "ORDER BY t.created_at DESC, t.id DESC LIMIT 1")
    if not row:
        return None
    return {"headline": row["title"] or (row["tweet_text"] or "")[:80],
            "posted_at": row["created_at"]}


def _recent_posted_titles(db, minutes, now):
    """Titles posted within the topic-cooldown window — used to spot a
    story about a topic we just covered."""
    cutoff = (now - timedelta(minutes=minutes))\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.query(
        "SELECT a.title FROM tweets t JOIN articles a "
        "ON a.id=t.article_id WHERE t.status='posted' "
        "AND t.created_at >= ?", (cutoff,))
    return [r["title"] or "" for r in rows]


# Generic newsroom words that say nothing about WHICH story it is.
# Two headlines sharing only these are NOT the same topic: "Government
# announces new tax policy" vs "Police arrest suspect in Hyderabad".
# Topic similarity must key on entities and topical terms instead.
_GENERIC_TOPIC_TOKENS = frozenset("""
government governments govt minister ministers ministry police
official officials india indian people public state states says said
say announce announces announced report reports reported news latest
update updates dies died dead death killed kills killing injured
arrest arrests arrested court case cases filed probe inquiry
authorities administration department bureau agency chief senior
major several including according amid ahead after over under
""".split())


def _similar_to_posted(title, posted_titles):
    """Token overlap between the candidate headline and a recently
    posted one — same topic, not necessarily the same story. Generic
    newsroom words (government, police, minister, …) are removed first
    so unrelated stories sharing only boilerplate vocabulary are never
    treated as the same topic."""
    toks = {t for t in tokens_lite(title)
            if t not in _GENERIC_TOPIC_TOKENS}
    if not toks:
        return False
    for posted in posted_titles:
        ptoks = {t for t in tokens_lite(posted)
                 if t not in _GENERIC_TOPIC_TOKENS}
        if ptoks and len(toks & ptoks) / min(len(toks), len(ptoks)) >= 0.4:
            return True
    return False


def _passes_gates(breakdown, similar, cooldown_left):
    """The recommendation gates:
    1. global post cooldown (only a genuinely breaking story — ALL three
       bars — is recommended right after a post),
    2. topic cooldown (a story about a topic we just posted must be a
       major NEW development),
    3. quality bar (below MIN_PUBLISH_SCORE, never recommended),
    4. two-tier quality gate (momentum + importance floors — a normal
       story must be confirmed by multiple outlets AND genuinely
       important; breaking stories clear the floors automatically)."""
    if cooldown_left > 0 and not (
            breakdown["importance"] >= CONFIG.BREAKING_IMPORTANCE and
            breakdown["momentum"] >= CONFIG.BREAKING_MOMENTUM and
            breakdown["freshness"] >= CONFIG.BREAKING_FRESHNESS):
        return False
    if similar and not (
            breakdown["importance"] >= CONFIG.MAJOR_DEV_IMPORTANCE and
            breakdown["freshness"] >= CONFIG.MAJOR_DEV_FRESHNESS):
        return False
    if breakdown["publish"] < CONFIG.MIN_PUBLISH_SCORE:
        return False
    if (breakdown["momentum"] < CONFIG.MIN_STORY_MOMENTUM or
            breakdown["importance"] < CONFIG.MIN_STORY_IMPORTANCE):
        return False
    return True


def _is_breaking(breakdown):
    """Genuinely breaking (all three bars) — may replace the current
    recommendation immediately, without waiting for the score-margin."""
    return (breakdown["importance"] >= CONFIG.BREAKING_IMPORTANCE and
            breakdown["momentum"] >= CONFIG.BREAKING_MOMENTUM and
            breakdown["freshness"] >= CONFIG.BREAKING_FRESHNESS)


def _xrp_of(entry):
    """Advisory reach-potential score of an entry (0 when absent).
    Display/tiebreak only — never a gate, never an impressions claim."""
    return (entry.get("reach_potential") or {}).get("score") or 0.0


def _recommendation(db, now=None):
    """The ONE story worth posting right now, after every gate:
    publish-score quality bar, topic cooldown, global post cooldown.
    Competition is STORY-level: one representative per cluster (same
    rep rule as the queue), so sibling articles never compete against
    each other and a story is judged by its full coverage. Returns
    (entry_or_None, note, cooldown_minutes_left). The entry carries
    the full publish breakdown and 3 tweet options with the best hook
    pre-selected.

    Stability: the current recommendation is persisted and sticky. A
    challenger replaces it only when it wins by MIN_SCORE_IMPROVEMENT
    publish-score points, is genuinely breaking, or — the approved XRP
    tiebreak — reaches materially higher advisory reach potential
    (XRP_CHALLENGE_MARGIN) despite a smaller score margin. XRP never
    opens the gate for a below-bar story."""
    now = now or datetime.now(timezone.utc)
    last = _last_posted_at(db)
    cooldown_left = 0.0
    if last is not None:
        cooldown_left = (CONFIG.NEWS_COOLDOWN_MINUTES -
                         (now - last).total_seconds() / 60.0)
    posted_titles = _recent_posted_titles(
        db, CONFIG.TOPIC_COOLDOWN_MINUTES, now)
    best = None
    passed = {}          # article_id -> gate-passing entry (for stability)
    for entry in _cluster_reps(db):
        article = dict(db.article_by_id(entry["article_id"]))
        similar = _similar_to_posted(article.get("title") or "",
                                     posted_titles)
        breakdown = publish_score(
            article, cluster_rows=_cluster_rows(db, article),
            similar_to_posted=similar, now=now)
        entry.update(breakdown)
        entry["why_publish"] = why_publish(breakdown)
        if not _passes_gates(breakdown, similar, cooldown_left):
            continue
        passed[entry["article_id"]] = entry
        # story-level competition: publish score first, advisory XRP
        # breaks exact ties (never overrides a clear score win)
        if best is None or (breakdown["publish"], _xrp_of(entry)) > \
                (best["publish"], _xrp_of(best)):
            best = entry

    # stability: a challenger needs a clear margin, a breaking story,
    # or materially higher advisory reach potential to displace the
    # persisted current recommendation
    prev = db.get_recommendation()
    if best is not None and prev is not None and \
            prev["article_id"] != best["article_id"]:
        prev_entry = passed.get(prev["article_id"])
        if prev_entry is not None and not _is_breaking(best) and \
                best["publish"] < (prev_entry["publish"] +
                                   CONFIG.MIN_SCORE_IMPROVEMENT):
            # XRP tiebreak: a challenger within the score margin may
            # still win when its advisory reach potential is clearly
            # higher; anything less keeps the incumbent (no flip-flop)
            if _xrp_of(best) - _xrp_of(prev_entry) < \
                    CONFIG.XRP_CHALLENGE_MARGIN:
                best = prev_entry          # keep the incumbent

    # persist the choice (or clear it when nothing qualifies)
    chosen_id = best["article_id"] if best else None
    if chosen_id != (prev["article_id"] if prev else None):
        db.set_recommendation(chosen_id,
                              best["publish"] if best else None)

    if best is not None:
        note = best["why_publish"]
    elif cooldown_left > 0:
        note = ("Cooldown — %d min until the next story is recommended "
                "(only a genuinely breaking story overrides)."
                % int(cooldown_left + 0.999))
    else:
        note = ("No story worth posting right now — waiting for a "
                "genuinely strong one.")
    return best, note, max(0.0, cooldown_left)


def state(db):
    """Everything the dashboard UI needs in one payload."""
    queue = refresh_queue(db)
    current, note, cooldown_left = _recommendation(db)
    # the current story keeps its (single) entry in the browsable queue;
    # its cluster never ALSO fills Up Next
    if current is not None:
        up_next = [e for e in queue
                   if e["cluster_id"] != current["cluster_id"]][:4]
    else:
        up_next = queue[:4]
    counts = db.query(
        "SELECT status, COUNT(*) c FROM articles "
        "GROUP BY status")
    by_status = {r["status"]: r["c"] for r in counts}
    last_scan = db.query_one(
        "SELECT MAX(processed_at) m FROM articles")
    return {
        "last_scan": last_scan["m"] if last_scan else None,
        "stories_found": sum(by_status.values()),
        "candidates": len(queue),
        "posted": by_status.get("posted", 0),
        "skipped": by_status.get("skipped", 0),
        "copied": by_status.get("copied", 0),
        "publishing_status": "Manual posting mode — X API posting disabled",
        "recommendation_note": note,
        "cooldown_minutes_left": int(cooldown_left + 0.999),
        "last_posted": _last_posted_info(db),
        "current": current,
        "up_next": up_next,
        "queue": queue,
        "history": history(db),
        "skip_reasons": list(SKIP_REASONS),
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        "now": utcnow_iso(),
    }


def history(db, q=None, limit=100, status=None, source=None, since=None):
    """Unified story history with optional filters.

    status: None (all of posted/skipped/copied) or one of them.
    q: keyword over headline/tweet/source. source: exact source name.
    since: minimum ISO timestamp (date filter)."""
    wanted = [status] if status else ("posted", "skipped", "copied")
    rows = []
    if "posted" in wanted:
        for r in db.posted_history(limit=limit, q=q, source=source,
                                   since=since):
            rows.append({
                "article_id": r["article_id"],
                "posted_at": r["created_at"],
                "headline": r["title"],
                "source": r["source"],
                "tweet": r["tweet_text"],
                "url": r["normalized_url"],
                "status": "posted",
            })
        if not q and not source and not since:
            for r in db.pending_history(limit=limit):
                rows.append({
                    "posted_at": r["created_at"],
                    "headline": r["title"],
                    "source": r["source"],
                    "tweet": r["tweet_text"],
                    "url": r["article_url"],
                    "status": "posted",
                })
    if "skipped" in wanted:
        for r in db.skipped_history(limit=limit, q=q, source=source,
                                    since=since):
            rows.append({
                "article_id": r["id"],
                "posted_at": r["processed_at"],
                "headline": r["title"],
                "source": r["source"],
                "tweet": None,
                "url": r["normalized_url"],
                "status": "skipped",
                "skip_reason": r["skip_reason"],
            })
    if "copied" in wanted:
        for r in db.copied_history(limit=limit, q=q, source=source,
                                   since=since):
            rows.append({
                "article_id": r["id"],
                "posted_at": r["processed_at"],
                "headline": r["title"],
                "source": r["source"],
                "tweet": None,
                "url": r["normalized_url"],
                "status": "copied",
            })
    rows.sort(key=lambda r: r["posted_at"] or "", reverse=True)
    return rows[:limit]


def mark_copied(db, article_id):
    """User pressed COPY — record it. This NEVER means posted."""
    return db.mark_article_copied(article_id)


def mark_posted(db, article_id, tweet_text=None):
    """User pressed ✓ after manually posting on X. Records article ID,
    tweet text, source, URL, timestamp, status='posted'. The story never
    appears as a candidate again."""
    article = db.article_by_id(article_id)
    if article is None:
        return {"ok": False, "error": "not_found"}
    article = dict(article)
    if article["status"] == "posted":
        return {"ok": False, "error": "already_posted"}
    if not tweet_text:
        # no (or empty) text supplied — fall back to the generated tweet
        tweet, _, _ = _build_tweet(db, article)
        if tweet is None:
            row = db.query_one(
                "SELECT tweet_text FROM tweets WHERE article_id=? "
                "ORDER BY id DESC LIMIT 1", (article_id,))
            tweet = row["tweet_text"] if row else article["title"]
        tweet_text = tweet
    db.mark_article_posted(article_id, tweet_text,
                           article.get("source"),
                           article.get("normalized_url") or
                           article.get("url"))
    log.info("story %s marked posted (manual, no X API call)",
             article_id)
    return {"ok": True, "article_id": article_id}


def skip(db, article_id, reason=None):
    """User pressed Skip — story hidden from candidates but never
    deleted from the DB."""
    if db.skip_article(article_id, reason):
        return {"ok": True}
    return {"ok": False, "error": "not_found_or_final"}


def unskip(db, article_id):
    """Explicitly reconsider a skipped story: back into the candidate
    pool. Never touches posted stories; never posts anything."""
    if db.unskip_article(article_id):
        log.info("story %s reconsidered — back to 'ready' (no X API "
                 "call)", article_id)
        return {"ok": True, "article_id": article_id}
    return {"ok": False, "error": "not_skipped"}
