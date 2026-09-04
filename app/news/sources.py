"""Verified default sources; runtime list lives in the sources table."""
from app.database import utcnow_iso

DEFAULT_SOURCES = [
    # name, url, country, category, reliability
    ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", "IN", "national", 0.95),
    ("Indian Express", "https://indianexpress.com/feed/", "IN", "national", 0.85),
    ("Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "IN", "national", 0.80),
    ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "IN", "top", 0.75),
    ("NDTV", "https://feeds.feedburner.com/ndtvnews-india-news", "IN", "national", 0.80),
    ("Economic Times", "https://www.economictimes.indiatimes.com/rssfeedstopstories.cms", "IN", "business", 0.80),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml", "IN", "business", 0.75),
    ("PIB", "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", "IN", "government", 1.0),
    ("RBI", "https://rbi.org.in/Scripts/UnionRSS.aspx", "IN", "regulation", 1.0),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "GLOBAL", "world", 0.95),
    ("The Guardian", "https://www.theguardian.com/world/rss", "GLOBAL", "world", 0.90),
    ("TechCrunch", "https://techcrunch.com/feed/", "GLOBAL", "tech", 0.85),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "GLOBAL", "tech", 0.85),
]


def ensure_sources(db):
    """Seed sources table from defaults; existing rows are left untouched."""
    for name, url, country, category, rel in DEFAULT_SOURCES:
        db.execute(
            """INSERT OR IGNORE INTO sources (name, url, country, category,
            reliability_score, enabled, poll_interval) VALUES (?,?,?,?,?,1,900)""",
            (name, url, country, category, rel))


def enabled_sources(db):
    return db.query("SELECT * FROM sources WHERE enabled=1")
