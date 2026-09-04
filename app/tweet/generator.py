"""Deterministic newsroom-style tweet generation — zero AI.

Public tweet format (no URL in the text):

    {catchy but factual headline}

    • {context/development point}
    • {second detail point}
    • {0-3 more optional points}

    Source: {source name}

    {0-2 relevant hashtags}

Rules:
- The article's stored normalized URL is REQUIRED internally (a story
  without one is not eligible for posting) but never appears in the tweet.
- Headline and every bullet point come verbatim from the source title/
  summary — no invented facts, no bot filler, uncertainty words preserved.
- Points are never padded: a thin source simply yields fewer points.
- Labels (BREAKING/Developing/Update) must be earned by the source's own
  wording; most tweets have none.
- Character limit is enforced on the raw text (no URL to shorten).
"""
import re

from app.tweet.templates import (BREAKING_MAJOR, DEVELOPING_MARKERS,
                                 FILLER_PHRASES, HASHTAG_RULES, TCO_LENGTH)

_ELLIPSIS = "…"

# label-style prefixes feeds prepend to headlines (ours or generic)
_PREFIX_RE = re.compile(
    r"^\s*(breaking news|breaking|just in|developing|update|exclusive)\s*[:\-–]\s*",
    re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

MAX_SUMMARY_SENTENCES = 5
MAX_SUMMARY_CHARS = 200
MAX_HEADLINE = 140
# There is NO per-bullet character limit: a source sentence of any
# length may be kept, and during final construction a sentence is kept
# ONLY if the COMPLETE sentence fits the remaining tweet budget —
# otherwise the whole sentence is dropped, never truncated.
BULLET = "•"

# leading discourse markers that make a point read like raw RSS copy
_LEAD_MARKER_RE = re.compile(
    r"^(however|meanwhile|also|but|in addition|moreover|furthermore|"
    r"additionally)\s*[,:]?\s+", re.IGNORECASE)

# phrases that add no information; a point made only of these is filler
_GENERIC_POINT_RE = re.compile(
    r"^(this comes amid|more details are awaited|the situation is "
    r"developing|watch this space|stay tuned|details soon"
    r"|this is a developing story"
    r"|authorities (?:are|were) monitoring the situation"
    r"|the situation remains developing"
    r"|(?:officials|authorities) (?:are|were) (?:reviewing|assessing) "
    r"the situation)\b", re.IGNORECASE)

# Initial/acronym sequences ("D.K.", "A.K.", "U.S.", "U.K.", "J.") and
# common abbreviations are not sentence ends. Only periods inside such
# tokens are masked — normal capitalized words like "ISRO." still split.
_INITIAL_SEQ = re.compile(r"(?<![A-Za-z])(?:[A-Za-z]\.)+")
_COMMON_ABBREV = re.compile(
    r"(?<![A-Za-z])(?:Mr|Mrs|Ms|Dr|Prof|St|Sr|Jr|vs|etc|Inc|Ltd|Co)\.(?=\s)",
    re.IGNORECASE)


def _mask_abbreviations(text):
    """Replace abbreviation-internal periods with a placeholder so sentence
    splitting can't fire on them; reversible via _unmask()."""
    def _mask(m):
        return m.group(0).replace(".", "\x00")
    text = _INITIAL_SEQ.sub(_mask, text)
    return _COMMON_ABBREV.sub(_mask, text)


def _unmask(text):
    return text.replace("\x00", ".")


def clean_headline(title):
    """Light formatting cleanup only — never reword the source."""
    h = _WS_RE.sub(" ", (title or "")).strip()
    h = _PREFIX_RE.sub("", h)
    h = re.sub(r"\s+([,.;:!?])", r"\1", h)
    return h.strip()


def _no_ellipsis(text):
    """Remove ALL truncation ellipses ('...', '…') from text. An ellipsis
    in raw source text must never leak into a public tweet; removal adds
    no words, it only drops the marker."""
    return _WS_RE.sub(" ", re.sub(r"(?:\.{3}|…)+", " ", text or "")).strip()


_ELLIPSIS_TOKEN = "\x01"


def _split_sentences(text):
    """Split into sentences, honoring abbreviation masking. Truncation
    ellipses are masked to a placeholder FIRST so the '.' inside '...'
    can never act as a sentence boundary — otherwise a leading
    '... fragment' would split into a bare '...' plus a clean-looking
    (but incomplete) sentence, letting the fragment's text survive."""
    cleaned = re.sub(r"(?:\.{3}|…)+", _ELLIPSIS_TOKEN,
                     _WS_RE.sub(" ", text).strip())
    masked = _mask_abbreviations(cleaned)
    parts = re.split(r"(?<=[.!?])\s+", masked)
    return [_unmask(p).strip() for p in parts if p.strip()]


def context_sentences(summary, max_sentences=MAX_SUMMARY_SENTENCES,
                      max_chars=MAX_SUMMARY_CHARS):
    """1-2 verbatim source sentences of context, length-capped as a whole.
    Weak stories are never padded — fewer sentences are simply fewer."""
    if not summary or not summary.strip():
        return ""
    sentences = _split_sentences(summary)
    chosen = []
    total = 0
    for s in sentences[:max_sentences]:
        if total + len(s) + 1 > max_chars:
            break
        chosen.append(s)
        total += len(s) + 1
    if not chosen and sentences:
        # single overlong sentence: cap at a word boundary
        chosen.append(truncate_at_word(sentences[0], max_chars))
    return " ".join(chosen)


def choose_label(headline, summary, importance_score):
    """Return 'BREAKING', 'Developing', 'Update', or None.

    The label must be earned by the source's own wording — never by
    recency or a raw importance score alone. Words like "live",
    "latest" or "updates" never trigger it. For BREAKING, the headline
    itself must claim urgency ("breaking"/"just in"), the claim must be
    backed by a major-event keyword, and importance must be high.
    """
    headline_l = (headline or "").strip().lower()
    text = ("%s %s" % (headline, summary)).lower()

    if "breaking" in headline_l or "just in" in headline_l:
        if importance_score >= 0.60 and any(k in text for k in BREAKING_MAJOR):
            return "BREAKING"

    if any(m in text for m in DEVELOPING_MARKERS):
        return "Developing"

    # "Update:" only when the source frames it as a follow-up to a big story
    if re.match(r"(?i)^update\s*[:\-]", headline.strip()) and importance_score >= 0.60:
        return "Update"

    return None


def pick_hashtags(title, summary, max_tags=2):
    """0-2 specific, story-derived hashtags. No generic tags, no side tags."""
    text = ("%s %s" % (title or "", summary or "")).lower()
    tags = []
    for keywords, tag_group in HASHTAG_RULES:
        if len(tags) >= max_tags:
            break
        if any(k in text for k in keywords):
            for tag in tag_group:
                if tag not in tags and len(tags) < max_tags:
                    tags.append(tag)
    return tags


def effective_length(text, url=None):
    """Length as X counts it: a URL (if present) counts as TCO_LENGTH.
    Tweets no longer contain URLs, so this equals len(text)."""
    if url and url in text:
        return len(text) - len(url) + TCO_LENGTH
    return len(text)


# Words that must never dangle at the end of a truncated headline/context
# (e.g. "Dies After 2", "announced plans for") — stripped after a cut.
_DANGLING = {
    "after", "of", "in", "on", "for", "to", "and", "the", "a", "an", "at",
    "by", "with", "from", "as", "over", "into", "amid", "vs", "or",
}


def truncate_at_word(text, limit):
    """Truncate on a word boundary and strip dangling prepositions/particles
    so headlines never end like 'Dies After 2' or 'plans for'."""
    if len(text) <= limit:
        return trim_dangling(text)
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    words = cut.split()
    # strip dangling prepositions/articles and bare numbers ("After 2")
    while words and (words[-1].lower() in _DANGLING or
                     words[-1].strip(".,;:").isdigit()):
        words.pop()
    result = " ".join(words).rstrip(" ,;:-")
    return result + _ELLIPSIS if result else text[: limit - 1] + _ELLIPSIS


def trim_dangling(text):
    """Strip a trailing dangling word/number fragment WITHOUT adding an
    ellipsis. Applied to every headline — even one that already fits —
    because RSS titles themselves often arrive pre-truncated by the feed
    (e.g. '...Dies After 2')."""
    words = text.split()
    while words and (words[-1].lower() in _DANGLING or
                     words[-1].strip(".,;:!?").isdigit()):
        words.pop()
    return " ".join(words).rstrip(" ,;:-")


# "2-month battle", "two week ordeal", "10 day" … — a trailing timeframe
# phrase a feed-truncated headline may have cut off mid-way.
_UNIT_TAIL_RE = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[- ]?(month|day|week|year|hour|minute)s?(?:[- ]([a-z]+))?", re.IGNORECASE)
_NUM_WORDS = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
              "6": "six", "7": "seven", "8": "eight", "9": "nine",
              "10": "ten", "11": "eleven", "12": "twelve"}
