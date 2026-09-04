"""CLI entry point.

    python -m app.main --once --dry-run
    python -m app.main --serve
"""
import argparse
import subprocess
import time

from app.config import CONFIG
from app.database import Database
from app.logger import get_logger
from app.news.collector import collect_and_process, select_stories
from app.news.sources import ensure_sources
from app.tweet.generator import generate_tweet
from app.tweet.validator import check_rate_limits, validate_tweet
from app.x.client import XClient

log = get_logger("main", log_dir=CONFIG.DATA_DIR)

# bounded retry for transient X failures only; auth/permission/rate-limit
# errors are permanent for this cycle and are never blindly retried
_POST_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2


def _transient_post_failure(result):
    """Network errors and 5xx server errors are worth retrying; 401/403
    (credentials/permissions) and 429 (rate limit) are not."""
    return result.error == "network_error" or (result.status or 0) >= 500


def _post_with_retry(client, text, log):
    """POST a tweet with bounded backoff. Returns the final XResult."""
    result = None
    for attempt in range(1, _POST_ATTEMPTS + 1):
        result = client.post_tweet(text)
        if result.ok or not _transient_post_failure(result):
            return result
        if attempt < _POST_ATTEMPTS:
            log.warning("transient X post failure (%s, attempt %d/%d) — "
                        "retrying", result.error, attempt, _POST_ATTEMPTS)
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    return result


def run_cycle(db, dry_run=True, force=False):
    if not CONFIG.BOT_ENABLED and not dry_run:
        log.warning("BOT_ENABLED=false — refusing to run live cycle (kill switch)")
        return []
    if not force and not dry_run and not CONFIG.AUTO_POST:
        log.warning("AUTO_POST=false — refusing to post")
        return []

    log.info("=== cycle start (dry_run=%s) ===", dry_run)
    stats = collect_and_process(db)
    log.info("sources=%s discovered=%s duplicates=%s clustered=%s clusters=%s",
             stats.get("sources", 0), stats.get("discovered", 0),
             stats.get("duplicates", 0), stats.get("clustered", 0),
             stats.get("clusters", 0))

    ok, reason = check_rate_limits(db, CONFIG.MAX_TWEETS_PER_HOUR,
                                   CONFIG.MAX_TWEETS_PER_DAY)
    if not ok:
        log.warning("rate limit hit: %s", reason)
        return []

    stories = select_stories(db, CONFIG.MAX_TWEETS_PER_RUN)
    client = XClient(CONFIG.X_API_KEY, CONFIG.X_API_SECRET,
                     CONFIG.X_ACCESS_TOKEN, CONFIG.X_ACCESS_TOKEN_SECRET)
    results = []
    for a in stories:
        a = dict(a)
        # same-story cluster siblings: their verbatim title+summary text
        # is used ONLY to repair a feed-truncated headline
        cluster_texts = []
        if a.get("story_cluster_id"):
            for sib in db.query(
                    "SELECT title, summary FROM articles "
                    "WHERE story_cluster_id=? AND id<>?",
                    (a["story_cluster_id"], a["id"])):
                cluster_texts.append(
                    "%s %s" % (sib["title"] or "", sib["summary"] or ""))
        tweet = generate_tweet(a, a["india_relevance_score"],
                               a["importance_score"],
                               char_limit=CONFIG.TWEET_CHAR_LIMIT,
                               cluster_texts=cluster_texts)
        if tweet is None:
            log.warning("no valid stored URL for article %s — invalid for posting", a["id"])
            db.update_scores(a["id"], a["india_relevance_score"],
                             a["importance_score"], a["story_cluster_id"],
                             "invalid_no_url")
            continue
        valid, vreason = validate_tweet(
            tweet, CONFIG.TWEET_CHAR_LIMIT,
            source_text=(a.get("title") or "") + " " + (a.get("summary") or ""))
        if not valid:
            log.warning("invalid tweet for article %s: %s", a["id"], vreason)
            db.update_scores(a["id"], a["india_relevance_score"],
                             a["importance_score"], a["story_cluster_id"],
                             "rejected")
            continue

        log.info("ARTICLE %s | india=%.2f imp=%.2f src=%s | %s", a["id"],
                 a["india_relevance_score"], a["importance_score"], a["source"],
                 a.get("normalized_url"))
        log.info("RAW TITLE: %s", a.get("title"))
        log.info("RAW SUMMARY: %s", (a.get("summary") or "")[:200])
        log.info("TWEET (deterministic, 0 AI calls, %d chars):\n%s\n---",
                 len(tweet), tweet)

        if dry_run:
            db.insert_tweet(a["id"], tweet, "deterministic", "dry_run")
            db.update_scores(a["id"], a["india_relevance_score"],
                             a["importance_score"], a["story_cluster_id"],
                             "tweeted_dry_run")
            results.append((a, tweet, "dry_run"))
        else:
            if not client.configured:
                missing = ", ".join(client.missing_fields())
                log.error("X credentials missing (%s) — cannot post", missing)
                db.insert_tweet(a["id"], tweet, "deterministic", "failed",
                                error="not_configured")
                continue
            result = _post_with_retry(client, tweet, log)
            if result.ok and result.data.get("id"):
                db.insert_tweet(a["id"], tweet, "deterministic", "posted",
                                tweet_id=result.data["id"])
                db.update_scores(a["id"], a["india_relevance_score"],
                                 a["importance_score"],
                                 a["story_cluster_id"], "tweeted")
                log.info("X API OK (HTTP %s): posted tweet id=%s url=%s",
                         result.status, result.data["id"],
                         "https://x.com/SayoxHQ/status/%s"
                         % result.data["id"])
                results.append((a, tweet, "posted"))
            else:
                if result.error == "payment_required":
                    # X API HTTP 402: developer account has no credits.
                    # The tweet is NOT discarded and the story is NOT
                    # marked tweeted — it is queued as a pending post
                    # for manual posting / later retry.
                    db.insert_tweet(a["id"], tweet, "deterministic",
                                    "failed", error="payment_required")
                    pid = db.insert_pending_post(
                        a["id"], tweet, a.get("source"),
                        a.get("normalized_url") or a.get("url"))
                    db.update_scores(a["id"], a["india_relevance_score"],
                                     a["importance_score"],
                                     a["story_cluster_id"], "pending_post")
                    if pid:
                        log.warning("X API 402 (no credits) — tweet saved "
                                    "as pending post #%d (article %s); "
                                    "story NOT marked tweeted", pid, a["id"])
                    else:
                        log.warning("X API 402 (no credits) — pending post "
                                    "for article %s already queued",
                                    a["id"])
                    continue
                # the story stays retryable: a failed tweet row never
                # marks the article tweeted and never blocks its story
                db.insert_tweet(a["id"], tweet, "deterministic", "failed",
                                error=result.error)
                log.error("post failed: %s", result.error)
                if result.error == "rate_limited" and result.retry_after:
                    log.warning("rate limited; stopping this cycle "
                                "(retry after %ss)", result.retry_after)
                    break
    log.info("=== cycle end: %d tweets ===", len(results))
    return results


