"""Generate code fixes via Claude and create GitHub PRs."""

from datetime import datetime, timezone
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from .config import GITHUB_REPO
from .db import connect, init_db
from .github_ops import (
    PRResult, commit_file, create_branch, create_pull_request,
    ensure_fork, fetch_file, get_branch_sha, get_default_branch, sync_fork,
)
from .recommend import get_stored_recommendation


class FileFix(BaseModel):
    file_path: str = Field(..., description="Path relative to repo root.")
    explanation: str = Field(..., description="What changed and why, 2-3 sentences.")
    content: str = Field(..., description="The COMPLETE modified file content.")


class CodeFixResponse(BaseModel):
    fixes: list[FileFix]
    commit_message: str = Field(..., description="A concise commit message starting with the JIRA key: DIS-XXXX: description")


SYSTEM_PROMPT = """You are implementing a code fix for Aspen Discovery (open-source library discovery platform).

The codebase is PHP (web frontend under code/web/), Java (indexers under code/reindexer/ and code/aspen_app_server/), and Smarty templates.

You will receive:
1. A JIRA issue description
2. A fix recommendation
3. The current content of the file(s) to modify (may be truncated)

Your job: return the COMPLETE modified file content for each file that needs changes. Be surgical — change only what's needed.

Key Aspen conventions to follow (the codebase has historical issues with these, so be careful):
- Always parameterize SQL queries — use prepared statements, never string concatenation
- Always CSRF-protect state-changing forms
- Always filter template output (XSS prevention)
- Never use eval() or unserialize() on untrusted data
- Never use exec()/shell_exec() with user input
- PR/commit messages must start with the JIRA key: DIS-XXXX: description"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate_file(content: str, path: str, max_lines: int = 500) -> str:
    lines = content.splitlines()
    if len(lines) > max_lines:
        snippet = "\n".join(lines[:max_lines])
        return (
            f"### {path} (first {max_lines} of {len(lines)} lines)\n"
            f"```\n{snippet}\n```\n"
            f"(... {len(lines) - max_lines} more lines truncated)"
        )
    return f"### {path}\n```\n{content}\n```"


def generate_code_fix(
    db_path: Path,
    issue_internal_id: int,
    api_key: str,
    github_token: str,
    model: str = "claude-opus-4-6",
    max_files: int = 3,
) -> CodeFixResponse:
    init_db(db_path)
    stored = get_stored_recommendation(db_path, issue_internal_id)
    if stored is None:
        raise ValueError("No recommendation exists. Generate one first.")

    rec, _model, _created = stored

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Issue {issue_internal_id} not found")

    issue = dict(row)
    upstream_owner, repo = GITHUB_REPO.split("/", 1)
    default_branch = get_default_branch(upstream_owner, repo, github_token)

    max_lines = 500
    file_contents: list[dict] = []
    for path in rec.likely_files[:max_files]:
        try:
            content, sha = fetch_file(upstream_owner, repo, path, ref=default_branch, token=github_token)
            lines = content.splitlines()
            truncated = "\n".join(lines[:max_lines]) if len(lines) > max_lines else content
            file_contents.append({"path": path, "content": content, "truncated": truncated, "sha": sha})
        except Exception as e:
            file_contents.append({"path": path, "content": None, "truncated": None, "error": str(e)})

    truncated_context = []
    for fc in file_contents:
        if fc.get("content"):
            truncated_context.append(_truncate_file(fc["content"], fc["path"], max_lines=max_lines))
        else:
            truncated_context.append(f"### {fc['path']}\n(Could not fetch: {fc.get('error', 'unknown')})")

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"## Issue: {issue['jira_key']}\n"
                    f"**Summary:** {issue['summary']}\n"
                    f"**Type:** {issue['issue_type']}\n"
                    f"**Status:** {issue['status']}\n\n"
                    f"**Description:**\n{issue.get('description') or '(empty)'}\n\n"
                    f"---\n\n## Recommendation\n\n"
                    f"**Fix approach:** {rec.fix_approach}\n\n"
                    f"**Key guidelines:** {', '.join(rec.key_guidelines)}\n\n"
                    f"**Test plan:** {rec.test_plan}\n\n"
                    f"---\n\n## Current file contents\n\n"
                    + "\n\n".join(truncated_context)
                    + "\n\n---\n\nProduce the complete modified file content for each file."
                ),
            }
        ],
        output_format=CodeFixResponse,
    ) as stream:
        final = stream.get_final_message()

    fix = final.parsed_output
    if fix is None:
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        fix = CodeFixResponse.model_validate_json(text)

    now = _utc_now_iso()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM code_fixes WHERE issue_id = ?", (issue_internal_id,))
        for f in fix.fixes:
            conn.execute(
                """
                INSERT INTO code_fixes (issue_id, file_path, original_content, fixed_content, explanation, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_internal_id,
                    f.file_path,
                    next((fc.get("truncated") or fc.get("content") for fc in file_contents if fc["path"] == f.file_path), None),
                    f.content,
                    f.explanation,
                    model,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO code_fix_meta (issue_id, commit_message, model, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (issue_internal_id, fix.commit_message, model, now),
        )

    return fix


def get_stored_fixes(db_path: Path, issue_internal_id: int) -> tuple[list[dict], dict | None]:
    init_db(db_path)
    with connect(db_path) as conn:
        fixes = conn.execute(
            "SELECT * FROM code_fixes WHERE issue_id = ? ORDER BY id",
            (issue_internal_id,),
        ).fetchall()
        meta = conn.execute(
            "SELECT * FROM code_fix_meta WHERE issue_id = ?",
            (issue_internal_id,),
        ).fetchone()
    return [dict(f) for f in fixes], dict(meta) if meta else None


def create_pr_from_fixes(
    db_path: Path,
    issue_internal_id: int,
    github_token: str,
    fork_owner: str,
) -> PRResult:
    """Create a GitHub PR on Aspen-Discovery/aspen-discovery."""
    init_db(db_path)
    fixes, meta = get_stored_fixes(db_path, issue_internal_id)
    if not fixes:
        raise ValueError("No code fixes stored. Generate them first.")

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT jira_key, summary, url FROM issues WHERE id = ?",
            (issue_internal_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Issue {issue_internal_id} not found")

    issue = dict(row)
    jira_key = issue["jira_key"]
    jira_url = issue["url"]
    upstream_owner, repo = GITHUB_REPO.split("/", 1)
    default_branch = get_default_branch(upstream_owner, repo, github_token)

    rec_stored = get_stored_recommendation(db_path, issue_internal_id)
    branch_name = f"{jira_key.lower()}-fix"
    if rec_stored:
        rec, _, _ = rec_stored
        branch_name = rec.suggested_branch_name or branch_name

    ensure_fork(upstream_owner, repo, fork_owner, github_token)
    sync_fork(fork_owner, repo, default_branch, github_token)
    base_sha = get_branch_sha(fork_owner, repo, default_branch, github_token)
    create_branch(fork_owner, repo, branch_name, base_sha, github_token)

    commit_msg = (meta or {}).get("commit_message", f"{jira_key}: {issue['summary']}")
    for fix in fixes:
        commit_file(
            fork_owner, repo, branch_name,
            fix["file_path"], fix["fixed_content"],
            commit_msg, github_token,
        )

    pr_title = commit_msg if commit_msg.startswith(jira_key) else f"{jira_key}: {commit_msg}"
    pr_body = (
        f"Addresses [{jira_key}]({jira_url})\n\n"
        f"## Changes\n\n"
    )
    for fix in fixes:
        pr_body += f"- `{fix['file_path']}`: {fix['explanation']}\n"
    pr_body += (
        f"\n## Generated by\n\n"
        f"[aspen-triage](https://github.com/brendan1226/aspen-triage) — "
        f"AI-assisted fix based on issue analysis + Aspen QA guidelines. "
        f"Human review is required before merge.\n"
    )

    result = create_pull_request(
        upstream_owner, repo, pr_title, pr_body,
        head=f"{fork_owner}:{branch_name}",
        base=default_branch,
        token=github_token,
        draft=True,
    )

    now = _utc_now_iso()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE code_fix_meta SET pr_url = ?, pr_number = ? WHERE issue_id = ?",
            (result.html_url, result.number, issue_internal_id),
        )

    return result
