"""Ranking engine: turn the existing scoring signals into a single
REACH SCORE (0-100) with a human-readable breakdown for the dashboard's
"Why this story?" panel.

Reuses the stored india_relevance_score / importance_score /
reliability_score and the story's cluster size (existing coverage =
topic momentum). Deterministic, zero AI, zero network."""
import re
from datetime import datetime, timezone

from app.news.importance import parse_published


def _parse_dt(value):
    """Parse a stored published_at/discovered_at value: ISO-8601
    (what our tests and feeds store) or RFC-822 (raw RSS dates).
    Returns an aware UTC datetime or None."""
    if not value:
        return None
    dt = parse_published(value)          # RFC-822 path
    if dt is not None:
        return dt
    for candidate in (value.strip(), value.strip()[:19]):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
        if dt is not None:
            break
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# tolerance on the 1-hour freshness boundary: stored published_at values
# are truncated to whole seconds (and clocks skew slightly), so a story
# published "exactly" an hour ago parses a few seconds older than 1h —
# it must still count as fully fresh
_FRESH_TOLERANCE_H = 5.0 / 3600.0


def _freshness(published_at, discovered_at, now=None):
    """100 = published within the last hour; decays to 0 at 48h.
    Falls back to discovered_at when the feed gave no date."""
    dt = _parse_dt(published_at)
    if dt is None:
        dt = _parse_dt(discovered_at)
    if dt is None:
        return 50.0   # unknown age — neutral
    now = now or datetime.now(timezone.utc)
    age_h = max(0.0, (now - dt).total_seconds() / 3600)
    if age_h <= 1.0 + _FRESH_TOLERANCE_H:
        return 100.0
    if age_h >= 48:
        return 0.0
    return round(100.0 * (1.0 - age_h / 48.0), 1)


def _trending(cluster_size):
    """Topic momentum from existing coverage: one outlet = 20, five or
    more outlets covering the same event = 100."""
    if cluster_size <= 1:
        return 20.0
    return round(min(100.0, 20.0 + (cluster_size - 1) * 20.0), 1)


def reach_score(article, cluster_size=1, now=None):
    """Return dict with each signal (0-100) and the weighted final
    reach score. Weights: importance 30%, India relevance 25%,
    freshness 20%, trending/coverage 15%, source quality 10%."""
    freshness = _freshness(article.get("published_at"),
                           article.get("discovered_at"), now)
    trending = _trending(cluster_size)
    india = round(100.0 * float(article.get("india_relevance_score") or 0))
    importance = round(100.0 * float(article.get("importance_score") or 0))
    source_quality = round(100.0 *
                           float(article.get("reliability_score") or 0.8))
    reach = round(
        0.30 * importance +
        0.25 * india +
        0.20 * freshness +
        0.15 * trending +
        0.10 * source_quality, 1)
    return {
        "freshness": freshness,
        "india": india,
        "importance": importance,
        "trending": trending,
        "source_quality": source_quality,
        "reach": reach,
    }


def why_this(breakdown):
    """One-line human explanation of the selection."""
    parts = sorted(
        [("India relevance", breakdown["india"]),
         ("Importance", breakdown["importance"]),
         ("Trending potential", breakdown["trending"]),
         ("Freshness", breakdown["freshness"]),
         ("Source quality", breakdown["source_quality"])],
        key=lambda p: p[1], reverse=True)
    top = ", ".join("%s %d" % (name, val) for name, val in parts[:3])
    return ("Selected for: %s — final reach score %d"
            % (top, breakdown["reach"]))


# ============================================================================
# PUBLISH SCORE — the dashboard's selective-editor recommendation engine.
#
# A separate 0-100 score that answers "is this the ONE story worth
# posting right now?", built from legitimate news signals only (no
# fabricated X trend data):
#   momentum 20%, freshness 15%, importance 15%, conversation 15%,
#   hook 15%, source quality 10%, visual 5%, uniqueness 5%.
# It ranks editorial priority only — it is NOT a prediction of
# impressions or engagement.
# ============================================================================

