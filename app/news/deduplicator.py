"""Duplicate detection: URL-level + title-similarity."""
from app.news.normalizer import jaccard, tokens


def is_duplicate_url(url_hash_value, existing_hashes):
    return url_hash_value in existing_hashes


def find_similar_title(title, existing_titles, threshold=0.75):
    """Return the existing title considered a duplicate, else None."""
    t = tokens(title)
    for other in existing_titles:
        if jaccard(t, tokens(other)) >= threshold:
            return other
    return None
