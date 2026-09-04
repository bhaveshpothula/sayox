"""Load configuration from environment / .env with safe defaults."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _bool(key, default):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self):
        # Safety kill switches (safe defaults)
        self.BOT_ENABLED = _bool("BOT_ENABLED", False)
        self.DRY_RUN = _bool("DRY_RUN", True)
        self.AUTO_POST = _bool("AUTO_POST", False)
        self.AI_ENABLED = _bool("AI_ENABLED", False)

        # Rate limits
        self.MAX_TWEETS_PER_HOUR = _int("MAX_TWEETS_PER_HOUR", 5)
        self.MAX_TWEETS_PER_DAY = _int("MAX_TWEETS_PER_DAY", 50)

        # AI budget
        self.AI_DAILY_LIMIT = _int("AI_DAILY_LIMIT", 20)

        # Pipeline tuning
        self.DUP_TITLE_THRESHOLD = 0.75
        self.CLUSTER_TITLE_THRESHOLD = 0.45
        self.INDIA_MIN_SCORE = 0.35      # below this, article treated as global
        self.MIN_IMPORTANCE = 0.45       # below this, never selected
        self.INDIA_TARGET_SHARE = 0.50   # soft target: ~50% India / ~50% global,
                                         # balanced over time, never forced per batch
        self.MAX_TWEETS_PER_RUN = _int("MAX_TWEETS_PER_RUN", 3)
        self.TWEET_CHAR_LIMIT = 280
        # feed entries older than this are never ingested as news
        # (unparseable/missing timestamps are kept — never discard fresh
        # content just because a feed omits its date)
        self.MAX_ARTICLE_AGE_HOURS = _int("MAX_ARTICLE_AGE_HOURS", 48)

        # --- publishing workflow (dashboard): fewer, stronger posts ---
        # after a post is marked posted, NO new recommendation is offered
        # until this cooldown expires (a breaking story may override)
        self.NEWS_COOLDOWN_MINUTES = _int("NEWS_COOLDOWN_MINUTES", 60)
        # a story about a topic we just posted about is suppressed for
        # this long (unless a major new development overrides)
        self.TOPIC_COOLDOWN_MINUTES = _int("TOPIC_COOLDOWN_MINUTES", 180)
        # quality gate: stories below this publish score are never
        # recommended ("No story worth posting right now" is acceptable)
        self.MIN_PUBLISH_SCORE = _int("MIN_PUBLISH_SCORE", 70)
        # two-tier quality gate: besides the overall publish bar, an
        # ordinary story must be CONFIRMED (momentum — roughly 2+
        # independent outlets) and genuinely important. A breaking story
        # (all three 90-bars) always clears these floors, so the
        # breaking override needs no exemption.
        self.MIN_STORY_MOMENTUM = _int("MIN_STORY_MOMENTUM", 50)
        self.MIN_STORY_IMPORTANCE = _int("MIN_STORY_IMPORTANCE", 80)
        # descriptive tier thresholds (display only — NEVER gates; the
        # authoritative floors are MIN_PUBLISH_SCORE / MIN_STORY_*).
        # Trending = multi-source momentum (roughly 3+ independent
        # outlets with acceleration) while still fresh.
        self.TRENDING_MOMENTUM = _int("TRENDING_MOMENTUM", 75)
        self.TRENDING_FRESHNESS = _int("TRENDING_FRESHNESS", 60)
        # XRP tiebreak margin: a challenger inside the hysteresis score
        # margin may displace the incumbent only when its ADVISORY
        # reach potential is at least this much higher. XRP never
        # gates a story and never claims to predict impressions.
        self.XRP_CHALLENGE_MARGIN = _int("XRP_CHALLENGE_MARGIN", 15)
        # a new story must beat the current recommendation by at least
        # this much (publish-score points) to replace it
        self.MIN_SCORE_IMPROVEMENT = _int("MIN_SCORE_IMPROVEMENT", 10)
        # X account context — DISPLAY NOTE ONLY, never a ranking
        # multiplier. Premium would amplify every post equally, so it
        # cannot change WHICH story is worth posting. "premium" is set
        # only when the user explicitly states the account has Premium;
        # the default assumes nothing.
        self.ACCOUNT_PREMIUM = os.environ.get(
            "ACCOUNT_PREMIUM", "unknown").strip().lower()
        # advisory saturation window: how far back SAYOX looks for its
        # OWN posts about a topic (reach-potential penalty only — the
        # authoritative topic cooldown stays TOPIC_COOLDOWN_MINUTES)
        self.SATURATION_WINDOW_HOURS = _int("SATURATION_WINDOW_HOURS", 48)
        # comma-separated local-time windows when posting is encouraged
        # (display guidance only — breaking stories are never blocked)
        self.POSTING_WINDOWS = os.environ.get(
            "POSTING_WINDOWS", "08:00-10:00,12:00-14:00,17:00-20:00")
        # breaking-news override during the global cooldown: ALL THREE
        # signals must clear the bar (0-100 scale)
        self.BREAKING_IMPORTANCE = _int("BREAKING_IMPORTANCE", 90)
        self.BREAKING_MOMENTUM = _int("BREAKING_MOMENTUM", 90)
        self.BREAKING_FRESHNESS = _int("BREAKING_FRESHNESS", 90)
        # a topic-cooldown-blocked story is still allowed when it is a
        # major NEW development (importance + freshness bars, 0-100)
        self.MAJOR_DEV_IMPORTANCE = _int("MAJOR_DEV_IMPORTANCE", 90)
        self.MAJOR_DEV_FRESHNESS = _int("MAJOR_DEV_FRESHNESS", 90)
        # hashtag policy: 0-2 highly relevant hashtags per tweet. The
        # first tag is the strongest specific topical/entity match; a
        # second tag is allowed ONLY when it names the story's own
        # location (e.g. #Earthquake #Delhi). Generic filler (#India,
        # #News, #BreakingNews) never fills the quota — zero or one tag
        # is a fine outcome. This caps the FINAL selection; the
        # candidate pool the dashboard narrows from is wider.
        self.MAX_HASHTAGS = _int("MAX_HASHTAGS", 2)

        # Paths
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.DATA_DIR = os.environ.get("DATA_DIR", os.path.join(base, "data"))
        self.DB_PATH = os.environ.get("DB_PATH", os.path.join(self.DATA_DIR, "newsbot.db"))

        # X API credentials (never logged)
        self.X_API_KEY = os.environ.get("X_API_KEY", "")
        self.X_API_SECRET = os.environ.get("X_API_SECRET", "")
        self.X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
        self.X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

        # Scheduler
        self.POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 900)
        # dashboard background scanner: one scan every N seconds
        # (fixed-delay: next scan is a full interval after the previous
        # scan COMPLETES, so slow feeds never stack up)
        self.AUTO_SCAN_INTERVAL_SECONDS = _int("AUTO_SCAN_INTERVAL_SECONDS", 120)

        # Network
        self.HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 20)
        self.USER_AGENT = "IndiaNewsBot/1.0 (+https://github.com/indianewsbot)"

        os.makedirs(self.DATA_DIR, exist_ok=True)


CONFIG = Config()
