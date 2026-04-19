from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .db import connect, init_db

GUIDELINES_DIR = Path(__file__).parent / "guidelines"


class Recommendation(BaseModel):
    summary: str = Field(..., description="1-2 sentence summary of the issue.")
    affected_areas: list[str] = Field(
        ..., description="Aspen areas affected (PHP frontend, Java indexer, templates, DB, etc.)."
    )
    likely_files: list[str] = Field(
        ..., description="File paths that likely need changes (relative to repo root)."
    )
    complexity: Literal["easy", "medium", "hard"] = Field(
        ..., description="Estimated complexity of the fix."
    )
    fix_approach: str = Field(
        ..., description="A paragraph explaining what to change, why, and key constraints."
    )
    key_guidelines: list[str] = Field(
        ..., description="Relevant coding guideline rules that apply (short phrases)."
    )
    test_plan: str = Field(
        ..., description="How to verify the fix — which scenarios to test."
    )
    suggested_branch_name: str = Field(
        ..., description="Branch name in the form DIS-XXXX-short-description."
    )
    needs_db_update: bool = Field(
        ..., description="True if the fix requires a database schema change."
    )


def _load_guidelines() -> str:
    """Load the core QA / coding guidelines for Aspen."""
    parts = []
    for name in ["security.md", "QA_CODE_ANALYSIS.md", "SOLR_CODE_REVIEW.md"]:
        path = GUIDELINES_DIR / name
        if path.exists():
            # Cap each file to keep under rate limits
            text = path.read_text()
            if len(text) > 15000:
                text = text[:15000] + "\n\n(... truncated)"
            parts.append(f"# {path.stem}\n\n{text}")
    return "\n\n---\n\n".join(parts) if parts else "(no guidelines available)"


SYSTEM_PROMPT = """You are a senior Aspen Discovery developer.

Aspen Discovery is an open-source library discovery platform built in PHP (web frontend), Java (Solr indexers and cron jobs), and Smarty templates. The codebase has known security issues (SQL injection, CSRF, XSS) that contributors should avoid reintroducing.

You will be given:
1. Aspen Discovery QA/security guidelines
2. A JIRA issue with its description and comments

Your job: analyze the issue and produce a structured fix recommendation that a developer (or AI coding agent) can act on. Be specific about file paths, function names, and the approach.

Key Aspen conventions:
- PHP frontend files are under code/web/
- Java indexers are under code/reindexer/ and code/aspen_app_server/
- Smarty templates are under code/web/interface/themes/
- Database schema files: install/sql/
- Always use parameterized queries (SQL injection is a major ongoing issue)
- Always use CSRF tokens on state-changing forms
- Always filter template output (XSS prevention)
- PR title format: "DIS-XXXX: description" (reference the JIRA ticket)

Be pragmatic — recommend the simplest fix that solves the problem while improving security where possible."""


def _build_issue_context(issue: dict, comments: list[dict]) -> str:
    lines = [
        f"## Issue: {issue['jira_key']}",
        f"**Summary:** {issue['summary']}",
        f"**Status:** {issue['status']}",
        f"**Type:** {issue['issue_type']}",
        f"**Priority:** {issue.get('priority') or 'unset'}",
        f"**Components:** {issue.get('components') or 'none'}",
        f"**Reporter:** {issue.get('reporter') or 'unknown'}",
        f"**Created:** {issue['created'][:10]}",
        "",
        "**Description:**",
        issue.get("description") or "(empty)",
    ]

    if comments:
        lines.append("")
        lines.append(f"**Comments ({len(comments)}):**")
        for c in comments[:10]:
            lines.append(f"\n--- {c.get('author', 'unknown')} ({c['created'][:10]}):")
            lines.append(c.get("body") or "(empty)")

    return "\n".join(lines)


def generate_recommendation(
    db_path: Path,
    issue_internal_id: int,
    api_key: str,
    model: str = "claude-opus-4-6",
) -> Recommendation:
    init_db(db_path)

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Issue {issue_internal_id} not found")

        comments = conn.execute(
            "SELECT * FROM comments WHERE issue_id = ? ORDER BY created LIMIT 10",
            (issue_internal_id,),
        ).fetchall()

    issue = dict(row)
    guidelines = _load_guidelines()
    issue_context = _build_issue_context(issue, [dict(c) for c in comments])

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"# ASPEN DISCOVERY GUIDELINES\n\n{guidelines}\n\n"
                    f"---\n\n# ISSUE TO ANALYZE\n\n{issue_context}\n\n"
                    "Produce a structured fix recommendation."
                ),
            }
        ],
        output_format=Recommendation,
    ) as stream:
        final = stream.get_final_message()

    rec = final.parsed_output
    if rec is None:
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        rec = Recommendation.model_validate_json(text)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO recommendations (issue_id, model, recommendation, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                model = excluded.model,
                recommendation = excluded.recommendation,
                created_at = excluded.created_at
            """,
            (issue_internal_id, model, rec.model_dump_json(), now),
        )

    return rec


def get_stored_recommendation(db_path: Path, issue_internal_id: int) -> tuple[Recommendation, str, str] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT model, recommendation, created_at FROM recommendations WHERE issue_id = ?",
            (issue_internal_id,),
        ).fetchone()
    if row is None:
        return None
    rec = Recommendation.model_validate_json(row["recommendation"])
    return rec, row["model"], row["created_at"]