_DANGLING_TAIL_RE = re.compile(r"^(?:(\w+)\s+)?(\d+)$", re.IGNORECASE)


def repair_headline(headline, summary, extra_texts=()):
    """Repair a feed-pre-truncated headline. Returns (headline, malformed).

    If the title ends in a dangling fragment like 'After 2', the article's
    own summary — and then the same-story cluster siblings' text — is
    searched for the matching timeframe phrase (e.g. '2-month battle') and
    the tail is completed with that VERBATIM source text — never invented.
    If no completion exists, the dangling fragment is simply dropped."""
    trimmed = trim_dangling(headline)
    if trimmed == headline:
        return headline, False
    m = _DANGLING_TAIL_RE.match(headline[len(trimmed):].strip())
    if not m:
        return trimmed, True
    lead, num = m.group(1) or "", m.group(2)
    wanted = {num.lower(), _NUM_WORDS.get(num, "").lower()}
    for text in (summary,) + tuple(extra_texts):
        if not text:
            continue
        for um in _UNIT_TAIL_RE.finditer(text):
            if um.group(1).lower() in wanted:
                completion = um.group(0)
                return ("%s %s %s" % (trimmed, lead, completion) if lead
                        else "%s %s" % (trimmed, completion)), True
    return trimmed, True


def cut_clean(text, limit):
    """Word-boundary cut WITHOUT an ellipsis, with dangling-word
    stripping. Public tweets never contain '…' or '...' — if a headline
    must be cut (rare last resort), it ends on a clean word."""
    if len(text) <= limit:
        return _strip_ellipsis(text)
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    words = cut.split()
    while words and (words[-1].lower() in _DANGLING or
                     words[-1].strip(".,;:!?").isdigit()):
        words.pop()
    return " ".join(words).rstrip(" ,;:-…")


