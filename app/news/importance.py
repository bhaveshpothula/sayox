"""Importance scoring — deterministic, keyword-tiered."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_BREAKING = [
    "breaking", "bREAKING".lower(), "just in", "huge", "massive",
    " major ", "announces", "announced", "launches", "unveils", "wins",
    "dies", "dead", "killed", "earthquake", "flood", "cyclone", "stampede",
    "blast", "attack", "crash", "resigns", "resignation", "verdict",
    "bans", "ban on", "raid", "arrest", "arrested", "acquit", "convict",
    "sentenced", "hikes", "cuts", "repo rate", "budget", "election results",
    "results", "record high", "record low", "all-time",
]
_HIGH = [
    "rbi", "sebi", "supreme court", "high court", "cabinet", "parliament",
    "prime minister", "president", "chief minister", "election commission",
    "isro", "chandrayaan", "gaganyaun", "gaganyaan", "nasa", "fda",
    "supreme", "gdp", "inflation", "repo", "policy rate", "monetary policy",
    "mpc", "ipo", "merger", "acquisition", "acquires", "layoff", "layoffs",
    "championship", "world cup", "olympics", "medal", "nobel",
    "genome", "vaccine", "breakthrough", "discovery",
]
_MEDIUM = [
    "study", "report", "survey", "research", "startup", "funding",
    "launch", "update", "upgrade", "expansion", "investment", "moU".lower(),
    "policy", "bill", "committee", "probe", "investigation", "review",
    "quarterly", "earnings", "profit", "loss", "growth", "exports",
]
_LOW = [  # penalty terms
    "opinion", "editorial", "sponsored", "promoted", "poll:", "quiz",
    "lifestyle", "horoscope", "recipe", "top 10", "best of", "how to",
    "5 things", "explained:", "watch:", "viral", "memes",
]


def parse_published(published):
    """Parse an RSS date string to an aware UTC datetime, or None."""
    if not published:
        return None
    try:
        dt = parsedate_to_datetime(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def score_importance(title, summary, reliability=0.8, published_at=None):
    """Return score in [0, 1]."""
    text = (" %s %s " % (title or "", summary or "")).lower()

    score = 0.10  # base
    for kw in _BREAKING:
        if kw.strip() in text:
            score += 0.12
    for kw in _HIGH:
        if kw in text:
            score += 0.10
    for kw in _MEDIUM:
        if kw in text:
            score += 0.05
    for kw in _LOW:
        if kw in text:
            score -= 0.15

    # source reliability is a multiplier, not additive noise
    score *= (0.6 + 0.5 * max(0.0, min(1.0, reliability)))

    # recency bonus: < 6h old gets a boost, > 48h decays
    dt = parse_published(published_at)
    if dt:
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if age_h < 0:
            age_h = 0
        if age_h <= 6:
            score += 0.10
        elif age_h > 48:
            score -= 0.10
        elif age_h > 24:
            score -= 0.05

    return max(0.0, min(1.0, score))