PUBLISH_WEIGHTS = {
    "momentum": 0.20, "freshness": 0.15, "importance": 0.15,
    "conversation": 0.15, "hook": 0.15, "source_quality": 0.10,
    "visual": 0.05, "uniqueness": 0.05,
}


def momentum_score(cluster_rows, now=None):
    """News momentum from the collected coverage itself (0-100).

    cluster_rows: the article rows of the same-story cluster (the
    candidate included). Signals: number of independent sources, major
    publishers, how rapidly coverage is appearing, and how recently the
    story first broke. Returns (score, reasons)."""
    now = now or datetime.now(timezone.utc)
    rows = list(cluster_rows or [])
    if not rows:
        return 0.0, ["no coverage data"]

    sources = {r.get("source") for r in rows if r.get("source")}
    n_sources = max(1, len(sources))

    ages_h = []
    recent_count = 0
    for r in rows:
        dt = _parse_dt(r.get("published_at")) or \
            _parse_dt(r.get("discovered_at"))
        if dt is None:
            continue
        age_h = max(0.0, (now - dt).total_seconds() / 3600)
        ages_h.append(age_h)
        if age_h <= 2:
            recent_count += 1
    newest_age_h = min(ages_h) if ages_h else None

    # one outlet is a baseline 40; each additional independent outlet
    # adds 12.5 up to +50 (5+ outlets saturate); accelerating coverage
    # (several articles in the last 2h) adds up to +20
    score = 40.0 + min(50.0, (n_sources - 1) * 12.5)
    score += min(20.0, max(0, recent_count - 1) * 10.0)
    if newest_age_h is not None:
        if newest_age_h <= 0.5:
            score += 8.0
        elif newest_age_h <= 2:
            score += 4.0

    reasons = []
    if n_sources == 1:
        reasons.append("1 independent report")
    else:
        reasons.append("%d independent reports" % n_sources)
    if newest_age_h is not None:
        if newest_age_h <= 1:
            mins = max(1, int(round(newest_age_h * 60)))
            reasons.append("first reported %d minutes ago" % mins)
        else:
            reasons.append("first reported %.0f hours ago" % newest_age_h)
    if recent_count >= 3:
        reasons.append("coverage increasing rapidly")
    elif recent_count == 2:
        reasons.append("coverage building")

    return round(min(100.0, score), 1), reasons


# strong first-line action verbs: a hook that leads with what HAPPENED
_HOOK_VERB_RE = re.compile(
    r"\b(kills?|killed|dies?|dead|death|cuts?|hikes?|slashes?|bans?|"
    r"launches?|unveils?|resigns?|wins?|loses?|defeats?|seized?|seizes?|"
    r"arrests?|arrested|collapses?|strikes?|striking|hits?|slams?|"
    r"approves?|rejects?|approv\w+|verdict|acquitted|convicted|sentenced|"
    r"rescued?|evacuat\w+|rescues?|attacks?|invades?|bombs?|explodes?|"
    r"collides?|crashes?|sweeps?|breaches?|record\w*)\b", re.IGNORECASE)

# weak intros that bury the lead — a hook must not start like this
_WEAK_INTRO_RE = re.compile(
    r"^\s*(according to (reports|sources)|in a recent development|"
    r"officials (said|stated)|reports (say|suggest)|it (is|has been) "
    r"reported|sources (said|told))\b", re.IGNORECASE)

_NUMBER_RE = re.compile(r"(\d+([.,]\d+)?%?|[₹$€£]\s?\d+|\b\d{1,3}(,\d{3})+\b)")
_ENTITY_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")

_CONSEQUENCE_HINT_RE = re.compile(
    r"\b(cut off|stranded|displaced|homeless|evacuated|injured|hospitali"
    r"[sz]ed|killed|dead|missing|rescued|restored|suspended|cancelled|"
    r"canceled|disrupted|collapsed|destroyed|damaged|submerged|cheaper|"
    r"costlier|jobs|prices)\b", re.IGNORECASE)

