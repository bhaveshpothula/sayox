"""URL and text normalization."""
import hashlib
import html
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "ref_url", "fbclid", "gclid", "amp", "share_url",
}


def normalize_url(url):
    """Strip scheme variant, tracking params, trailing slash; lowercase host.

    http/https are treated as equivalent (scheme normalized to https)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                       if k.lower() not in _TRACKING_PARAMS])
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit(("https", parts.netloc.lower(), path, query, ""))


def url_hash(normalized_url):
    return hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()


def clean_text(text):
    """HTML-unescape, strip tags, collapse whitespace."""
    if not text:
        return ""
    text = html.unescape(text)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def strip_source_suffix(title):
    """Remove trailing ' - Source' / ' | Source' commonly added by feeds."""
    return re.sub(r"\s*[-|–]\s*[A-Z][\w\s&.]{2,30}$", "", title).strip()


_STOPWORDS = {
    "the", "a", "an", "in", "on", "of", "for", "to", "and", "is", "are",
    "was", "were", "at", "as", "by", "with", "from", "after", "over",
    "into", "amid", "vs", "amidst", "his", "her", "its", "their", "this",
    "that", "new", "says", "said", "will", "be", "not", "no", "it",
}


def tokens(text):
    """Lowercase alpha tokens minus stopwords."""
    return [t for t in re.findall(r"[a-z]+", (text or "").lower())
            if t not in _STOPWORDS and len(t) > 2]


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