def shorten_headline(headline, limit):
    """Shorten a headline at a natural boundary, best option first:
    1. drop a leading pre-colon clause ("Assaulted, Tied To Tree,
       Burned: Punjab Influencer Dies After 2-Month Battle" ->
       "Punjab Influencer Dies After 2-Month Battle");
    2. word-boundary truncation with dangling-word stripping.
    Never leaves a dangling number/preposition/conjunction/article."""
    if len(headline) <= limit:
        return _strip_ellipsis(headline)
    # try dropping a leading clause before a colon when what follows is
    # a substantive, self-contained headline
    if ":" in headline:
        head, _, tail = headline.partition(":")
        tail = tail.strip()
        if len(tail.split()) >= 4 and len(tail) <= limit:
            return _strip_ellipsis(trim_dangling(tail))
    return cut_clean(headline, limit)


def _assemble(label, headline, points, source, hashtags=()):
    lead = ("%s: %s" % (label, headline)) if label else headline
    bullets = "".join("\n%s %s" % (BULLET, p) for p in points)
    tags = list(hashtags) if hashtags else []
    tag_block = ("\n\n%s" % " ".join(tags)) if tags else ""
    return ("%s%s\n\nSource: %s%s" % (lead, bullets, source, tag_block)).strip()


def _strip_ellipsis(text):
    """Remove leading/trailing truncation ellipses ('...', '…') so raw
    source-text ellipses can never leak into a public tweet."""
    return re.sub(r"^[\s.…]+|[\s.…]+$", "", text or "").strip()


