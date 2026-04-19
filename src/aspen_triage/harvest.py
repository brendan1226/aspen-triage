"""Harvest issues and comments from the Aspen Discovery JIRA."""

import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx

from .config import JIRA_URL, JIRA_PROJECT
from .db import connect, init_db

REST_BASE = f"{JIRA_URL}/rest/api/3"
PAGE_LIMIT = 100

ISSUE_FIELDS = [
    "summary", "status", "issuetype", "priority", "resolution",
    "components", "labels", "creator", "assignee", "reporter",
    "created", "updated", "resolutiondate", "description",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_client(email: str, api_token: str) -> httpx.Client:
    auth_str = f"{email}:{api_token}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()
    return httpx.Client(
        base_url=REST_BASE,
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {auth_b64}",
            "User-Agent": "aspen-triage/0.0.1",
        },
        timeout=60.0,
    )


def _flatten_adf(doc) -> str:
    """Flatten Atlassian Document Format (ADF) to plain text.

    ADF is a nested structure; we only extract text content and basic
    structure (paragraphs, lists, code blocks).
    """
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    if not isinstance(doc, dict):
        return ""

    node_type = doc.get("type", "")
    text_parts: list[str] = []

    if "text" in doc:
        text_parts.append(doc["text"])

    for child in doc.get("content", []):
        text_parts.append(_flatten_adf(child))

    text = "".join(text_parts)

    # Add newlines after block-level nodes
    if node_type in ("paragraph", "heading", "listItem", "codeBlock"):
        text += "\n"
    elif node_type == "hardBreak":
        text = "\n"

    return text


