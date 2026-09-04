"""Read-only RSS fetch diagnostic. Makes only GET requests; posts nothing.
Prints proxy-env presence as booleans and exception details (no secrets)."""
import os
import socket
import traceback

import httpx

print("== proxy env (booleans only) ==")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy", "NO_PROXY"):
    print(k, "set:", bool(os.environ.get(k)))

print("== DNS ==")
for host in ("www.ndtv.com", "feeds.feedburner.com", "www.theguardian.com"):
    try:
        print(host, "->", socket.gethostbyname(host))
    except Exception as e:
        print(host, "FAILED:", e)

URL = "https://feeds.feedburner.com/ndtvnews-india-news"

print("== plain httpx.get (bot UA) ==")
try:
    r = httpx.get(URL, timeout=10, follow_redirects=True,
                  headers={"User-Agent": "IndiaNewsBot/1.0"})
    print("status:", r.status_code, "bytes:", len(r.content))
except Exception:
    traceback.print_exc()

print("== plain httpx.get (trust_env=False) ==")
try:
    with httpx.Client(trust_env=False, timeout=10, follow_redirects=True) as c:
        r = c.get(URL, headers={"User-Agent": "IndiaNewsBot/1.0"})
        print("status:", r.status_code, "bytes:", len(r.content))
except Exception:
    traceback.print_exc()

print("== app fetch_feed ==")
from app.news.rss import fetch_feed  # noqa: E402

try:
    rows = fetch_feed({"url": URL, "name": "NDTV", "country": "IN",
                       "category": "national", "reliability_score": 0.8})
    print("entries:", len(rows))
    if rows:
        print("first title:", rows[0]["title"][:80])
except Exception:
    traceback.print_exc()
