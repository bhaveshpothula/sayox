"""Lightweight data models (plain dicts + helpers)."""


class Article(dict):
    """Convenience dict subclass with attribute access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
