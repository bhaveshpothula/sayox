#!/bin/bash
# Stop the bot service (keeps .env, database, and plist installed).
launchctl bootout gui/$(id -u)/com.sayox.newsbot 2>/dev/null || echo "not running"
echo "Bot stopped. Start again with: bash deploy/start.sh"