def print_pending_posts(db):
    """Print queued tweets (X API posting unavailable) in a clean
    copy-paste format: the tweet text verbatim, one per block."""
    rows = db.pending_posts()
    if not rows:
        print("No pending posts.")
        return
    for r in rows:
        print("--- pending post #%d | %s | %s | %s ---" % (
            r["id"], r["created_at"], r["source"] or "unknown source",
            r["article_url"] or "(no url)"))
        print(r["tweet_text"])
        print()
    print("%d pending post(s). Post them manually on X, then clear "
          "them once posted." % len(rows))


def publish_next(db):
    """Manual publish workflow (zero X API calls): show the OLDEST
    pending tweet, copy its text to the macOS clipboard, and open the
    X compose page in the browser. The pending post is NOT marked
    posted and NOT deleted — that happens only via --mark-posted after
    the human has actually posted it."""
    row = db.oldest_pending_post()
    if row is None:
        print("No pending posts.")
        return 1
    print("--- pending post #%d | %s | %s | %s ---" % (
        row["id"], row["created_at"], row["source"] or "unknown source",
        row["article_url"] or "(no url)"))
    print()
    print(row["tweet_text"])
    print()
    try:
        subprocess.run(["pbcopy"],
                       input=row["tweet_text"].encode("utf-8"),
                       check=True)
        copied = True
    except Exception as e:
        copied = False
        print("Clipboard copy failed (%s) — copy the tweet text above "
              "manually." % type(e).__name__)
    try:
        subprocess.run(["open", "https://x.com/compose/post"], check=True)
    except Exception:
        print("Open https://x.com/compose/post in your browser.")
    print()
    print("The tweet %s and ready to paste on X (⌘V in the compose "
          "box)." % ("is copied to your clipboard" if copied
                     else "is shown above"))
    print("After you post it manually, run: "
          "python3 -m app.main --mark-posted %d" % row["id"])
    return 0


