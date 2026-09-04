"""Optional AI provider (disabled by default).

Never called unless AI_ENABLED=true. Kept as a stub so the deterministic
pipeline has a clearly defined fallback hook.
"""
from app.logger import get_logger

log = get_logger("ai.provider")


class AIProvider:
    def __init__(self, budget, enabled=False):
        self.budget = budget
        self.enabled = enabled

    def rewrite_tweet(self, headline, summary, base_tweet, tokens_est=500):
        if not self.enabled:
            log.info("AI disabled; using deterministic tweet")
            return base_tweet, "deterministic"
        try:
            self.budget.consume(tokens_est, "tweet_rewrite")
        except Exception as e:
            log.warning("AI budget unavailable (%s); deterministic fallback", e)
            return base_tweet, "deterministic"
        # No external LLM is wired in by design; deterministic output is
        # always returned. Replace this method body when attaching a provider.
        return base_tweet, "deterministic"
