"""SQLite database layer."""
import os
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    normalized_url TEXT NOT NULL,
    url_hash TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    category TEXT,
    country TEXT,
    india_relevance_score REAL DEFAULT 0,
    importance_score REAL DEFAULT 0,
    reliability_score REAL DEFAULT 0,
    published_at TEXT,
    discovered_at TEXT,
    processed_at TEXT,
    status TEXT DEFAULT 'new',
    story_cluster_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_articles_normurl ON articles(normalized_url);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

CREATE TABLE IF NOT EXISTS tweets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    tweet_id TEXT,
    tweet_text TEXT,
    generation_method TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'pending',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    provider TEXT,
    model TEXT,
    estimated_tokens INTEGER DEFAULT 0,
    reason TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    country TEXT,
    category TEXT,
    reliability_score REAL DEFAULT 0.8,
    enabled INTEGER DEFAULT 1,
    poll_interval INTEGER DEFAULT 900
);

CREATE TABLE IF NOT EXISTS story_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    representative_article_id INTEGER,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS pending_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    tweet_text TEXT NOT NULL,
    source TEXT,
    article_url TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS recommendation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    article_id INTEGER NOT NULL,
    publish_score REAL NOT NULL,
    updated_at TEXT
);
"""


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        # safe additive migrations (never destroy existing data)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add columns introduced after launch via ALTER TABLE. Existing
        rows keep their data; new columns default cleanly."""
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(articles)")}
        if "skip_reason" not in cols:
            self.conn.execute(
                "ALTER TABLE articles ADD COLUMN skip_reason TEXT")
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(pending_posts)")}
        if "article_status" not in cols:
            self.conn.execute(
                "ALTER TABLE pending_posts ADD COLUMN article_status TEXT")

    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- articles ---
    def article_by_url(self, url):
        return self.query_one("SELECT * FROM articles WHERE url=?", (url,))

    def article_by_hash(self, url_hash):
        return self.query_one(
            "SELECT id FROM articles WHERE url_hash=? LIMIT 1", (url_hash,))

    def insert_article(self, a):
        cur = self.execute(
            """INSERT OR IGNORE INTO articles
            (url, normalized_url, url_hash, title, summary, source, category,
             country, reliability_score, published_at, discovered_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new')""",
            (a["url"], a["normalized_url"], a["url_hash"], a["title"],
             a.get("summary"), a.get("source"), a.get("category"),
             a.get("country"), a.get("reliability", 0.8),
             a.get("published_at"), utcnow_iso()))
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid
        return None

    def recent_articles(self, hours=48):
        return self.query(
            "SELECT * FROM articles WHERE discovered_at >= datetime('now', ?)",
            ("-%d hours" % hours,))

    def update_scores(self, article_id, india, importance, cluster_id, status):
        self.execute(
            """UPDATE articles SET india_relevance_score=?, importance_score=?,
            story_cluster_id=?, status=?, processed_at=? WHERE id=?""",
            (india, importance, cluster_id, status, utcnow_iso(), article_id))

    # --- clusters ---
    def create_cluster(self, representative_article_id):
        cur = self.execute(
            "INSERT INTO story_clusters (representative_article_id, created_at, updated_at) "
            "VALUES (?,?,?)", (representative_article_id, utcnow_iso(), utcnow_iso()))
        return cur.lastrowid

    # --- tweets ---
    def insert_tweet(self, article_id, text, method, status, tweet_id=None, error=None):
        cur = self.execute(
            """INSERT INTO tweets (article_id, tweet_id, tweet_text,
            generation_method, created_at, status, error_message)
            VALUES (?,?,?,?,?,?,?)""",
            (article_id, tweet_id, text, method, utcnow_iso(), status, error))
        return cur.lastrowid

    def posted_tweets_count(self, since_iso):
        row = self.query_one(
            "SELECT COUNT(*) c FROM tweets WHERE status='posted' AND created_at>=?",
            (since_iso,))
        return row["c"] if row else 0

    # --- pending posts (queued when X API posting is unavailable) ---
    def insert_pending_post(self, article_id, tweet_text, source,
                            article_url):
        """Queue a final validated tweet for later posting. Never
        creates a duplicate pending post for the same article/story."""
        existing = self.query_one(
            "SELECT id FROM pending_posts WHERE article_id=? "
            "AND status='pending'", (article_id,))
        if existing:
            return None
        cur = self.execute(
            "INSERT INTO pending_posts (article_id, tweet_text, source, "
            "article_url, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
            (article_id, tweet_text, source, article_url, utcnow_iso()))
        return cur.lastrowid

    def pending_posts(self):
        return self.query(
            "SELECT * FROM pending_posts WHERE status='pending' "
            "ORDER BY id")

    def oldest_pending_post(self):
        return self.query_one(
            "SELECT * FROM pending_posts WHERE status='pending' "
            "ORDER BY id LIMIT 1")

    def mark_pending_posted(self, pending_id):
        """Mark a pending post as manually published (status='posted').
        Returns True if the row was pending and is now marked."""
        row = self.query_one(
            "SELECT status FROM pending_posts WHERE id=?", (pending_id,))
        if row is None or row["status"] != "pending":
            return False
        self.execute(
            "UPDATE pending_posts SET status='posted' WHERE id=?",
            (pending_id,))
        return True

    # --- dashboard: article state, queue, history ---

    def article_by_id(self, article_id):
        return self.query_one("SELECT * FROM articles WHERE id=?",
                              (article_id,))

    def set_article_status(self, article_id, status):
        self.execute("UPDATE articles SET status=?, processed_at=? "
                     "WHERE id=?", (status, utcnow_iso(), article_id))

    def mark_article_copied(self, article_id):
        """Record that the tweet was copied (does NOT mean posted)."""
        row = self.article_by_id(article_id)
        if row is None or row["status"] in ("posted", "skipped"):
            return False
        self.set_article_status(article_id, "copied")
        return True

    def skip_article(self, article_id, reason=None):
        """Mark a story skipped (never deleted). Returns True if the
        article existed and was not already posted/skipped."""
        row = self.article_by_id(article_id)
        if row is None or row["status"] in ("posted", "skipped"):
            return False
        self.execute(
            "UPDATE articles SET status='skipped', skip_reason=?, "
            "processed_at=? WHERE id=?",
            (reason or "manual", utcnow_iso(), article_id))
        # a skipped story must never come back through the pending queue
        self.execute(
            "UPDATE pending_posts SET status='skipped' "
            "WHERE article_id=? AND status='pending'", (article_id,))
        return True

    def mark_article_posted(self, article_id, tweet_text, source,
                            article_url):
        """Mark a story as manually posted on X. Stores the tweet text,
        source, article URL, timestamp and status='posted'. The story is
        then permanently excluded from candidates."""
        row = self.article_by_id(article_id)
        if row is None:
            return None
        if row["status"] == "posted":
            return None   # already posted — never double-mark
        self.insert_tweet(article_id, tweet_text, "deterministic",
                          "posted", tweet_id=None)
        self.set_article_status(article_id, "posted")
        # clear any pending post for this article
        self.execute(
            "UPDATE pending_posts SET status='posted' "
            "WHERE article_id=? AND status='pending'", (article_id,))
        return article_id

    def posted_history(self, limit=100, q=None, source=None, since=None):
        """Posted tweets (manual + API) newest first, optionally filtered
        by a case-insensitive keyword search over headline/tweet text,
        an exact source, and a minimum timestamp (ISO)."""
        sql = ("SELECT t.id, t.article_id, t.tweet_text, t.created_at, "
               "a.title, a.source, a.normalized_url "
               "FROM tweets t LEFT JOIN articles a ON a.id=t.article_id "
               "WHERE t.status='posted'")
        params = []
        if q:
            sql += " AND (t.tweet_text LIKE ? OR a.title LIKE ? OR a.source LIKE ?)"
            like = "%" + q + "%"
            params = [like, like, like]
        if source:
            sql += " AND a.source = ?"
            params.append(source)
        if since:
            sql += " AND t.created_at >= ?"
            params.append(since)
        sql += " ORDER BY t.created_at DESC, t.id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def pending_history(self, limit=100):
        """Pending posts already published manually on X (kept for the
        history view)."""
        return self.query(
            "SELECT p.id, p.tweet_text, p.created_at, a.title, p.source, "
            "p.article_url FROM pending_posts p "
            "LEFT JOIN articles a ON a.id=p.article_id "
            "WHERE p.status='posted' ORDER BY p.created_at DESC "
            "LIMIT ?", (limit,))

    def skipped_history(self, limit=100, q=None, source=None, since=None):
        """Stories the user skipped, newest first. The article is never
        deleted — only hidden from candidates. Filters: keyword (title/
        source), exact source, and minimum timestamp (ISO)."""
        sql = ("SELECT a.id, a.title, a.source, a.normalized_url, "
               "a.processed_at, a.skip_reason, a.published_at "
               "FROM articles a WHERE a.status='skipped'")
        params = []
        sql, params = self._history_filters(
            sql, params, q=q, source=source, since=since,
            ts_col="a.processed_at", title_col="a.title",
            source_col="a.source")
        sql += " ORDER BY a.processed_at DESC, a.id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def copied_history(self, limit=100, q=None, source=None, since=None):
        """Stories copied to the clipboard but NOT yet marked posted
        (status='copied'). They remain live candidates."""
        sql = ("SELECT a.id, a.title, a.source, a.normalized_url, "
               "a.processed_at, a.published_at "
               "FROM articles a WHERE a.status='copied'")
        params = []
        sql, params = self._history_filters(
            sql, params, q=q, source=source, since=since,
            ts_col="a.processed_at", title_col="a.title",
            source_col="a.source")
        sql += " ORDER BY a.processed_at DESC, a.id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    @staticmethod
    def _history_filters(sql, params, q=None, source=None, since=None,
                         ts_col=None, title_col="a.title",
                         source_col="a.source"):
        """Shared WHERE-clause builder for history queries (keyword,
        source, date-since). All filters are optional and additive."""
        if q:
            sql += (" AND (%s LIKE ? OR %s LIKE ?)"
                    % (title_col, source_col))
            like = "%" + q + "%"
            params.extend([like, like])
        if source:
            sql += " AND %s = ?" % source_col
            params.append(source)
        if since and ts_col:
            sql += " AND %s >= ?" % ts_col
            params.append(since)
        return sql, params

    def unskip_article(self, article_id):
        """Explicitly reconsider a skipped story: back to 'ready' so it
        can reappear as a candidate. Only ever works on a skipped
        article — never on a posted one. Returns True if restored."""
        row = self.article_by_id(article_id)
        if row is None or row["status"] != "skipped":
            return False
        self.execute(
            "UPDATE articles SET status='ready', skip_reason=NULL, "
            "processed_at=? WHERE id=? AND status='skipped'",
            (utcnow_iso(), article_id))
        return True

    # --- recommendation state (dashboard: sticky current story) ---

    def get_recommendation(self):
        """The persisted current recommendation, or None."""
        return self.query_one(
            "SELECT article_id, publish_score FROM recommendation_state "
            "WHERE id=1")

    def set_recommendation(self, article_id, publish_score):
        """Persist (or clear, when article_id is None) the current
        recommendation. Single row — the dashboard has ONE story."""
        self.execute("DELETE FROM recommendation_state WHERE id=1")
        if article_id is None:
            return
        self.execute(
            "INSERT INTO recommendation_state (id, article_id, "
            "publish_score, updated_at) VALUES (1,?,?,?)",
            (article_id, publish_score, utcnow_iso()))

    # --- ai usage ---
    def record_ai_usage(self, provider, model, tokens, reason):
        self.execute(
            "INSERT INTO ai_usage (date, provider, model, estimated_tokens, reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (utcnow_iso()[:10], provider, model, tokens, reason, utcnow_iso()))

    def ai_tokens_today(self):
        row = self.query_one(
            "SELECT COALESCE(SUM(estimated_tokens),0) t FROM ai_usage WHERE date=?",
            (utcnow_iso()[:10],))
        return row["t"] if row else 0

    def close(self):
        self.conn.close()
