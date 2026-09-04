"""READ-ONLY live-data audit of SAYOX's recommendation pipeline.

Copies the production DB to a temp snapshot and runs the REAL
production pipeline (service.state, _build_tweet, ranking functions)
against the copy. The production DB is never opened for write, no X
API call is ever made, nothing is posted. Prints a diagnostic report.

Usage: python3 scripts/audit_state.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app.config import CONFIG                       # noqa: E402
from app.database import Database                   # noqa: E402
from app.dashboard import service                   # noqa: E402
from app.news.ranking import publish_score          # noqa: E402
from app.tweet.hashtags import suggest_hashtags     # noqa: E402

SRC = CONFIG.DB_PATH
now = datetime.now(timezone.utc)


def gate_report(breakdown, similar, cooldown_left):
    """Which of the four gates (if any) this story fails right now."""
    fails = []
    if cooldown_left > 0 and not (
            breakdown["importance"] >= CONFIG.BREAKING_IMPORTANCE and
            breakdown["momentum"] >= CONFIG.BREAKING_MOMENTUM and
            breakdown["freshness"] >= CONFIG.BREAKING_FRESHNESS):
        fails.append("global-cooldown (%.0f min left, not all-90s breaking)"
                     % cooldown_left)
    if similar and not (
            breakdown["importance"] >= CONFIG.MAJOR_DEV_IMPORTANCE and
            breakdown["freshness"] >= CONFIG.MAJOR_DEV_FRESHNESS):
        fails.append("topic-cooldown (similar to recent post, not a "
                     "major 90/90 development)")
    if breakdown["publish"] < CONFIG.MIN_PUBLISH_SCORE:
        fails.append("publish %.1f < %d" % (breakdown["publish"],
                                            CONFIG.MIN_PUBLISH_SCORE))
    if (breakdown["momentum"] < CONFIG.MIN_STORY_MOMENTUM or
            breakdown["importance"] < CONFIG.MIN_STORY_IMPORTANCE):
        fails.append("two-tier (momentum %.0f<%d or importance %.0f<%d)"
                     % (breakdown["momentum"], CONFIG.MIN_STORY_MOMENTUM,
                        breakdown["importance"], CONFIG.MIN_STORY_IMPORTANCE))
    return fails


def main():
    if not os.path.exists(SRC):
        print("NO DB at", SRC)
        return
    tmp = tempfile.mkdtemp(prefix="sayox-audit-")
    snap = os.path.join(tmp, "snapshot.db")
    shutil.copy2(SRC, snap)
    db = Database(snap)                    # snapshot only — prod untouched
    try:
        report(db)
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def report(db):
    print("=" * 78)
    print("SAYOX LIVE-DATA AUDIT — %s" % now.strftime("%Y-%m-%d %H:%M UTC"))
    print("snapshot of %s (read-only; production DB untouched)" % SRC)
    print("=" * 78)

    s = service.state(db)
    current = s["current"]

    # last real scan stats from the log (read-only)
    print("\n### 0. LAST SCAN (from data/newsbot.log)")
    try:
        with open(os.path.join(CONFIG.DATA_DIR, "newsbot.log")) as fh:
            scans = [ln for ln in fh if "dashboard scan" in ln]
        print(scans[-1].strip() if scans else "no dashboard scan logged")
    except OSError as exc:
        print("log unreadable: %s" % exc)

    # --- 1. current recommendation -------------------------------------
    print("\n### 1. CURRENT RECOMMENDATION")
    if current is None:
        print("NONE. note: %s" % s["recommendation_note"])
    else:
        print("headline : %s" % current["headline"])
        print("source   : %s | article %s | cluster %s | kind %s | tier %s"
              % (current.get("source"), current["article_id"],
                 current["cluster_id"], current.get("kind"),
                 current.get("tier")))
        print("why      : %s" % current.get("why_publish"))
        print("publish components:")
        for k in ("momentum", "freshness", "importance", "conversation",
                  "hook", "source", "visual", "uniqueness"):
            print("   %-13s %s" % (k, current.get(k)))
        print("   %-13s %s   (FINAL)" % ("publish", current.get("publish")))
        rp = current.get("reach_potential") or {}
        print("XRP      : %s (%s)" % (rp.get("score"), rp.get("label")))
        print("XRP signals      : %s" % json.dumps(rp.get("signals", {})))
        print("XRP saturation   : %s" % rp.get("saturation_penalty"))
        print("XRP reasons:")
        for r in rp.get("reasons", []):
            print("   - %s" % r)

    # --- 2. top competing stories --------------------------------------
    print("\n### 2. TOP COMPETING STORIES (ranked queue, one per cluster)")
    reps = service._cluster_reps(db)
    multi = 0
    for e in reps:
        rows = service._cluster_rows(
            db, dict(db.article_by_id(e["article_id"])))
        if len({r.get("source") for r in rows if r.get("source")}) > 1:
            multi += 1
    print("candidate stories (clusters): %d | multi-source clusters: %d"
          % (len(reps), multi))
    print("%-4s %-34s %3s %3s %5s %5s %5s %5s %5s %-9s %s"
          % ("#", "headline", "cl", "src", "momt", "fres", "impt", "publ",
             "XRP", "tier", "why it lost / status"))
    for i, e in enumerate(s["queue"][:10], 1):
        art = dict(db.article_by_id(e["article_id"]))
        rows = service._cluster_rows(db, art)
        br = publish_score(art, cluster_rows=rows, now=now)
        n_src = len({r.get("source") for r in rows if r.get("source")})
        xrp = (e.get("reach_potential") or {}).get("score")
        verdict = ""
        if current is not None and \
                e["article_id"] == current["article_id"]:
            verdict = "<== CURRENT RECOMMENDATION"
        else:
            fails = gate_report(br, False, 0.0)  # cooldowns off: raw quality
            if fails:
                verdict = "; ".join(fails)
            else:
                verdict = ("gate-passing challenger: publish %.1f vs "
                           "current %.1f (margin rule +%d needed)"
                           % (br["publish"],
                              current["publish"] if current else 0,
                              CONFIG.MIN_SCORE_IMPROVEMENT))
        print("%-4d %-34s %3d %3d %5.0f %5.0f %5.0f %5.1f %5s %-9s %s"
              % (i, (e["headline"] or "")[:34], len(rows), n_src,
                 br["momentum"], br["freshness"], br["importance"],
                 br["publish"], xrp, e.get("tier", "?"), verdict))

    # --- 3. duplication check ------------------------------------------
    print("\n### 3. DUPLICATION CHECK")
    seen = {}
    for e in s["queue"]:
        seen.setdefault(e["cluster_id"], []).append(e["headline"])
    dup = {c: h for c, h in seen.items() if len(h) > 1}
    print("queue entries: %d, distinct clusters: %d, duplicated clusters: %d"
          % (len(s["queue"]), len(seen), len(dup)))
    for c, h in dup.items():
        print("  DUPLICATE cluster %s: %s" % (c, h))
    print("up_next clusters: %s"
          % [e["cluster_id"] for e in s["up_next"]])
    print("up_next headlines:")
    for e in s["up_next"]:
        print("   - %s" % e["headline"])

    # --- 4. cooldown check ---------------------------------------------
    print("\n### 4. COOLDOWN CHECK")
    posted = db.query(
        "SELECT t.created_at, t.article_id, a.title FROM tweets t "
        "JOIN articles a ON a.id=t.article_id "
        "WHERE t.status='posted' ORDER BY t.created_at DESC LIMIT 10")
    print("recent posted tweets: %d" % len(posted))
    for r in posted:
        print("   %s | %s" % (r["created_at"], (r["title"] or "")[:60]))
    print("cooldown minutes left (global 60): %s"
          % s["cooldown_minutes_left"])
    print("recommendation note: %s" % s["recommendation_note"])
    # which candidates are topic-cooldown suppressed right now
    posted_titles = service._recent_posted_titles(
        db, CONFIG.TOPIC_COOLDOWN_MINUTES, now)
    print("topic-cooldown window posted titles: %s" % posted_titles)
    cl = max(0.0, s["cooldown_minutes_left"])
    suppressed = []
    for art in service._candidate_rows(db):
        rows = service._cluster_rows(db, art)
        br = publish_score(art, cluster_rows=rows, now=now)
        sim = service._similar_to_posted(art.get("title") or "",
                                         posted_titles)
        fails = gate_report(br, sim, cl)
        if any(f.startswith("topic-cooldown") or
               f.startswith("global-cooldown") for f in fails):
            suppressed.append((art["title"], fails))
    print("stories suppressed by a cooldown right now: %d" % len(suppressed))
    for t, f in suppressed[:10]:
        print("   - %s\n       %s" % ((t or "")[:60], "; ".join(f)))

    # --- 5. hashtag audit ----------------------------------------------
    print("\n### 5. HASHTAG AUDIT (top stories)")
    for e in (s["queue"] + ([current] if current else []))[:6]:
        if not e:
            continue
        art = dict(db.article_by_id(e["article_id"]))
        title = art.get("title") or ""
        summ = art.get("summary") or ""
        pool = suggest_hashtags(title, summ,
                                art.get("india_relevance_score") or 0.0)
        sel = service._select_tags(pool)
        print("  story : %s" % title[:60])
        print("  pool  : %s" % pool)
        print("  final : %s  (0-2 policy, generic stripped)" % sel)

    # --- 6. variant audit ----------------------------------------------
    print("\n### 6. VARIANT AUDIT (current recommendation)")
    if current is None:
        print("no current recommendation to audit")
    else:
        art = dict(db.article_by_id(current["article_id"]))
        text, tags, opts = service._build_tweet(db, art)
        share = service.shareability_score(art.get("title") or "",
                                           art.get("summary") or "")
        save = service.save_value_score(art.get("title") or "",
                                        art.get("summary") or "")
        max_facts = max((o["facts"] for o in opts), default=0)
        print("shareability %.0f | save value %.0f | fact floor %d "
              "(max facts %d)"
              % (share, save, service._fact_floor(max_facts), max_facts))
        for o in opts:
            win = " <== SELECTED" if o["text"] == text else ""
            print("  [%-8s] hook %3d facts %d chars %3d obj %3d%s"
                  % (o["style"], o["hook"], o["facts"], o["char_count"],
                     service._variant_objective(o, share, save, max_facts),
                     win))
        print("  chosen tweet (%d chars):" % len(text))
        for line in text.splitlines():
            print("     | %s" % line)
        print("  tags: %s" % tags)

    print("\n(end of audit)")


if __name__ == "__main__":
    main()
