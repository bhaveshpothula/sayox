#!/bin/bash
# Start (or restart) the bot service after it has been installed.
set -e
cd "$(dirname "$0")/.."
launchctl bootout gui/$(id -u)/com.sayox.newsbot 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.sayox.newsbot.plist"
sleep 2
launchctl print gui/$(id -u)/com.sayox.newsbot 2>/dev/null | grep -E "state|pid" | head -3
echo "Bot started."
