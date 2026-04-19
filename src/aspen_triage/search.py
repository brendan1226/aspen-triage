from pathlib import Path
from typing import TypedDict

import numpy as np
from fastembed import TextEmbedding

from .db import connect, init_db
from .embed import _normalize, deserialize_embedding


class SearchResult(TypedDict):
    jira_key: str
    internal_id: int
    summary: str
    url: str
    status: str
    status_category: str
    issue_type: str
    priority: str
    resolution: str
    components: str
    reporter: str
    assignee: str
    score: float
    description_snippet: str
    description: str


SNIPPET_CHARS = 300


class NoEmbeddingsError(RuntimeError):
    pass


def _embed_query(model_name: str, query: str) -> np.ndarray:
    model = TextEmbedding(model_name=model_name)
    vec = next(model.embed([query]))
    return _normalize(np.array(vec, dtype=np.float32))


def search(
    db_path: Path,
    query: str,
    model_name: str,
    top_k: int = 5,
    issue_type: str | None = None,
    status_category: str | None = None,
) -> list[SearchResult]:
    init_db(db_path)
    query_vec = _embed_query(model_name, query)

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, jira_key, summary, description, url, status, status_category,
                   issue_type, priority, resolution, components, reporter, assignee, embedding
            FROM issues
            WHERE embedding IS NOT NULL
            """
        ).fetchall()

    if issue_type:
        rows = [r for r in rows if r["issue_type"] == issue_type]
    if status_category:
        rows = [r for r in rows if r["status_category"] == status_category]

    if not rows:
        raise NoEmbeddingsError(
            "No embedded issues. Run `aspen-triage embed` first to index issues."
        )

    matrix = np.vstack([deserialize_embedding(r["embedding"]) for r in rows])
    scores = matrix @ query_vec
    top_indices = np.argsort(-scores)[:top_k]

    results: list[SearchResult] = []
    for idx in top_indices:
        row = rows[int(idx)]
        desc = row["description"] or ""
        snippet = desc.strip().replace("\r\n", "\n")
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS].rstrip() + "..."
        results.append(
            SearchResult(
                jira_key=row["jira_key"],
                internal_id=row["id"],
                summary=row["summary"],
                url=row["url"],
                status=row["status"],
                status_category=row["status_category"] or "",
                issue_type=row["issue_type"],
                priority=row["priority"] or "",
                resolution=row["resolution"] or "",
                components=row["components"] or "",
                reporter=row["reporter"] or "",
                assignee=row["assignee"] or "",
                score=float(scores[int(idx)]),
                description_snippet=snippet,
                description=desc,
            )
        )
    return results