_IMPACT_RE = re.compile(
    r"\b(people|families|villages|cities|commuters|passengers|students|"
    r"patients|farmers|workers|homeowners|borrowers|travellers|"
    r"travelers|consumers|taxpayers)\b", re.IGNORECASE)


def hook_score(headline, summary=""):
    """How strong a first line the story supports (0-100).

    Higher when the lead can carry a concrete number, a major
    # consequence, a strong action verb or a named entity; penalized
    for weak intros ("According to reports…") that bury the lead."""
    lead = (headline or "").strip()
    text = "%s %s" % (lead, summary or "")
    score = 20.0   # neutral baseline: a clean factual headline
    if _NUMBER_RE.search(lead):
        score += 30.0
    elif _NUMBER_RE.search(text or ""):
        score += 15.0
    if len(_ENTITY_RE.findall(lead)) >= 1:
        score += 15.0
    if len(_ENTITY_RE.findall(lead)) >= 2:
        score += 5.0
    if _HOOK_VERB_RE.search(lead):
        score += 20.0
    if _CONSEQUENCE_HINT_RE.search(text or ""):
        score += 10.0
    if _WEAK_INTRO_RE.match(lead):
        score -= 40.0
    return round(max(0.0, min(100.0, score)), 1)


# conversation drivers: policy consequences, public impact, economy,
# major decisions, sport, tech, controversy, geopolitics (0-100)
_CONVERSATION_RULES = [
    (re.compile(r"\b(policy|bill|law|legislation|regulation|ban|"
                r"decree|ordinance|amendment|act)\b", re.I), 25.0),
    (re.compile(r"\b(repo rate|inflation|gdp|budget|tax|gst|prices|"
                r"rupee|markets?|economy|jobs?|wages?|pension|loan|"
                r"fuel|interest)\b", re.I), 25.0),
    (re.compile(r"\b(attack|war|invasion|missile|strike|border|"
                r"ceasefire|sanctions?|summit|talks?|treaty|"
                r"geopolitic\w*)\b", re.I), 25.0),
    (re.compile(r"\b(election|verdict|convicted|acquitted|resigns?|"
                r"resignation|cabinet|parliament|minister|no-?confidence|"
                r"probe|corruption|scam|alleged|accused)\b", re.I), 20.0),
    (re.compile(r"\b(killed|dead|injured|evacuated|flood|cyclone|"
                r"earthquake|landslide|rescue|disaster|hospital|"
                r"outbreak|epidemic)\b", re.I), 20.0),
    (re.compile(r"\b(wins?|beats?|defeats?|victory|final|match|series|"
                r"world cup|olympics?|medal|championship)\b", re.I), 18.0),
    (re.compile(r"\b(\bai\b|artificial intelligence|chip|semiconductor|"
                r"satellite|spacecraft|launch|isro|nasa|startup|"
                r"breakthrough|discovery|vaccine)\b", re.I), 18.0),
    (_IMPACT_RE, 12.0),
]


def conversation_score(title, summary=""):
    """How naturally the story invites genuine discussion (0-100) —
    policy consequences, public impact, economy, major decisions,
    sport, tech, controversy, geopolitics. Never manufactured: purely
    descriptive of what the source itself reports."""
    text = "%s %s" % (title or "", summary or "")
    score = 15.0   # neutral baseline
    for rx, weight in _CONVERSATION_RULES:
        if rx.search(text):
            score += weight
    return round(max(0.0, min(100.0, score)), 1)


# visual potential: what kind of visual genuinely helps the story
_VISUAL_HIGH = [
    (re.compile(r"\b(cyclone|flood|earthquake|landslide|storm|"
                r"disaster)\b", re.I), "map of the affected region"),
    (re.compile(r"\b(election|results?|vote count|poll|exit poll)\b",
                re.I), "results map or scoreboard"),
    (re.compile(r"\b(match|final|series|world cup|score|wickets?|"
                r"goals?|medal)\b", re.I), "scoreboard graphic"),
    (re.compile(r"\b(satellite|spacecraft|launch|isro|chandrayaan|"
                r"gaganyaan|nasa|rocket)\b", re.I), "launch photograph"),
    (re.compile(r"\b(budget|gdp|inflation|repo rate|quarterly|"
                r"earnings|markets?|sensex|nifty)\b", re.I),
     "data chart of the key figure"),
]
_VISUAL_MEDIUM = [
    (re.compile(r"\b(verdict|court|supreme court|rbi|policy|bill|"
                r"parliament|cabinet|protest|rally)\b", re.I),
     "photo from the scene"),
]


