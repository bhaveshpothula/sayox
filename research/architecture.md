# Architecture

Pipeline (all deterministic, zero LLM by default):

RSS fetch (feedparser/httpx)
→ normalize (URL + text cleaning)
→ dedup (URL hash → simhash-lite title similarity)
→ cluster (token-overlap greedy clustering)
→ India relevance (keyword/entity scoring, source priors)
→ importance (keyword tiers + source reliability + recency)
→ selection (India ~50% / global ~50% soft target, balanced over time, min thresholds; breaking news overrides)
→ tweet generation (templates + rule-based hashtags)
→ validation (280 chars, no fabrication, rate caps)
→ [dry-run print | X API post]
→ SQLite records

## Modules
- app/main.py — CLI: `--once --dry-run`, `--serve` loop
- app/config.py — env-driven config (BOT_ENABLED, DRY_RUN, AI_ENABLED, caps)
- app/database.py — SQLite schema + access
- app/news/* — collector, rss, normalizer, deduplicator, classifier (India), importance, sources
- app/tweet/* — generator, templates, validator, ai_generator (optional)
- app/x/client.py — OAuth 1.0a signed POST /2/tweets
- app/ai/* — budget tracker, provider stub (never called when AI_ENABLED=false)

## Safety
BOT_ENABLED=false + DRY_RUN=true + AUTO_POST=false defaults. Kill switch honored everywhere. Rate caps enforced from tweets table.

## Deployment
Docker + docker-compose; long-running loop with configurable interval.
