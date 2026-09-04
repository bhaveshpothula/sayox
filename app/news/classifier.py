"""India relevance scoring — deterministic keyword + source-prior system."""
import re

# Weighted keyword groups. Every term is matched on word boundaries
# (phrases on whitespace-normalized word boundaries), never as an
# arbitrary substring — "arrested" must not trigger the "ed"
# (Enforcement Directorate) topic keyword.
_CORE = ["india", "indian", "bharat"]  # decisive terms, extra weight

_STRONG = [  # nearly always India-core
    "delhi", "new delhi", "mumbai", "bombay",
    "kolkata", "calcutta", "chennai", "bangalore", "bengaluru", "hyderabad",
    "ahmedabad", "pune", "jaipur", "lucknow", "varanasi", "surat", "kanpur",
    "nagpur", "indore", "bhopal", "patna", "kochi", "coimbatore", "guwahati",
    "chandigarh", "amritsar", "srinagar", "ayodhya", "ram mandir",
    "rbi", "reserve bank", "sebi", "isro", "niti aayog", "loksabha",
    "lok sabha", "rajya sabha", "parliament", "supreme court of india",
    "modi", "narendra modi", "shah rukh", "bollywood", "tollywood",
    "iit", "iims", "upsc", "nda", "congress party", "bjp", "aam aadmi",
    "trinamool", "samajwadi", "shiv sena", "dmk", "aiadmk", "telugu desam",
    "rupee", "crore", "lakh", "gst", "ayushman", "aadhaar",
]
_STATES = [
    "maharashtra", "karnataka", "tamil nadu", "tamilnadu", "kerala",
    "andhra pradesh", "telangana", "gujarat", "rajasthan", "punjab",
    "haryana", "uttar pradesh", "madhya pradesh", "bihar", "bengal",
    "west bengal", "odisha", "assam", "jharkhand", "chhattisgarh",
    "uttarakhand", "himachal", "goa", "manipur", "meghalaya", "mizoram",
    "nagaland", "tripura", "sikkim", "arunachal", "ladakh", "jammu",
    "kashmir", "puducherry",
]
_TOPICS = [
    "centre", "union minister", "cabinet", "prime minister's office",
    "indian army", "indian navy", "indian air force", "drdo", "bsf",
    "crpf", "cbi", "ed", "enforcement directorate", "income tax",
    "nifty", "sensex", "bse", "nse", "dalal street", "infosys", "tcs",
    "reliance", "adani", "tata", "wipro", "hcl", "byju", "paytm",
    "zomato", "swiggy", "ola", "flipkart", "phonepe", "airtel", "jio",
    "irctc", "railways", "monsoon", "kisan", "farmers protest",
    "ipl", "bcci", "team india", "virat", "rohit", "kohli", "dhoni",
    "sindhu", "neeraj", "chopra", "hockey india", "chandrayaan",
    "gaganyaan", "mangalyaan", "startup india", "unicorn",
]
_WEAK = [  # common in global stories about India; low weight
    "asia", "south asia", "saarc", "hindu", "muslim", "yoga", "curry",
]


def _keyword_re(phrase):
    """Word-boundary regex for a keyword or keyword phrase."""
    words = phrase.split()
    return re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words)
                      + r"\b", re.IGNORECASE)


_MATCHERS = [( _keyword_re(kw), weight) for kws, weight in (
    (_CORE, 0.25), (_STRONG, 0.20), (_STATES, 0.15),
    (_TOPICS, 0.10), (_WEAK, 0.05)) for kw in kws]


def score_india_relevance(title, summary, source_country="GLOBAL"):
    """Return score in [0, 1]."""
    text = ("%s %s" % (title or "", summary or "")).lower()
    if not text.strip():
        return 0.0

    score = 0.0
    hits = 0
    for rx, weight in _MATCHERS:
        if rx.search(text):
            score += weight
            if weight >= 0.10:
                hits += 1

    # source prior: Indian source boosts, global source discounts
    if source_country == "IN":
        score += 0.30
    else:
        # global sources cover India only when explicitly mentioned
        if hits == 0:
            score = min(score, 0.10)

    return min(score, 1.0)
