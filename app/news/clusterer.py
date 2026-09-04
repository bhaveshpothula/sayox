"""Story clustering: greedy token-overlap grouping of related articles."""
from app.news.normalizer import jaccard, tokens, strip_source_suffix


def cluster_articles(articles, threshold=0.45):
    """articles: list of dicts with 'title' and 'id'.
    Returns list of clusters; each cluster is a list of article ids
    (first id = representative).

    A candidate joins a cluster when it is similar to ANY member, not
    just the representative: different outlets phrase the same event
    differently, so comparing against the first article alone splits
    genuine multi-source coverage into single-article clusters (which
    would read as 'no momentum' downstream)."""
    tok = {a["id"]: tokens(strip_source_suffix(a["title"])) for a in articles}
    clusters = []
    for a in articles:
        placed = False
        for c in clusters:
            if any(jaccard(tok[a["id"]], tok[mid]) >= threshold
                   for mid in c):
                c.append(a["id"])
                placed = True
                break
        if not placed:
            clusters.append([a["id"]])
    return clusters