def _is_complete_sentence(s):
    """A candidate point must be a complete grounded source sentence:
    it ends in terminal punctuation and doesn't start mid-thought
    (lowercase/ellipsized fragments are RSS continuation crumbs)."""
    s = s.strip()
    if not s or not s[0].isupper():
        return False
    return s[-1] in ".!?"


def _strip_headline_label(point, headline):
    """Feeds repeat the story label inside summary sentences ('Nepal
    Flash Flood LIVE Updates: A glacier collapse…'). If a point starts
    with a label whose words are all in the headline, drop the label and
    keep the complementary fact. Returns the point minus the label, or
    None if nothing substantive remains."""
    if ":" not in point:
        return point
    label, _, rest = point.partition(":")
    label_words = [w for w in re.findall(r"[a-z]+", label.lower()) if len(w) > 2]
    hwords = set(re.findall(r"[a-z]+", (headline or "").lower()))
    if label_words and all(w in hwords for w in label_words) and len(rest.strip()) >= 15:
        return rest.strip()
    return point


def build_points(summary, max_points=5):
    """Turn the article's own summary into 0-5 COMPLETE bullet points.

    Every point is a complete, verbatim source sentence of ANY length —
    there is no per-point character cap and no truncation. Sentences
    that cannot fit whole in the final tweet budget are dropped later
    during selection, never cut. Leading discourse markers ("However,",
    "Meanwhile,"), leading ellipses and repeated story labels are
    stripped. Filler sentences, duplicates, and mid-thought fragments
    are rejected — a fragment is never doctored into a sentence.
    NOTHING is invented to reach a point count."""
    if not summary or not summary.strip():
        return []
    sentences = _split_sentences(summary)
    pieces = []
    for raw in sentences:
        # a sentence/fragment the SOURCE begins with an ellipsis (kept
        # intact as a masked marker by _split_sentences) or a slash
        # continuation is a truncation continuation — the ENTIRE fragment
        # is dropped; its remainder is never published, because it is
        # not a complete standalone sentence from the beginning
        if raw.startswith(_ELLIPSIS_TOKEN) or raw.startswith(".../"):
            continue
        # an internal ellipsis marks a truncation boundary inside the
        # source sentence — each side is judged on its own completeness
        parts = [p.strip() for p in raw.split(_ELLIPSIS_TOKEN)]
        pieces.extend(p for p in parts if p)
    points = []
    for raw in pieces:
        if len(points) >= max_points:
            break
        s = _LEAD_MARKER_RE.sub("", raw.strip())
        if s and s[0].islower():
            s = s[0].upper() + s[1:]   # re-capitalize after marker strip
        if len(s) < 15 or _GENERIC_POINT_RE.match(s.strip()):
            continue
        if not _is_complete_sentence(s):
            continue          # fragment — drop, never invent wording
        if any(s == p for p in points):
            continue
        points.append(s)
    return points


def tokens_lite(text):
    return [t for t in re.findall(r"[a-z]+", (text or "").lower()) if len(t) > 3]


_POINT_PRIORITY_RE = re.compile(r"\d|[₹$€£]")

_NAMED_RE = re.compile(r"\b[A-Z][a-z]+\b")


