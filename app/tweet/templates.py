"""Deterministic tweet style constants. Zero AI.

Newsroom style: most tweets have NO label. Labels are rare and meaningful:
  BREAKING:  only genuinely breaking major events (the source itself says
             "breaking"/"just in" AND a major-event keyword is present AND
             importance is high — recency alone never triggers it)
  Developing: only when the source says the event is actively unfolding
  Update:    only for meaningful follow-ups to an existing major story
"""

# X shortens every URL to a 23-char t.co link when counting the character limit.
TCO_LENGTH = 23

# Phrases the bot must never add (only acceptable if the source itself says it)
FILLER_PHRASES = [
    "stay tuned", "more details to follow", "watch this space",
]

# Category labels that must never be generated.
BANNED_LABELS = [
    "DECISION", "ANNOUNCEMENT", "REPORT", "RESULT", "LAUNCH", "DISCOVERY",
    "ALERT", "NEWS", "BREAKING NEWS", "INDIA UPDATE", "GLOBAL", "UPDATE\n",
]

# Major-event keywords that qualify a source's own "breaking" claim
BREAKING_MAJOR = [
    "attack", "killed", "dead", "dies", "death", "earthquake", "flood",
    "cyclone", "stampede", "blast", "explosion", "crash", "war", "invasion",
    "verdict", "convicted", "sentenced", "resigns", "resignation",
    "election result", "wins election", "bans", "ban on", "collapse",
    "shooting", "terror", "militant", "missile", "nuclear test",
]

# Markers that an event is actively unfolding
DEVELOPING_MARKERS = [
    "developing story", "unfolding", "live updates", "live blog",
    "operation ongoing", "rescue operations", "as it happens",
]

# Uncertainty words that must be preserved verbatim
UNCERTAINTY_WORDS = [
    "alleged", "allegedly", "reportedly", "suspected", "suspect",
    "according to", "sources said", "unconfirmed",
]

# keyword -> hashtag pairs (specific entities/topics only; no generic tags).
# A pair may map to two tags when they belong together.
HASHTAG_RULES = [
    (("rbi", "repo rate", "monetary policy"), ("#RBI",)),
    (("sebi",), ("#SEBI",)),
    (("supreme court",), ("#SupremeCourt",)),
    (("parliament",), ("#Parliament",)),
    (("election",), ("#Elections",)),
    (("isro", "chandrayaan", "gaganyaan"), ("#ISRO", "#Space")),
    (("satellite", "spacecraft", "space mission"), ("#Space",)),
    (("gdp", "inflation", "economy"), ("#Economy",)),
    (("budget",), ("#Budget",)),
    (("gst",), ("#GST",)),
    (("rupee",), ("#Rupee",)),
    (("sensex", "nifty", "ipo", "stock market", "shares"), ("#Markets",)),
    (("startup", "unicorn"), ("#Startups",)),
    (("banking", "bank loan", "bank"), ("#Banking",)),
    (("cricket", "world cup"), ("#Cricket",)),
    (("ipl",), ("#IPL",)),
    (("monsoon", "rainfall"), ("#Monsoon",)),
    (("ai ", " ai", "artificial intelligence", "openai", "chatgpt"), ("#AI",)),
    (("semiconductor", "chip plant", "chipmaking"), ("#Semiconductors",)),
    (("flood", "floods"), ("#Floods",)),
    (("earthquake",), ("#Earthquake",)),
    (("cyclone",), ("#Cyclone",)),
    (("landslide",), ("#Landslide",)),
    (("nepal",), ("#Nepal",)),
    (("china", "beijing"), ("#China",)),
    (("pakistan",), ("#Pakistan",)),
    (("gaza", "israel",), ("#IsraelPalestine",)),
    (("ukraine", "russia"), ("#Ukraine",)),
]

# Generic hashtags to never emit
GENERIC_HASHTAGS = {"#News", "#BreakingNews", "#Update", "#India", "#World"}
