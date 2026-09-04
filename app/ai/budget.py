"""AI budget tracker. Only consulted when AI_ENABLED=true."""
from app.logger import get_logger

log = get_logger("ai.budget")


class BudgetExceeded(Exception):
    pass


class AIBudget:
    def __init__(self, db, daily_limit=20):
        self.db = db
        self.daily_limit = daily_limit

    def remaining(self):
        return max(0, self.daily_limit - self.db.ai_tokens_today())

    def consume(self, tokens, reason=""):
        if self.remaining() < tokens:
            raise BudgetExceeded("daily AI limit reached")
        self.db.record_ai_usage("none", "none", tokens, reason)