def visual_score(title, summary=""):
    """Visual opportunity (0-100) + level + suggested visual type.
    Nothing is downloaded — this is an editorial hint only."""
    text = "%s %s" % (title or "", summary or "")
    for rx, suggestion in _VISUAL_HIGH:
        if rx.search(text):
            return 85.0, "HIGH", suggestion
    for rx, suggestion in _VISUAL_MEDIUM:
        if rx.search(text):
            return 55.0, "MEDIUM", suggestion
    return 25.0, "LOW", None


def publish_score(article, cluster_rows=None, similar_to_posted=False,
                  now=None):
    """Full publish breakdown for the dashboard's recommendation.

    article: the candidate row; cluster_rows: its same-story cluster
    rows (coverage signals); similar_to_posted: True when the story
    matches a topic posted recently (topic cooldown) — lowers
    uniqueness. Returns a dict of all signals (0-100) plus reasons and
    the weighted 'publish' score. reach_score() stays separate and
    unchanged."""
    now = now or datetime.now(timezone.utc)
    momentum, momentum_reasons = momentum_score(cluster_rows, now)
    freshness = _freshness(article.get("published_at"),
                           article.get("discovered_at"), now)
    importance = round(100.0 * float(article.get("importance_score") or 0))
    conversation = conversation_score(article.get("title"),
                                      article.get("summary"))
    hook = hook_score(article.get("title"), article.get("summary"))
    source_quality = round(100.0 *
                           float(article.get("reliability_score") or 0.8))
    visual, visual_level, visual_suggestion = visual_score(
        article.get("title"), article.get("summary"))
    uniqueness = 30.0 if similar_to_posted else 100.0

    signals = {
        "momentum": momentum, "freshness": freshness,
        "importance": importance, "conversation": conversation,
        "hook": hook, "source_quality": source_quality,
        "visual": visual, "uniqueness": uniqueness,
    }
    publish = round(sum(PUBLISH_WEIGHTS[k] * v
                        for k, v in signals.items()), 1)
    signals.update({
        "publish": publish,
        "momentum_reasons": momentum_reasons,
        "visual_level": visual_level,
        "visual_suggestion": visual_suggestion,
    })
    return signals


def why_publish(breakdown):
    """One-line 'Why this story?' explanation for the publish card."""
    phrases = []
    if breakdown.get("momentum", 0) >= 70:
        phrases.append("coverage is accelerating across multiple sources")
    elif breakdown.get("momentum", 0) >= 50:
        phrases.append("several outlets are covering it")
    if breakdown.get("hook", 0) >= 70:
        phrases.append("the story has a strong, specific first-line hook")
    if breakdown.get("freshness", 0) >= 80:
        phrases.append("it is highly recent")
    if breakdown.get("conversation", 0) >= 60:
        phrases.append("it naturally invites discussion")
    if breakdown.get("importance", 0) >= 80:
        phrases.append("it is a high-importance event")
    if not phrases:
        phrases.append("it is the strongest qualifying story right now")
    return "Selected because " + ", ".join(phrases[:3]) + "."


# ============================================================================
# X REACH POTENTIAL (XRP) — ADVISORY EDITORIAL ESTIMATE ONLY.
#
# A third score, separate from reach_score (queue display) and
# publish_score (selection). It estimates how well a story fits the
# conditions under which posts travel on X — recency, multi-outlet
# momentum, importance, semantic clarity, shareability, discussion
# potential, save value, media potential, source quality — minus a
# saturation penalty when SAYOX itself already covered the topic.
#
# Basis: third-party analysis of X's public behavior (Sprout Social,
# "How the Twitter algorithm works in 2026"). These are editorial
# heuristics derived from a non-official source, NOT a reproduction of
# X's proprietary algorithm, NOT a prediction of impressions, and NEVER
# a guarantee of reach. XRP never gates eligibility and never replaces
# publish_score: the gates and cooldowns stay authoritative.
# ============================================================================