def _fetch_issues(
    client: httpx.Client,
    jql: str,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Fetch issues using the v3 cursor-pagination endpoint /search/jql."""
    all_issues: list[dict] = []
    next_token: str | None = None
    page = 1

    while True:
        params: dict = {
            "jql": jql,
            "maxResults": PAGE_LIMIT,
            "fields": ",".join(ISSUE_FIELDS),
        }
        if next_token:
            params["nextPageToken"] = next_token

        resp = client.get("/search/jql", params=params)
        resp.raise_for_status()
        data = resp.json()

        issues = data.get("issues", [])
        if on_page is not None:
            on_page(page, len(issues))
        all_issues.extend(issues)

        if data.get("isLast") or not issues:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        page += 1

    return all_issues


def _fetch_comments(client: httpx.Client, issue_key: str, max_retries: int = 3) -> list[dict]:
    """Fetch all comments for an issue."""
    import time
    for attempt in range(max_retries):
        try:
            resp = client.get(f"/issue/{issue_key}/comment", params={"maxResults": 100})
            resp.raise_for_status()
            return resp.json().get("comments", [])
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                print(f"    Failed to fetch comments for {issue_key}: {e}", flush=True)
                return []
        except httpx.HTTPStatusError:
            return []
    return []


def upsert_issue(conn, issue: dict, harvested_at: str) -> int:
    fields = issue.get("fields", {})
    key = issue["key"]
    jira_id = issue["id"]

    status = fields.get("status", {})
    issue_type = fields.get("issuetype", {})
    priority = fields.get("priority", {}) or {}
    resolution = fields.get("resolution", {}) or {}
    creator = fields.get("creator", {}) or {}
    assignee = fields.get("assignee", {}) or {}
    reporter = fields.get("reporter", {}) or {}

    components = ",".join([c.get("name", "") for c in fields.get("components", [])])
    labels = ",".join(fields.get("labels", []))

    description = _flatten_adf(fields.get("description")).strip()

    url = f"{JIRA_URL}/browse/{key}"

    conn.execute(
        """
        INSERT INTO issues (
            jira_key, jira_id, summary, description, status, status_category,
            issue_type, priority, resolution, components, labels,
            creator, assignee, reporter, created, updated, resolved,
            url, harvested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(jira_key) DO UPDATE SET
            summary = excluded.summary,
            description = excluded.description,
            status = excluded.status,
            status_category = excluded.status_category,
            priority = excluded.priority,
            resolution = excluded.resolution,
            components = excluded.components,
            labels = excluded.labels,
            assignee = excluded.assignee,
            updated = excluded.updated,
            resolved = excluded.resolved,
            harvested_at = excluded.harvested_at
        """,
        (
            key, jira_id,
            fields.get("summary", ""),
            description,
            status.get("name", ""),
            (status.get("statusCategory") or {}).get("key", ""),
            issue_type.get("name", ""),
            priority.get("name", "") if priority else "",
            resolution.get("name", "") if resolution else "",
            components,
            labels,
            creator.get("displayName", ""),
            assignee.get("displayName", "") if assignee else "",
            reporter.get("displayName", "") if reporter else "",
            fields.get("created", ""),
            fields.get("updated", ""),
            fields.get("resolutiondate"),
            url,
            harvested_at,
        ),
    )
    row = conn.execute("SELECT id FROM issues WHERE jira_key = ?", (key,)).fetchone()
    return row["id"]


def upsert_comment(conn, internal_issue_id: int, comment: dict) -> None:
    author = (comment.get("author") or {}).get("displayName", "")
    body = _flatten_adf(comment.get("body")).strip()

    conn.execute(
        """
        INSERT INTO comments (issue_id, jira_comment_id, author, body, created, updated)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(jira_comment_id) DO UPDATE SET
            body = excluded.body,
            updated = excluded.updated
        """,
        (
            internal_issue_id,
            comment["id"],
            author,
            body,
            comment.get("created", ""),
            comment.get("updated", ""),
        ),
    )


def harvest(
    db_path: Path,
    email: str,
    api_token: str,
    months_back: int = 18,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Harvest JIRA issues. First run pulls last N months; later runs are incremental."""
    init_db(db_path)
    new_harvested_at = _utc_now_iso()
    counts = {"issues": 0, "new_issues": 0, "updated_issues": 0, "comments": 0}

    with connect(db_path) as conn:
        row = conn.execute("SELECT last_harvested_at FROM harvest_state WHERE id = 1").fetchone()
        last_harvest = row["last_harvested_at"] if row else None

    # Build JQL
    if last_harvest:
        # JQL date format: "yyyy/MM/dd HH:mm"
        try:
            dt = datetime.fromisoformat(last_harvest.replace("Z", "+00:00"))
            since_jql = dt.strftime("%Y-%m-%d %H:%M")
            jql = f'project = {JIRA_PROJECT} AND updated >= "{since_jql}" ORDER BY updated ASC'
        except Exception:
            jql = f'project = {JIRA_PROJECT} ORDER BY updated DESC'
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
        since_jql = cutoff.strftime("%Y-%m-%d")
        jql = f'project = {JIRA_PROJECT} AND updated >= "{since_jql}" ORDER BY updated ASC'

    print(f"  JQL: {jql}", flush=True)

    with _build_client(email, api_token) as client:
        print("  Fetching issues from JIRA...", flush=True)
        issues = _fetch_issues(client, jql, on_page=on_page)
        counts["issues"] = len(issues)

        if not issues:
            print("  No issues to process.", flush=True)
            return counts

        with connect(db_path) as conn:
            issue_id_map: dict[str, int] = {}
            for issue in issues:
                existing = conn.execute(
                    "SELECT id FROM issues WHERE jira_key = ?", (issue["key"],)
                ).fetchone()
                was_new = existing is None
                internal_id = upsert_issue(conn, issue, new_harvested_at)
                issue_id_map[issue["key"]] = internal_id
                if was_new:
                    counts["new_issues"] += 1
                else:
                    counts["updated_issues"] += 1

        print(f"  Fetching comments for {len(issues)} issues...", flush=True)
        with connect(db_path) as conn:
            for i, issue in enumerate(issues):
                internal_id = issue_id_map[issue["key"]]
                comments = _fetch_comments(client, issue["key"])
                for comment in comments:
                    upsert_comment(conn, internal_id, comment)
                    counts["comments"] += 1
                if (i + 1) % 50 == 0:
                    print(f"    {i + 1}/{len(issues)} issues processed, {counts['comments']} comments saved", flush=True)

            conn.execute(
                """
                INSERT INTO harvest_state (id, last_harvested_at, total_issues)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_harvested_at = excluded.last_harvested_at,
                    total_issues = excluded.total_issues
                """,
                (new_harvested_at, counts["issues"]),
            )

    return counts
