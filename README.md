# India News Bot

Fully automated India-first X news bot. **Zero AI tokens by default** — the entire pipeline (RSS → normalize → dedup → cluster → India scoring → importance → tweet) is deterministic.

## Quick start (dry run — safe, posts nothing)

```bash
pip install -r requirements.txt
python -m app.main --once --dry-run
```

You'll see sources checked, articles discovered, duplicates removed, India/importance scores, selected stories, and generated tweets — with 0 AI calls.

## Run continuously

```bash
python -m app.main --serve --dry-run
```

## Tests

```bash
python -m unittest discover tests -v
```

Tests never post to X.

## Going live (only after dry-run review)

1. Copy `.env.example` → `.env`, fill X API credentials (OAuth 1.0a user context keys from developer.x.com).
2. Set `BOT_ENABLED=true`, `DRY_RUN=false`, `AUTO_POST=true`.
3. `python -m app.main --serve`

Kill switch: `BOT_ENABLED=false` stops all posting instantly.

## Safety defaults

`BOT_ENABLED=false`, `DRY_RUN=true`, `AUTO_POST=false`, `AI_ENABLED=false`,
`MAX_TWEETS_PER_HOUR=5`, `MAX_TWEETS_PER_DAY=50`.

## India-first logic

Soft target of **50% India / 50% global**, maintained over time — never forced per batch. Weak stories on either side are never selected just to hit the balance; strong stories from one side fill the gap when the other lacks quality. Major breaking news always overrides the balance. Tunable in `app/config.py` (`INDIA_TARGET_SHARE`).

## Deployment

`docker compose up -d` — runs 24/7 on any server; data persists in `./data`.

## Docs

See `research/` for verified sources, X API notes, and decisions log.
