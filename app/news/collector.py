"""Pipeline orchestration: collect → normalize → dedup → score → select → tweet."""
from datetime import datetime, timezone

from app.config import CONFIG
from app.database import Database, utcnow_iso
from app.logger import get_logger
from app.news import sources as sources_mod
from app.news.classifier import score_india_relevance
from app.news.clusterer import cluster_articles
from app.news.deduplicator import find_similar_title
from app.news.importance import score_importance, parse_published
from app.news.normalizer import (clean_text, jaccard, normalize_url,
                                 strip_source_suffix, tokens, url_hash)
from app.news.rss import fetch_feed
from app.tweet.generator import generate_tweet
from app.tweet.validator import check_rate_limits, validate_tweet

log = get_logger("pipeline")


def _is_stale(published_at, max_age_hours):
    """True only when the entry carries a parseable timestamp that is
    older than the cutoff. Entries without a usable date are never
    treated as stale — a feed omitting dates must not lose fresh news."""
    dt = parse_published(published_at)
    if dt is None:
        return False
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return age_h > max_age_hours


def collect_and_process(db, limit_per_feed=20):
    """Fetch all feeds, insert new articles, score, cluster.
    Returns stats dict."""
    stats = {"sources": 0, "discovered": 0, "duplicates": 0,
             "clustered": 0, "clusters": 0}

    srcs = sources_mod.enabled_sources(db)
    stats["sources"] = len(srcs)

    existing = db.recent_articles(hours=72)
    seen_hashes = {r["url_hash"] for r in existing}
    # title dedup is SOURCE-AWARE: a near-identical title from a
    # different source is cross-outlet coverage of the same event —
    # keep it so clustering (and momentum's source count) can see the
    # confirmation. Only the SAME source re-publishing near-identical
    # copy is a duplicate. Distinct source names are counted as-is by
    # momentum; no syndication detection is attempted here.
    seen_titles = {}
    for r in existing:
        seen_titles.setdefault(r["source"], []).append(r["title"])

    for src in srcs:
        entries = fetch_feed(src, timeout=CONFIG.HTTP_TIMEOUT,
                             user_agent=CONFIG.USER_AGENT)[:limit_per_feed]
        for e in entries:
            if _is_stale(e["published_at"], CONFIG.MAX_ARTICLE_AGE_HOURS):
                stats["duplicates"] += 1
                continue
            title = clean_text(strip_source_suffix(e["title"]))
            summary = clean_text(e["summary"])[:500]
            if not title or len(title) < 15:
                continue
            nurl = normalize_url(e["url"])
            h = url_hash(nurl)
            if h in seen_hashes:
                stats["duplicates"] += 1
                continue
            if find_similar_title(title, seen_titles.get(e["source"], []),
                                  CONFIG.DUP_TITLE_THRESHOLD):
                stats["duplicates"] += 1
                continue
            aid = db.insert_article({
                "url": e["url"], "normalized_url": nurl, "url_hash": h,
                "title": title, "summary": summary, "source": e["source"],
                "category": e["category"], "country": e["source_country"],
                "reliability": e["reliability"],
                "published_at": e["published_at"]})
            if aid:
                seen_hashes.add(h)
                seen_titles.setdefault(e["source"], []).append(title)
                stats["discovered"] += 1
                india = score_india_relevance(
                    title, summary, e["source_country"])
                importance = score_importance(
                    title, summary, e["reliability"], e["published_at"])
                db.execute(
                    "UPDATE articles SET india_relevance_score=?, "
                    "importance_score=?, processed_at=? WHERE id=?",
                    (india, importance, utcnow_iso(), aid))

    # cluster recent new articles
    recent = db.recent_articles(hours=24)
    arts = [{"id": r["id"], "title": r["title"],
             "india": r["india_relevance_score"],
             "imp": r["importance_score"]} for r in recent]
    clusters = cluster_articles(
        [{"id": a["id"], "title": a["title"]} for a in arts],
        CONFIG.CLUSTER_TITLE_THRESHOLD)
    for c in clusters:
        if len(c) > 1:
            cid = db.create_cluster(c[0])
            # EVERY member joins the cluster — representative included.
            # create_cluster() only inserts the story_clusters row; the
            # representative's story_cluster_id must be set here too,
            # else it sits outside its own cluster and downstream reads
            # the story as single-source coverage (momentum collapse).
            for aid in c:
                db.execute(
                    "UPDATE articles SET story_cluster_id=? WHERE id=?",
                    (cid, aid))
            stats["clustered"] += len(c) - 1
        else:
            db.execute("UPDATE articles SET story_cluster_id=? WHERE id=?",
                       (c[0], c[0]))
    stats["clusters"] = len(clusters)
    return stats


