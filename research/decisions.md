# Decisions Log

- 2026-08-29: No previous session artifacts found (empty dir). Started fresh.
- 2026-08-29: Feed balance target changed to 50% India / 50% global (soft, over-time balance; quality floor on both sides; breaking news overrides). Previously 60–75% India.
- X posting via OAuth 1.0a User Context, hand-signed with stdlib (no tweepy dependency).
- Dedup: normalized-URL exact match, then Jaccard token similarity on titles (≥0.75 = duplicate).
- Clustering: greedy, Jaccard ≥ 0.45 on title tokens + shared entities → same story cluster.
- India scoring: keyword lists (states, cities, institutions, topics) + source country prior.
- Importance: tiered keyword scoring × source reliability; recency decay.
- Tweet generation: 6 deterministic templates; hashtags from fixed keyword→tag map; strict 280-char truncation-safe assembly.
- AI: budget table, provider raises if disabled; deterministic path never calls it.
- Python 3.9+ compatible (no match statements, no | unions in annotations).
- SQLite WAL mode, single file at data/newsbot.db.