def _point_score(point):
    """Rank factual usefulness — signals, never requirements. Numbers,
    named entities and action verbs help, but so do consequences,
    causes, context, responses and status changes: 'The collapse cut off
    several villages from the main highway' is strong news with no
    number or name in it."""
    score = 0.0
    if _POINT_PRIORITY_RE.search(point):
        score += 2.0                       # concrete numbers/amounts
    score += 1.5 * min(len(_NAMED_RE.findall(point)), 3)   # named entities
    if re.search(_FACT_SIGNAL_RE, point):
        score += 1.5                       # concrete development/action
    if re.search(_CONSEQUENCE_SIGNAL_RE, point):
        score += 1.5                       # consequence/cause/response
    score -= len(point) / 400.0            # mildly prefer tighter points
    return score


# direct factual developments, decisions and actions
_FACT_SIGNAL_RE = re.compile(
    r"\b(announced|said|confirmed|arrested|killed|injured|filed|"
    r"registered|banned|approved|launched|resigned|won|lost|died|"
    r"hospitali[sz]ed|rescued|deployed|suspend\w+|investigat\w+|"
    r"recover\w+|miss\w+|declared|warned|evacuat\w+|held|met|signed|"
    r"passed|rejected|ordered)\b", re.IGNORECASE)

# consequences, causes, responses and status changes — concrete
# developments, not process filler ("monitoring the situation" is
# deliberately NOT here; generic watchwords stay weak)
_CONSEQUENCE_SIGNAL_RE = re.compile(
    r"\b(disrupt\w+|cut off|blocked|stranded|isolat\w+|"
    r"affected|triggered|caused|led to|resulted in|forced|closure|"
    r"collapsed|damaged|destroyed|submerged|washed away|connectivity|"
    r"restored|resumed|opened|reopened|shut down|halted|cancelled|"
    r"canceled|diverted|delayed|displaced|rendered homeless|"
    r"respond\w+|react\w+|appeal\w+|sought|demanded|protest\w+|"
    r"critici[sz]ed|welcomed|condoled|relief|aid|assistance)\b",
    re.IGNORECASE)

# quality threshold: a point below this information-value bar is never
# selected — not even to fill a third slot. Any single real signal
# (number, named entity, concrete development, consequence, response)
# clears it.
_USEFULNESS_BAR = 1.0

# the final/latest-development point gets this small priority bonus. It
# only breaks close calls (it can displace a point whose intrinsic value
# is within this margin) — it can never override substantially better
# information.
_FINAL_POINT_BONUS = 0.5


def _select_points(label, headline, candidates, source, char_limit,
                   hashtags=()):
    """Select the highest-value COMPLETE facts that fit the full tweet
    budget. A point is kept whole or dropped — never truncated.

    Every candidate gets an information-value score (numbers, named
    entities, concrete developments, consequences, causes, responses —
    signals, never requirements). Only points that clear the quality bar
    are eligible at all: 2 excellent facts beat 3 mediocre ones, and a
    weak point is never chosen merely to reach a target count. Among
    eligible points, the strongest that fit are selected — 4-5 bullets
    appear only when each is independently useful and fits. The final
    candidate (latest development) gets a small tie-breaking bonus so
    the briefing ends on the newest fact; the bonus cannot override
    quality. Nothing is fabricated to raise the count."""
    if not candidates:
        return [], _assemble(label, headline, [], source, list(hashtags))

    def intrinsic(i):
        return _point_score(candidates[i])

    # quality gate — no unconditional slots, no filler to reach a count
    eligible = [i for i in range(len(candidates))
                if intrinsic(i) >= _USEFULNESS_BAR]
    if not eligible:
        # nothing clears the bar: keep the single best point only if it
        # carries at least one signal; otherwise the headline stands
        best = max(range(len(candidates)), key=intrinsic)
        eligible = [best] if intrinsic(best) > 0.0 else []

    final_i = len(candidates) - 1
    ranked = sorted(
        eligible,
        key=lambda i: intrinsic(i)
                      + (_FINAL_POINT_BONUS if i == final_i else 0.0),
        reverse=True)

    chosen = []
    for i in ranked[:5]:
        trial = sorted(chosen + [i])
        pts = [candidates[j] for j in trial]
        if len(_assemble(label, headline, pts, source, list(hashtags))) \
                <= char_limit:
            chosen = trial
    # display order: strongest facts first; the final candidate (latest
    # development) closes the briefing when selected
    final_i = len(candidates) - 1
    ordered = [i for i in sorted(chosen, key=intrinsic, reverse=True)
               if i != final_i]
    if final_i in chosen:
        ordered.append(final_i)
    pts = [candidates[j] for j in ordered]
    text = _assemble(label, headline, pts, source, list(hashtags))
    # if even the best single point doesn't fit with hashtags, retry
    # without them
    if len(text) > char_limit and hashtags:
        return _select_points(label, headline, candidates, source,
                              char_limit, hashtags=())
    return pts, text


