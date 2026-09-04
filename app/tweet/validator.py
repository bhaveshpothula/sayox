"""Tweet validation: length, filler, hashtag cap, rate limits.

Public tweets no longer contain the article URL; the URL requirement is
enforced internally by the generator (has_valid_source_url) before a
tweet is ever built, and the URL stays in the database for tracking.
"""
from app.tweet.generator import contains_filler, effective_length


def validate_tweet(text, char_limit=280, source_text=None):
    """Return (ok, reason)."""
    if not text or not text.strip():
        return False, "empty"
    # hashtag-cap policy is checked first: a policy violation must be
    # reported as such even if the text is also short
    if text.count("#") > 2:
        return False, "hashtag_spam"
    if len(text.strip()) < 20:
        return False, "too_short"
    if effective_length(text) > char_limit:
        return False, "too_long:%d" % effective_length(text)
    if text.count("#") > 2:
        return False, "hashtag_spam"
    if contains_filler(text, source_text or ""):
        return False, "bot_filler_phrase"
    if "http://" in text or "https://" in text:
        return False, "url_in_public_tweet"
    return True, ""


def check_rate_limits(db, max_per_hour, max_per_day):
    """Return (ok, reason) against posted-tweet history."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    hour_ago = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if db.posted_tweets_count(hour_ago) >= max_per_hour:
        return False, "hourly_limit"
    if db.posted_tweets_count(day_ago) >= max_per_day:
        return False, "daily_limit"
    return True, ""
