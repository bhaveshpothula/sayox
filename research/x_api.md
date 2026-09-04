# X API Research (verified 2026-08-29)

## Posting
- Endpoint: `POST https://api.x.com/2/tweets`
- Body: `{"text": "..."}`
- Response 201 → `{"data": {"id": "...", "text": "..."}}`

## Auth
- OAuth 1.0a User Context (API Key, API Secret, Access Token, Access Token Secret) — simplest for posting with the four env keys. Recommended for this bot.
- Alternative: OAuth 2.0 User Token with scopes `tweet.read`, `tweet.write`, `users.read`.
- Library: `requests` + manual HMAC-SHA1 OAuth 1.0a signing (no extra deps), or `tweepy`. Decision: implement OAuth 1.0a signing with stdlib (hmac/hashlib) to avoid dependency; use httpx for HTTP.

## Limits
- Post length: 280 chars (longer for Premium, assume 280).
- Rate limits vary by tier (Basic/paid or pay-per-use self-serve tiers; Free tier historically disallows posting — user must have a paid tier or legacy access). Bot handles 403/429 gracefully.

## Restrictions
- Quote-posting requires Enterprise — avoid.
- No browser automation. Official API only.
- Automation rules: must not spam; our rate caps (5/hr, 50/day) are well within limits.

## Credentials (.env only, never in code/logs)
X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