# decorative live-blog labels feeds put in front of the real news
# ("Nepal Flash Flood LIVE Updates: 734 Bodies Recovered…")
_LABEL_PREFIX_RE = re.compile(
    r"^.{0,60}?(?:live updates?|updates?|live blog|highlights|as it "
    r"happens|live coverage)\s*[:\-–]\s*(.+)$", re.IGNORECASE)


def _merge_sibling_points(own, siblings, max_points=8, headline=""):
    """Top up a thin article with same-story cluster siblings' sentences
    (other outlets' coverage of the identical event, already clustered by
    the pipeline). Sibling candidates pass the SAME acceptance filters as
    the article's own points: headline-label stripping, duplicate
    rejection, and headline-restatement rejection. Every added point is
    still verbatim published source text — nothing invented. When the
    whole cluster yields only 1-2 independent facts, that is what is
    published — never padded."""
    if not siblings:
        return own
    points = list(own)
    seen = " ".join(points).lower()
    for sib in siblings:
        if len(points) >= max_points:
            break
        for cand in build_points(sib):
            if len(points) >= max_points:
                break
            p = _strip_headline_label(cand, headline)
            lm = _LABEL_PREFIX_RE.match(p)
            if lm and len(lm.group(1).split()) >= 3:
                p = lm.group(1).strip()
            if len(p) < 15:
                continue
            if p.lower() in seen or p[:30].lower() in seen:
                continue
            toks = set(tokens_lite(p))
            htoks = set(tokens_lite(headline))
            if toks and htoks and len(toks & htoks) / len(toks) > 0.6:
                continue
            if (headline or "").lower()[:40] in p.lower():
                continue
            points.append(p)
            seen += " " + p.lower()
    return points


def _drop_order(points):
    """Order in which points are dropped when the tweet is over budget:
    middle points first (from the end of the middle backwards), keeping
    the first detail and — longest — the final point, which carries the
    latest development / ending."""
    n = len(points)
    if n <= 2:
        return list(range(n))
    order = list(range(1, n - 1))[::-1]   # middle points, last-middle first
    order.append(0)                        # then the first point
    # the final point is kept until everything else is gone
    return order


def has_valid_source_url(article):
    """Internal eligibility: a story needs a stored, valid source URL
    (kept in the DB for tracking/dedup/audit) before it may be posted."""
    url = (article.get("normalized_url") or article.get("url") or "").strip()
    return url.startswith(("http://", "https://"))