XRP_WEIGHTS = {
    "recency": 0.20, "momentum": 0.20, "importance": 0.15,
    "relevance": 0.10, "shareability": 0.10, "discussion": 0.10,
    "save_value": 0.05, "media": 0.05, "source_quality": 0.05,
}

# steep advisory recency decay: a post loses roughly half its potential
# visibility every ~6 hours (third-party estimate, NOT an official X
# constant). publish_score keeps its own gentler 48h linear freshness.
_RECENCY_HALF_LIFE_H = 6.0


def _recency_decay(published_at, discovered_at, now=None):
    """Advisory recency (0-100) with a ~6h half-life — much steeper than
    the 48h linear freshness used by publish_score. XRP only."""
    dt = _parse_dt(published_at) or _parse_dt(discovered_at)
    if dt is None:
        return 50.0
    now = now or datetime.now(timezone.utc)
    age_h = max(0.0, (now - dt).total_seconds() / 3600)
    return round(100.0 * (0.5 ** (age_h / _RECENCY_HALF_LIFE_H)), 1)


# superlatives/firsts that make a sourced fact naturally worth
# reposting — used only when the SOURCE's own words contain them,
# never invented into the tweet
_SHAREABLE_FIRST_RE = re.compile(
    r"\b(record|first|biggest|largest|highest|lowest|smallest|all-time|"
    r"historic|unprecedented)\b", re.IGNORECASE)


def shareability_score(headline, summary=""):
    """How naturally the story is worth REPOSTING (0-100) — advisory XRP
    signal. Rewards concrete numbers, named entities, strong action
    verbs, records/firsts and clear consequences, all from the source's
    own wording; penalizes weak intros. Never rewards sensational
    wording the source did not use."""
    lead = (headline or "").strip()
    text = "%s %s" % (lead, summary or "")
    score = 20.0
    if _NUMBER_RE.search(lead):
        score += 25.0
    elif _NUMBER_RE.search(text or ""):
        score += 12.0
    score += 12.0 * min(3, len(_ENTITY_RE.findall(lead)))
    if _HOOK_VERB_RE.search(lead):
        score += 15.0
    if _SHAREABLE_FIRST_RE.search(text or ""):
        score += 15.0
    if _CONSEQUENCE_HINT_RE.search(text or ""):
        score += 10.0
    if _WEAK_INTRO_RE.match(lead):
        score -= 30.0
    return round(max(0.0, min(100.0, score)), 1)


# reference utility: markers that make a story worth BOOKMARKING
_SAVE_RULES = [
    (re.compile(r"\b(rate|rates|price|prices|pricing|tax|taxes|fee|fees|"
                r"fare|fares|cutoff|deadline|schedule|timetable|quota|"
                r"limit|eligib\w*|rules?|policy|policies|scheme|"
                r"guidelines?|budget)\b", re.IGNORECASE), 20.0),
    (re.compile(r"\b(effective (from|on)|starts? (from|on)|applies "
                r"(from|to)|comes into (force|effect)|as of)\b",
                re.IGNORECASE), 15.0),
    (re.compile(r"\b(list|guide|explained|explainer|here'?s what|"
                r"what changed|need to know|checklist|full schedule)\b",
                re.IGNORECASE), 15.0),
    (re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|"
                r"sunday|january|february|march|april|june|july|august|"
                r"september|october|november|december)\b",
                re.IGNORECASE), 10.0),
]


def save_value_score(title, summary=""):
    """How useful the story is to SAVE/bookmark (0-100) — advisory XRP
    signal. Rewards durable reference facts (rates, prices, deadlines,
    schedules, effective dates, data density) from the source's own
    text."""
    text = "%s %s" % (title or "", summary or "")
    score = 15.0
    for rx, weight in _SAVE_RULES:
        if rx.search(text):
            score += weight
    numbers = len(_NUMBER_RE.findall(text or ""))
    if numbers >= 2:
        score += 15.0     # data density: figures worth referring back to
    elif numbers == 1:
        score += 7.0
    return round(max(0.0, min(100.0, score)), 1)


