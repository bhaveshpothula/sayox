"""Optional AI tweet generation wrapper (unused when AI_ENABLED=false)."""
from app.ai.provider import AIProvider


def maybe_ai_generate(article, base_tweet, provider):
    if provider is None or not provider.enabled:
        return base_tweet, "deterministic"
    return provider.rewrite_tweet(
        article.get("title", ""), article.get("summary", ""), base_tweet)