def prepare_story(article, importance_score=0.0, cluster_texts=None):
    """Shared story preparation for the tweet generator and the tweet
    options builder: eligibility check, headline repair, label choice
    and the verified pool of verbatim source facts (title-recovered +
    the article's own summary + same-story sibling top-up). Returns a
    dict {raw_title, summary, label, headline, points, source}, or None
    when the story is not eligible (no internal URL / no usable
    headline). Nothing is invented here — every point is published
    source text, and callers only select and assemble.

    cluster_texts: title+summary strings of same-story cluster siblings —
    used ONLY to repair a feed-truncated headline with verbatim source
    text (deterministic, no AI, nothing invented)."""
    # requirement: stored URL must exist internally, but is never published
    if not has_valid_source_url(article):
        return None

    raw_title = article.get("title") or ""
    summary_text = article.get("summary") or ""
    siblings = tuple(cluster_texts or ())
    clean = _no_ellipsis(clean_headline(raw_title))
    headline, malformed = repair_headline(clean, summary_text, siblings)
    extra_points = []
    if malformed and ":" in clean:
        # a malformed title's pre-colon clause ("Assaulted, Tied To
        # Tree, Burned:") is decoration as a headline — but it carries
        # real facts, so it becomes the first bullet instead of being lost
        pre, _, tail = clean.partition(":")
        tail_fixed, _ = repair_headline(tail.strip(), summary_text, siblings)
        if len(tail_fixed.split()) >= 4:
            headline = tail_fixed
            pre = trim_dangling(pre.strip(" ,;:-…"))
            if len(pre.split()) >= 3 and pre.lower() not in headline.lower():
                extra_points.append(_strip_ellipsis(pre.rstrip(".,;:")))
    if not headline:
        headline, _ = repair_headline(raw_title.strip(), summary_text,
                                      siblings)
    if not headline:
        return None
    # drop decorative live-blog labels from the headline when the title
    # carries a substantive main clause after the colon ("Nepal Flash
    # Flood LIVE Updates: 734 Bodies Recovered…" -> "734 Bodies
    # Recovered…"). The label's own words (location/topic) stay available
    # as bullet facts; the headline should be the main development.
    m = _LABEL_PREFIX_RE.match(headline)
    if m and len(m.group(1).split()) >= 4:
        headline = m.group(1).strip()
    source = article.get("source") or "News"
    label = choose_label(raw_title, summary_text, importance_score)

    # points: facts recovered from the title + the article's own summary,
    # topped up from same-story sibling coverage only if the article
    # itself is too thin — never padded beyond what sources published
    points = list(extra_points)
    seen = " ".join(points).lower()
    headline_words = set(re.findall(r"[a-z]+", headline.lower()))

    def _accept(p):
        """Accept a candidate point unless it (substantially) repeats the
        headline — feeds repeat the story label inside summary sentences,
        so the label is stripped and the complementary fact kept."""
        p = _strip_headline_label(p, headline)
        # live-blog labels inside summary sentences are dropped the same
        # way they are from the headline
        lm = _LABEL_PREFIX_RE.match(p)
        if lm and len(lm.group(1).split()) >= 3:
            p = lm.group(1).strip()
        if len(p) < 15:
            return None
        if p.lower().rstrip(".") == headline.lower().rstrip("."):
            return None          # the headline itself, verbatim
        if p.lower() in seen or p[:30].lower() in seen:
            return None
        # token overlap with the headline => same information, not new
        toks = set(tokens_lite(p))
        if headline_words and toks and \
                len(toks & headline_words) / len(toks) > 0.6:
            return None
        if headline_l and headline_l[:40] in p.lower():
            return None
        return p

    headline_l = headline.lower()
    for cand in build_points(summary_text):
        p = _accept(cand)
        if p:
            points.append(p)
            seen += " " + p.lower()
    points = _merge_sibling_points(points, siblings, headline=headline)

    return {"raw_title": raw_title, "summary": summary_text, "label": label,
            "headline": headline, "points": points, "source": source}


