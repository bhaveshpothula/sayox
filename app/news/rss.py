"""RSS fetching via feedparser + httpx."""
import time

import feedparser
import httpx

from app.logger import get_logger

log = get_logger("rss")


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0 Safari/537.36")


def _get(url, timeout, user_agent, _retries=1):
    """GET with resilience:
    - one retry after a short pause (transient DNS/connect blips, the
      classic false '[Errno 8] nodename nor servname' on macOS), and
    - a final attempt with trust_env=False so a stale/broken proxy
      environment variable cannot break every request.
    """
    last_exc = None
    for attempt in range(_retries + 1):
        try:
            return httpx.get(url, timeout=timeout, follow_redirects=True,
                             headers={"User-Agent": user_agent})
        except httpx.ConnectError as e:
            last_exc = e
            log.warning("connect error (attempt %d) for %s: %s",
                        attempt + 1, url, e)
            if attempt < _retries:
                time.sleep(1.5)
    # all direct attempts failed — bypass proxy env configuration entirely
    with httpx.Client(trust_env=False, timeout=timeout,
                      follow_redirects=True) as client:
        return client.get(url, headers={"User-Agent": user_agent})


def fetch_feed(source, timeout=20, user_agent="IndiaNewsBot/1.0"):
    """Fetch and parse one feed. Returns list of entry dicts.
    Retries once with a browser UA on 403 (some gov feeds block bots)."""
    url = source["url"]
    try:
        resp = _get(url, timeout, user_agent)
        if resp.status_code == 403:
            resp = _get(url, timeout, _BROWSER_UA)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        cause = getattr(e, "__cause__", None)
        log.warning("feed error %s: %s: %s%s", url, type(e).__name__, e,
                    (" (caused by %s: %s)" % (type(cause).__name__, cause)
                     if cause else ""))
        return []

    entries = []
    for e in parsed.entries:
        link = e.get("link")
        title = e.get("title")
        if not link or not title:
            continue
        published = None
        for key in ("published", "updated"):
            if e.get(key):
                published = e[key]
                break
        entries.append({
            "url": link.strip(),
            "title": title.strip(),
            "summary": (e.get("summary") or "").strip(),
            "published_at": published,
            "source": source["name"],
            "source_country": source["country"],
            "category": source["category"],
            "reliability": source["reliability_score"],
        })
    return entries
