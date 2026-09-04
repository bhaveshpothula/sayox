#!/bin/bash
# Quick production status: process, runtime flags, last cycles, published tweets.
cd "$(dirname "$0")/.."
echo "== process =="
launchctl print gui/$(id -u)/com.sayox.newsbot 2>/dev/null | grep -E "state|pid" | head -3 || echo "service not loaded"
echo
echo "== runtime flags (.env) =="
grep -E "^(BOT_ENABLED|DRY_RUN|AUTO_POST|AI_ENABLED|MAX_TWEETS_PER_HOUR|MAX_TWEETS_PER_DAY|POLL_INTERVAL_SECONDS)" .env
echo
echo "== last 5 log lines =="
tail -5 data/logs/bot.stdout.log 2>/dev/null || echo "(no log yet)"
echo
echo "== published tweets (last 24h) =="
python3 - <<'EOF'
import sqlite3
from datetime import datetime, timedelta, timezone
since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
conn = sqlite3.connect("data/newsbot.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT t.created_at, t.tweet_id, t.status, a.url FROM tweets t "
    "LEFT JOIN articles a ON t.article_id=a.id WHERE t.created_at>=? "
    "ORDER BY t.id DESC LIMIT 10", (since,)).fetchall()
for r in rows:
    print(r["created_at"], r["status"], r["tweet_id"], (r["url"] or "")[:60])
if not rows:
    print("(none yet)")
EOF