def generate_tweet(article, india_score=0.0, importance_score=0.0,
                   char_limit=280, cluster_texts=None):
    """Build a deterministic newsroom-style tweet, or None if the story is
    not eligible for posting (missing internal source URL / headline).

    cluster_texts: title+summary strings of same-story cluster siblings —
    used ONLY to repair a feed-truncated headline with verbatim source
    text (deterministic, no AI, nothing invented)."""
    parts = prepare_story(article, importance_score, cluster_texts)
    if parts is None:
        return None
    label = parts["label"]
    headline = parts["headline"]
    source = parts["source"]
    context = context_sentences(parts["summary"])
    hashtags = pick_hashtags(headline, context)

    def fits(text):
        return len(text) <= char_limit

    # 1-2) select the strongest COMPLETE points that fit the full budget
    # (points are kept whole or dropped — never truncated with '...')
    pool = parts["points"]
    final_headline = headline
    points, text = _select_points(label, final_headline, pool, source,
                                  char_limit)
    # 3) if the headline squeezes points out, try a shorter headline
    #    (a decorative pre-colon clause dropped) and re-select — the news
    #    points must not lose space to headline decoration. The headline
    #    is the primary hook, so it is only shortened to reach a fuller
    #    briefing (up to 4 points), never for a marginal 5th bullet.
    if len(points) < 4 and len(pool) > len(points):
        shorter = shorten_headline(headline, len(headline) // 2)
        if shorter != headline and len(shorter) < len(headline):
            alt_points, alt_text = _select_points(
                label, shorter, pool, source, char_limit)
            if len(alt_points) > len(points):
                final_headline, points, text = shorter, alt_points, alt_text
    # 4) hashtags only if the complete news post still fits with them
    if hashtags and fits(text):
        with_tags = _assemble(label, final_headline, points, source,
                              hashtags)
        if fits(with_tags):
            text = with_tags
    # 5) last resort: headline + source only (never an ellipsis)
    if not fits(text):
        text = _assemble(label, final_headline, [], source, [])
        if not fits(text):
            trimmed = cut_clean(final_headline,
                                char_limit - len(source) - 12)
            text = _assemble(label, trimmed, [], source, [])
            if not fits(text) or not trimmed:
                return None
    # public tweets contain ZERO truncation ellipses anywhere; internal
    # source ellipses are scrubbed (marker removed, words unchanged)
    if "…" in text or "..." in text:
        text = _no_ellipsis(text)
        if "…" in text or "..." in text:
            return None
    return text


def tweet_options(article, importance_score=0.0, char_limit=280,
                  cluster_texts=None):
    """Up to three deterministic shape-variants of the same story —
    identical verbatim facts, different briefing depth:
      'briefing' — full headline + every point that fits
      'punchy'   — shortened headline, only the strongest points
      'flash'    — headline + source only
    Each variant is a complete, valid tweet on its own. Hashtags are
    NOT added here — the 0-1 tag policy is the dashboard's decision,
    applied afterwards. Returns [{style, text, hook, facts}] scored by
    the ranking engine's hook potential, or [] when the story is not
    eligible."""
    from app.news.ranking import hook_score   # local: keep import lazy
    parts = prepare_story(article, importance_score, cluster_texts)
    if parts is None:
        return []
    label = parts["label"]
    headline = parts["headline"]
    source = parts["source"]
    pool = parts["points"]

    options = []

    def _add(style, text, points, head):
        if not text or len(text) > char_limit:
            return
        lead = ("%s: %s" % (label, head)) if label else head
        options.append({
            "style": style, "text": text,
            "hook": hook_score(lead, " ".join(points)),
            "facts": len(points)})

    # 1) briefing — the full newsroom treatment
    points, text = _select_points(label, headline, pool, source, char_limit)
    _add("briefing", text, points, headline)

    # 2) punchy — shorter lead, only the strongest facts
    short = shorten_headline(headline, max(40, len(headline) // 2))
    if short != headline and len(short.split()) >= 3:
        top = sorted(pool, key=_point_score, reverse=True)[:2]
        pts, txt = _select_points(label, short, top, source, char_limit)
        _add("punchy", txt, pts, short)

    # 3) flash — the hook alone
    _add("flash", _assemble(label, headline, [], source), [], headline)

    # thin stories can produce identical variants — keep each text once
    seen, unique = set(), []
    for opt in options:
        if opt["text"] not in seen:
            seen.add(opt["text"])
            unique.append(opt)
    return unique


def contains_filler(text, source_text=""):
    """True if the tweet contains bot filler phrases the source never used."""
    low = text.lower()
    src = (source_text or "").lower()
    for phrase in FILLER_PHRASES:
        if phrase in low and phrase not in src:
            return True
    return False