def mark_posted(db, pending_id):
    """Mark a pending post as manually published (after the human has
    posted it on X). Only changes the pending_posts row status."""
    if db.mark_pending_posted(pending_id):
        print("Pending post #%d marked as posted." % pending_id)
        return 0
    row = db.query_one("SELECT status FROM pending_posts WHERE id=?",
                       (pending_id,))
    if row is None:
        print("Pending post #%d not found." % pending_id)
    else:
        print("Pending post #%d is already marked '%s'."
              % (pending_id, row["status"]))
    return 1


def main():
    ap = argparse.ArgumentParser(description="India-first X news bot")
    ap.add_argument("--once", action="store_true", help="run one cycle")
    ap.add_argument("--serve", action="store_true", help="run continuously")
    ap.add_argument("--dry-run", action="store_true", default=None)
    ap.add_argument("--force", action="store_true",
                    help="bypass AUTO_POST check (still respects kill switch)")
    ap.add_argument("--verify-x", action="store_true",
                    help="verify X credentials and show the authenticated "
                         "account (makes one GET /2/users/me call; "
                         "never posts)")
    ap.add_argument("--pending", action="store_true",
                    help="print queued tweets awaiting posting (e.g. "
                         "saved when the X API returned HTTP 402) in "
                         "copy-paste format; never posts")
    ap.add_argument("--publish-next", action="store_true",
                    help="manual publish workflow: show the oldest "
                         "pending tweet, copy it to the clipboard "
                         "(pbcopy) and open x.com/compose/post; never "
                         "calls the X API and never marks the post")
    ap.add_argument("--mark-posted", type=int, metavar="PENDING_ID",
                    help="mark a pending post as manually published "
                         "(run this only after you have posted it on X)")
    ap.add_argument("--dashboard", action="store_true",
                    help="launch the local NEWS-TO-X publishing dashboard "
                         "(manual posting: copy + open X + one-click "
                         "logging; never calls the X posting API)")
    ap.add_argument("--port", type=int, default=8300,
                    help="dashboard port (default 8300)")
    args = ap.parse_args()

    dry_run = CONFIG.DRY_RUN if args.dry_run is None else args.dry_run
    db = Database(CONFIG.DB_PATH)
    ensure_sources(db)

    if args.pending:
        print_pending_posts(db)
    elif args.publish_next:
        publish_next(db)
    elif args.mark_posted is not None:
        mark_posted(db, args.mark_posted)
    elif args.verify_x:
        client = XClient(CONFIG.X_API_KEY, CONFIG.X_API_SECRET,
                         CONFIG.X_ACCESS_TOKEN, CONFIG.X_ACCESS_TOKEN_SECRET)
        status = client.credential_status()
        for var, is_set in status.items():
            log.info("%s: %s", var, "set" if is_set else "MISSING")
        if not client.configured:
            log.error("X verification aborted — missing: %s",
                      ", ".join(client.missing_fields()))
            log.info("Fill these in .env (see .env.example), then re-run "
                     "with --verify-x. This command is read-only and never posts.")
        else:
            # read-only: a single GET /2/users/me — cannot post/like/follow
            result = client.verify_me()
            if result.ok:
                d = result.data
                log.info("X authentication OK: @%s (%s)",
                         d.get("username"), d.get("name"))
            else:
                log.error("X authentication failed: %s", result.error)
    elif args.dashboard:
        from app.dashboard.web import run as run_dashboard
        url = "http://127.0.0.1:%d" % args.port
        log.info("dashboard: %s — manual posting mode (no X API posting)",
                 url)
        try:
            subprocess.Popen(["open", url])
        except Exception:
            pass   # user can open the URL manually
        run_dashboard(db, host="127.0.0.1", port=args.port)
    elif args.once:
        run_cycle(db, dry_run=dry_run, force=args.force)
    elif args.serve:
        log.info("serving, interval=%ds", CONFIG.POLL_INTERVAL_SECONDS)
        while True:
            try:
                run_cycle(db, dry_run=dry_run, force=args.force)
            except Exception as e:
                log.exception("cycle failed: %s", e)
            time.sleep(CONFIG.POLL_INTERVAL_SECONDS)
    else:
        ap.print_help()
    db.close()


if __name__ == "__main__":
    main()
