"""AI-powered QA review for Aspen Discovery issues and PRs."""

from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from .db import connect, init_db

GUIDELINES_DIR = Path(__file__).parent / "guidelines"


class QAResult(BaseModel):
    overall_verdict: str = Field(
        ...,
        description="One of: 'passes_qa', 'needs_followup', 'fails_qa'",
    )
    summary: str = Field(..., description="2-3 sentence summary of the QA review.")
    strengths: list[str] = Field(..., description="What the code does well.")
    issues: list[str] = Field(..., description="Issues found — security, coding violations, logic errors, etc.")
    testing_notes: str = Field(..., description="What was checked and what should be tested manually.")
    suggested_followups: list[str] = Field(
        default_factory=list,
        description="Specific follow-up items if verdict is needs_followup.",
    )


def _load_guidelines() -> str:
    """Load core security + QA guidelines, trimmed."""
    parts = []
    for name in ["security.md", "QA_CODE_ANALYSIS.md"]:
        path = GUIDELINES_DIR / name
        if path.exists():
            text = path.read_text()
            if len(text) > 12000:
                text = text[:12000] + "\n\n(... truncated)"
            parts.append(f"# {path.stem}\n\n{text}")
    return "\n\n---\n\n".join(parts) if parts else "(no guidelines available)"


SYSTEM_PROMPT = """You are a QA reviewer for Aspen Discovery (open-source library discovery platform).

You will receive:
1. Aspen security and QA guidelines (with known historical issues)
2. A JIRA issue with description and comments
3. A code change or PR diff to review

Your job: review against security best practices and Aspen coding conventions.

Critical things to flag:
- SQL injection (string concatenation in queries)
- XSS (unescaped template output)
- CSRF (missing tokens on state-changing forms)
- Command injection (exec/shell_exec with user input)
- Unsafe deserialization (unserialize() on user data)
- eval() on any input
- Hardcoded credentials or paths
- Missing input validation

Verdict meanings:
- passes_qa: Clean, follows conventions, no security concerns. Ready to merge.
- needs_followup: Works but has minor issues. List specific follow-ups.
- fails_qa: Significant problems — security issues, breaks guidelines, or doesn't solve the problem.

Be constructive."""


def review_code(
    db_path: Path,
    issue_internal_id: int,
    diff_or_code: str,
    api_key: str,
    model: str = "claude-opus-4-6",
) -> QAResult:
    """Run AI QA review on a code change or diff."""
    init_db(db_path)

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Issue {issue_internal_id} not found")
        comments = conn.execute(
            "SELECT author, body, created FROM comments WHERE issue_id = ? ORDER BY created LIMIT 10",
            (issue_internal_id,),
        ).fetchall()

    issue = dict(row)
    guidelines = _load_guidelines()
    comment_text = "\n".join(
        f"**{c['author']}** ({c['created'][:10]}): {c['body'][:400]}"
        for c in comments
    )

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"# ASPEN GUIDELINES\n\n{guidelines}\n\n"
                    f"---\n\n# ISSUE\n\n"
                    f"## {issue['jira_key']}: {issue['summary']}\n"
                    f"**Status:** {issue['status']}\n\n"
                    f"**Description:**\n{issue.get('description') or '(empty)'}\n\n"
                    f"**Comments:**\n{comment_text}\n\n"
                    f"---\n\n# CODE TO REVIEW\n\n"
                    f"```\n{diff_or_code}\n```\n\n"
                    "Review this code against the guidelines and issue requirements."
                ),
            }
        ],
        output_format=QAResult,
    ) as stream:
        final = stream.get_final_message()

    result = final.parsed_output
    if result is None:
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        result = QAResult.model_validate_json(text)

    return result


def format_qa_comment(
    result: QAResult,
    jira_key: str,
    reviewer_name: str,
    reviewer_email: str,
) -> str:
    """Format QA review as a JIRA comment."""
    lines = [
        f"QA Review by {reviewer_name} <{reviewer_email}>",
        f"Assisted-by: Claude (Anthropic) via aspen-triage",
        "",
        f"**Overall: {result.overall_verdict.replace('_', ' ').title()}**",
        "",
        result.summary,
        "",
    ]

    if result.strengths:
        lines.append("**Strengths:**")
        for s in result.strengths:
            lines.append(f"- {s}")
        lines.append("")

    if result.issues:
        lines.append("**Issues:**")
        for issue in result.issues:
            lines.append(f"- {issue}")
        lines.append("")

    if result.suggested_followups:
        lines.append("**Follow-ups needed:**")
        for f in result.suggested_followups:
            lines.append(f"- {f}")
        lines.append("")

    lines.append(f"**Testing notes:** {result.testing_notes}")

    return "\n".join(lines)


def post_jira_comment(jira_url: str, email: str, api_token: str, issue_key: str, comment: str) -> None:
    """Post a comment to a JIRA issue. Requires email + API token."""
    import base64
    import httpx

    auth_b64 = base64.b64encode(f"{email}:{api_token}".encode()).decode()

    # JIRA v3 requires ADF (Atlassian Document Format) for comments
    adf_body = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": para}],
            }
            for para in comment.split("\n") if para
        ],
    }

    resp = httpx.post(
        f"{jira_url}/rest/api/3/issue/{issue_key}/comment",
        json={"body": adf_body},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth_b64}",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
