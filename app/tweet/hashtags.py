"""Hashtag engine for the dashboard: 2-4 highly relevant tags per story.

Builds on the existing keyword rules in app/tweet/templates.py, adding
location tags (states/cities) and #India for strongly India-relevant
stories. Deterministic, no AI. Tags are ordered specific -> general
(e.g. "#Kerala #India #Floods") and deduplicated. #India is allowed
here by design (the generic-tag ban in templates.GENERIC_HASHTAGS
applies to the old fully-automatic path, where #India added no signal;
in this dashboard it is an intentional audience tag)."""
from app.tweet.generator import pick_hashtags
from app.tweet.templates import HASHTAG_RULES

# Tags are never appended into the editor's warn zone (the last 20 chars
# of the limit — the same threshold the dashboard UI flags): dashboard
# tweets are editable and posted manually, so room for the user's edits
# is always preserved. A tweet already inside the zone has no room.
_WARN_ZONE = 20

# location -> hashtag, matched on word boundaries in title+summary
_LOCATION_TAGS = [
    ("kerala", "#Kerala"), ("kozhikode", "#Kozhikode"),
    ("maharashtra", "#Maharashtra"), ("mumbai", "#Mumbai"),
    ("delhi", "#Delhi"), ("karnataka", "#Karnataka"),
    ("bengaluru", "#Bengaluru"), ("tamil nadu", "#TamilNadu"),
    ("chennai", "#Chennai"), ("hyderabad", "#Hyderabad"),
    ("telangana", "#Telangana"), ("andhra pradesh", "#AndhraPradesh"),
    ("gujarat", "#Gujarat"), ("ahmedabad", "#Ahmedabad"),
    ("rajasthan", "#Rajasthan"), ("punjab", "#Punjab"),
    ("haryana", "#Haryana"), ("uttar pradesh", "#UP"),
    ("lucknow", "#Lucknow"), ("madhya pradesh", "#MP"),
    ("bihar", "#Bihar"), ("patna", "#Patna"),
    ("west bengal", "#WestBengal"), ("kolkata", "#Kolkata"),
    ("odisha", "#Odisha"), ("assam", "#Assam"), ("guwahati", "#Guwahati"),
    ("jharkhand", "#Jharkhand"), ("chhattisgarh", "#Chhattisgarh"),
    ("uttarakhand", "#Uttarakhand"), ("himachal", "#HimachalPradesh"),
    ("goa", "#Goa"), ("manipur", "#Manipur"), ("meghalaya", "#Meghalaya"),
    ("mizoram", "#Mizoram"), ("nagaland", "#Nagaland"),
    ("tripura", "#Tripura"), ("sikkim", "#Sikkim"),
    ("arunachal", "#ArunachalPradesh"), ("ladakh", "#Ladakh"),
    ("jammu", "#Jammu"), ("kashmir", "#Kashmir"),
    ("puducherry", "#Puducherry"), ("pune", "#Pune"),
    ("indore", "#Indore"), ("surat", "#Surat"), ("jaipur", "#Jaipur"),
]

_MIN, _MAX = 2, 4

# location tags as a set: the dashboard's 0-2 policy allows a SECOND tag
# only when it names the story's own location (e.g. #Earthquake #Delhi)
LOCATION_TAGS = frozenset(tag for _, tag in _LOCATION_TAGS)


def suggest_hashtags(title, summary, india_score=0.0, max_tags=_MAX):
    """Return 2-4 relevant hashtags for a story.

    A tag must be justified by the HEADLINE itself: topic and location
    keywords appearing only in the summary body describe background
    context (a share-price mention inside a sentencing story, a city
    name in boilerplate), not what THIS story is — they never produce
    a tag. Order: strongest topical tag first, then #India for
    India-relevant stories (score >= 0.5), then further topical and
    location tags. Always returns at least one tag when anything
    matched; falls back to ['#India'] for India-relevant stories."""
    headline = (title or "").lower()

    # candidate pools (deduped, in rule order) — headline matches only
    topic = []
    for keywords, rule_tags in HASHTAG_RULES:
        if any(kw in headline for kw in keywords):
            for t in rule_tags:
                if t not in topic:
                    topic.append(t)
    locations = [tag for phrase, tag in _LOCATION_TAGS
                 if phrase in headline and tag not in topic]

    # assemble: strongest topical tag, then #India (slot reserved —
    # never crowded out by extra topical tags), then the rest
    tags = []
    if topic:
        tags.append(topic[0])
    if india_score >= 0.5:
        tags.append("#India")
    for t in topic[1:] + locations:
        if len(tags) >= max_tags:
            break
        if t not in tags:
            tags.append(t)
    if not tags and india_score >= 0.35:
        tags.append("#India")
    return tags[:max_tags]


def with_hashtags(tweet_text, tags, char_limit=280):
    """Append any tags not already in the tweet, staying within
    char_limit. A tag is only appended if the result stays out of the
    warn zone (char_limit - _WARN_ZONE); a tweet with no room outside
    the zone is returned unchanged. Returns (final_text, tags_used)."""
    final = tweet_text.rstrip()
    used = [t for t in (tags or []) if t not in final.split()]
    for tag in used:
        candidate = "%s %s" % (final, tag)
        if len(candidate) > char_limit - _WARN_ZONE:
            break
        final = candidate
    return final, [t for t in tags if t in final.split()]