def select_stories(db, max_stories=3):
    """Pick representative articles: min importance, soft India preference.
    Returns list of article rows.

    Duplicate-story prevention is based on STORY IDENTITY, not cluster
    IDs: clustering is re-run every cycle and its IDs are unstable, so a
    story whose cluster regrouped (new sibling coverage arriving) must
    still be recognized as already tweeted. The identity check compares
    the cluster's representative title against the titles of articles
    that were already successfully tweeted (title similarity at the
    clustering threshold)."""
    rows = db.query(
        "SELECT * FROM articles WHERE status='new' "
        "AND importance_score >= ? AND story_cluster_id IS NOT NULL "
        "ORDER BY importance_score DESC LIMIT 100",
        (CONFIG.MIN_IMPORTANCE,))

    # stories already published successfully (dry-run rows also count:
    # the story has been "used" — same behavior as before). Failed and
    # rejected posts never block a story: it stays retryable.
    tweeted = db.query(
        "SELECT a.title t, a.story_cluster_id c FROM tweets t "
        "JOIN articles a ON t.article_id=a.id "
        "WHERE t.status IN ('posted', 'dry_run', 'tweeted')")
    tweeted_clusters = {r["c"] for r in tweeted}
    tweeted_title_tokens = [tokens(r["t"] or "") for r in tweeted]

    def already_published(title):
        t = tokens(title)
        if not t:
            return False
        return any(jaccard(t, tt) >= CONFIG.CLUSTER_TITLE_THRESHOLD
                   for tt in tweeted_title_tokens if tt)

    # de-duplicate by cluster: one story per cluster
    seen_clusters = set()

    pool = []
    for r in rows:
        cid = r["story_cluster_id"]
        if cid in seen_clusters:
            continue
        # representatives: prefer highest-importance member of each cluster
        best = db.query_one(
            "SELECT id FROM articles WHERE story_cluster_id=? AND status='new' "
            "ORDER BY importance_score DESC, reliability_score DESC LIMIT 1",
            (cid,))
        rep = r if (not best or best["id"] == r["id"]) else \
            db.query_one("SELECT * FROM articles WHERE id=?", (best["id"],))
        if rep is None:
            rep = r
        seen_clusters.add(cid)
        # story identity: stable across cluster re-grouping
        if rep["id"] in [p["id"] for p in pool]:
            continue
        if cid in tweeted_clusters or already_published(rep["title"]):
            continue
        pool.append(rep)

    pool.sort(key=lambda r: r["importance_score"], reverse=True)
    indians = [p for p in pool if p["india_relevance_score"] >= CONFIG.INDIA_MIN_SCORE]
    globals_ = [p for p in pool if p["india_relevance_score"] < CONFIG.INDIA_MIN_SCORE]

    selected = []
    target_india = round(max_stories * CONFIG.INDIA_TARGET_SHARE)
    for a in indians[:target_india]:
        selected.append(a)
    # fill remaining from either list, importance-ordered, never weak stories
    rest = sorted(globals_ + indians[target_india:],
                  key=lambda r: r["importance_score"], reverse=True)
    for r in rest:
        if len(selected) >= max_stories:
            break
        selected.append(r)
    return selected[:max_stories]