# words that say nothing about WHICH topic a headline belongs to
_CLARITY_GENERIC = frozenset("""
government govt minister ministry official officials authorities says
said announce announced report reported news latest update updates amid
ahead after over under several major including according people public
""".split())


def semantic_clarity_score(headline, summary=""):
    """How clearly the headline communicates WHICH topic the story
    belongs to (0-100) — advisory XRP signal. Rewards precise named
    entities (the idea behind X's topical communities: a post connects
    to an audience through concrete subjects); penalizes generic
    newsroom vocabulary that could describe any story."""
    lead = (headline or "").strip()
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", lead)
    if not words:
        return 20.0
    entities = len(_ENTITY_RE.findall(lead))
    generic = sum(1 for w in words if w.lower() in _CLARITY_GENERIC)
    generic_ratio = generic / float(len(words))
    score = 25.0 + 12.0 * min(3, entities)
    if entities and _ENTITY_RE.search(summary or ""):
        score += 10.0     # entity echoed below the headline: one topic
    score -= 30.0 * generic_ratio
    return round(max(0.0, min(100.0, score)), 1)


def x_reach_potential(article, cluster_rows=None, posted_overlap=False,
                      now=None, account_premium="unknown"):
    """Advisory 'Reach potential — editorial estimate' for the dashboard.

    Blends steep-decay recency, multi-outlet momentum, importance,
    semantic clarity, shareability, discussion potential, save value,
    media potential and source quality, minus a saturation penalty when
    SAYOX already posted this topic recently (posted_overlap = count of
    our own overlapping posts in the lookback window).

    ADVISORY ONLY: never gates eligibility and never replaces
    publish_score. account_premium is a display note only — Premium
    would amplify every post equally, so it is NEVER a ranking
    multiplier."""
    now = now or datetime.now(timezone.utc)
    overlap = int(posted_overlap or 0)
    momentum, momentum_reasons = momentum_score(cluster_rows, now)
    title = article.get("title")
    summary = article.get("summary")
    signals = {
        "recency": _recency_decay(article.get("published_at"),
                                  article.get("discovered_at"), now),
        "momentum": momentum,
        "importance": round(100.0 *
                            float(article.get("importance_score") or 0)),
        "relevance": semantic_clarity_score(title, summary),
        "shareability": shareability_score(title, summary),
        "discussion": conversation_score(title, summary),
        "save_value": save_value_score(title, summary),
        "media": visual_score(title, summary)[0],
        "source_quality": round(
            100.0 * float(article.get("reliability_score") or 0.8)),
    }
    score = sum(XRP_WEIGHTS[k] * v for k, v in signals.items())
    # saturation: followers have already seen this topic from us
    saturation_penalty = min(30.0, 15.0 * overlap)
    score = round(max(0.0, min(100.0, score - saturation_penalty)), 1)

    reasons = []
    if signals["recency"] >= 70:
        reasons.append("still inside the steep early-decay window")
    if signals["momentum"] >= 50:
        reasons.append(momentum_reasons[0] if momentum_reasons
                       else "multi-outlet coverage")
    if signals["shareability"] >= 60:
        reasons.append("concrete, repostable facts")
    if signals["discussion"] >= 55:
        reasons.append("naturally invites discussion")
    if signals["save_value"] >= 55:
        reasons.append("reference value worth bookmarking")
    if signals["relevance"] >= 60:
        reasons.append("topically precise — clearly one subject")
    if overlap:
        reasons.append("we already posted this topic %d time(s) in the "
                       "lookback window" % overlap)
    if account_premium == "premium":
        reasons.append("account has X Premium (amplifies every post "
                       "equally — never a ranking factor)")
    return {
        "label": "Reach potential — editorial estimate",
        "score": score,
        "signals": signals,
        "saturation_penalty": round(saturation_penalty, 1),
        "reasons": reasons,
    }
