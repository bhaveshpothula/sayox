#!/bin/bash
# Preflight + LIVE launch of the Sayox India News Bot via launchd.
# Run from the project directory. Never prints credential values.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
PLIST_SRC="deploy/com.sayox.newsbot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sayox.newsbot.plist"

echo "===== 1/5 TEST SUITE ====="
python3 -m unittest discover tests 2>&1 | tail -3

echo "===== 2/5 X AUTHENTICATION (read-only) ====="
python3 -m app.main --verify-x

echo "===== 3/5 FLIP FLAGS TO LIVE ====="
# flip only the four runtime flags; credentials and everything else untouched
python3 - <<'EOF'
import re
path = ".env"
lines = open(path).read().splitlines()
flags = {"BOT_ENABLED": "true", "DRY_RUN": "false",
         "AUTO_POST": "true", "AI_ENABLED": "false"}
out = []
seen = set()
for ln in lines:
    m = re.match(r"^(BOT_ENABLED|DRY_RUN|AUTO_POST|AI_ENABLED)\s*=", ln)
    if m and m.group(1) in flags:
        out.append("%s=%s" % (m.group(1), flags[m.group(1)]))
        seen.add(m.group(1))
    else:
        out.append(ln)
for k, v in flags.items():
    if k not in seen:
        out.append("%s=%s" % (k, v))
open(path, "w").write("\n".join(out) + "\n")
print("runtime flags set: BOT_ENABLED=true DRY_RUN=false AUTO_POST=true AI_ENABLED=false")
EOF

echo "===== 4/5 INSTALL LAUNCHD SERVICE ====="
mkdir -p data/logs
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootout gui/$(id -u)/com.sayox.newsbot 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST_DST"
echo "service installed and started (KeepAlive, RunAtLoad)"

echo "===== 5/5 STATUS ====="
sleep 3
launchctl print gui/$(id -u)/com.sayox.newsbot 2>/dev/null | grep -E "state|pid" | head -3 || true
echo
echo "Logs:   tail -f data/logs/bot.stdout.log"
echo "Stop:   bash deploy/stop.sh"
echo "Start:  bash deploy/start.sh"
