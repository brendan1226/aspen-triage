import difflib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import JIRA_URL, GITHUB_REPO, settings
from .db import connect, init_db

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="aspen-triage", version="0.0.1")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

oauth = OAuth()
if settings.google_client_id:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compute_diff(original: str, modified: str, file_path: str) -> list[dict]:
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(orig_lines, mod_lines, fromfile=f"a/{file_path}", tofile=f"b/{file_path}")
    lines: list[dict] = []
    for raw in diff:
        text = raw.rstrip("\n")
        if text.startswith("+++") or text.startswith("---"):
            lines.append({"type": "header", "text": text})
        elif text.startswith("@@"):
            lines.append({"type": "hunk", "text": text})
        elif text.startswith("+"):
            lines.append({"type": "add", "text": text})
        elif text.startswith("-"):
            lines.append({"type": "del", "text": text})
        else:
            lines.append({"type": "ctx", "text": text})
    return lines


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = None

    if not settings.google_client_id:
        request.state.user = {"id": 0, "email": "local", "name": "Local Dev", "picture_url": ""}
        return await call_next(request)

    public_prefixes = ("/login", "/auth/", "/healthz", "/static")
    if any(request.url.path.startswith(p) for p in public_prefixes):
        return await call_next(request)

    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")

    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        request.session.clear()
        return RedirectResponse("/login")

    request.state.user = dict(row)
    return await call_next(request)

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if not settings.google_client_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": error, "allowed_domains": settings.allowed_domains},
    )


@app.get("/auth/start")
async def auth_start(request: Request):
    if not settings.google_client_id:
        return RedirectResponse("/")
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not settings.google_client_id:
        return RedirectResponse("/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        return RedirectResponse(f"/login?error={quote(str(e))}")

    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse("/login?error=No+user+info")

    email = user_info.get("email", "")
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    allowed = [d.strip() for d in settings.allowed_domains.split(",")]
    if domain not in allowed:
        return RedirectResponse(f"/login?error=Domain+{quote(domain)}+not+allowed")

    now = _utc_now_iso()
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (email, name, picture_url, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name, picture_url = excluded.picture_url,
                last_login_at = excluded.last_login_at
            """,
            (email, user_info.get("name", ""), user_info.get("picture", ""), now, now),
        )
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()

    request.session["user_id"] = row["id"]
    return RedirectResponse("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login" if settings.google_client_id else "/")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _get_user_config(request: Request) -> dict:
    """Get per-user GitHub + JIRA credentials, falling back to system config."""
    user = request.state.user
    out = {
        "github_token": settings.github_token,
        "github_fork_owner": settings.github_fork_owner,
        "jira_email": settings.jira_email,
        "jira_api_token": settings.jira_api_token,
    }
    if user and user.get("id"):
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT github_token, github_fork_owner, jira_email, jira_api_token FROM user_settings WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if row:
            if row["github_token"]:
                out["github_token"] = row["github_token"]
                out["github_fork_owner"] = row["github_fork_owner"] or out["github_fork_owner"]
            if row["jira_email"] and row["jira_api_token"]:
                out["jira_email"] = row["jira_email"]
                out["jira_api_token"] = row["jira_api_token"]
    return out


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: bool = False):
    user = request.state.user
    current = {}
    gh_display = None
    jira_display = None

    if user and user.get("id"):
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT github_token, github_fork_owner, jira_email, jira_api_token FROM user_settings WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if row:
            current = dict(row)
            gh = current.get("github_token") or ""
            gh_display = f"...{gh[-4:]}" if len(gh) > 4 else ("set" if gh else None)
            jt = current.get("jira_api_token") or ""
            jira_display = f"...{jt[-4:]}" if len(jt) > 4 else ("set" if jt else None)
            current["github_token"] = ""
            current["jira_api_token"] = ""

    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={
            "user": user, "current_settings": current,
            "gh_display": gh_display, "jira_display": jira_display,
            "saved": saved,
        },
    )


@app.post("/settings")
def save_settings(
    request: Request,
    github_token: str = Form(""),
    github_fork_owner: str = Form(""),
    jira_email: str = Form(""),
    jira_api_token: str = Form(""),
):
    user = request.state.user
    if not user or not user.get("id"):
        return RedirectResponse("/login")

    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        existing = conn.execute(
            "SELECT github_token, jira_api_token FROM user_settings WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        if not github_token.strip() and existing:
            github_token = existing["github_token"] or ""
        if not jira_api_token.strip() and existing:
            jira_api_token = existing["jira_api_token"] or ""

        conn.execute(
            """
            INSERT INTO user_settings (user_id, github_token, github_fork_owner, jira_email, jira_api_token, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                github_token = excluded.github_token,
                github_fork_owner = excluded.github_fork_owner,
                jira_email = excluded.jira_email,
                jira_api_token = excluded.jira_api_token,
                updated_at = excluded.updated_at
            """,
            (user["id"], github_token.strip(), github_fork_owner.strip(),
             jira_email.strip(), jira_api_token.strip(), now),
        )

    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="about.html")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        open_issues = conn.execute("SELECT COUNT(*) FROM issues WHERE status_category != 'done'").fetchone()[0]
        embedded_count = conn.execute("SELECT COUNT(*) FROM issues WHERE embedding IS NOT NULL").fetchone()[0]
        total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        harvest = conn.execute("SELECT * FROM harvest_state WHERE id = 1").fetchone()

        by_type = conn.execute(
            """
            SELECT issue_type, COUNT(*) as cnt,
                   SUM(CASE WHEN status_category != 'done' THEN 1 ELSE 0 END) as open_cnt
            FROM issues GROUP BY issue_type ORDER BY cnt DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "total": total, "open_issues": open_issues,
            "embedded_count": embedded_count, "total_comments": total_comments,
            "harvest": dict(harvest) if harvest else None,
            "by_type": [dict(r) for r in by_type],
            "has_anthropic_key": bool(settings.anthropic_api_key),
        },
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", k: int = 10) -> HTMLResponse:
    q = (q or "").strip()
    if not q:
        return templates.TemplateResponse(
            request=request, name="search.html",
            context={"query": "", "has_anthropic_key": bool(settings.anthropic_api_key)},
        )

    # If query is a JIRA key (e.g. DIS-1234), jump straight there
    upper = q.upper()
    if upper.startswith("DIS-") and upper[4:].isdigit():
        init_db(settings.db_path)
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT id FROM issues WHERE jira_key = ?", (upper,)).fetchone()
        if row:
            return RedirectResponse(f"/issues/{row['id']}", status_code=302)
    elif q.isdigit():
        # Allow bare number — assume DIS-
        init_db(settings.db_path)
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT id FROM issues WHERE jira_key = ?", (f"DIS-{q}",)).fetchone()
        if row:
            return RedirectResponse(f"/issues/{row['id']}", status_code=302)

    k = max(1, min(k, 20))
    from .search import NoEmbeddingsError, search as semantic_search
    error = None
    results = []
    verdicts = []
    classified = False
    try:
        if settings.anthropic_api_key:
            from .classify import classify as run_classify
            results, verdicts = run_classify(
                settings.db_path, q, settings.embedding_model,
                settings.anthropic_api_key, settings.classification_model, top_k=k
            )
            classified = True
        else:
            results = semantic_search(settings.db_path, q, settings.embedding_model, top_k=k)
    except NoEmbeddingsError as e:
        error = str(e)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    verdicts_by_idx = {i: v for i, v in enumerate(verdicts) if i < len(results)}
    rows = [{
        **r,
        "verdict": (v := verdicts_by_idx.get(i)) and v.verdict,
        "rationale": v and v.rationale,
        "suggested_action": v and v.suggested_action,
    } for i, r in enumerate(results)]

    return templates.TemplateResponse(
        request=request, name="search.html",
        context={
            "query": q, "k": k, "rows": rows, "error": error,
            "classified": classified, "has_anthropic_key": bool(settings.anthropic_api_key),
            "model": settings.classification_model,
        },
    )


# ---------------------------------------------------------------------------
# Issue browser
# ---------------------------------------------------------------------------

SORT_COLUMNS = {
    "key": "i.jira_key",
    "summary": "i.summary",
    "status": "i.status",
    "type": "i.issue_type",
    "priority": "i.priority",
    "reporter": "i.reporter",
    "updated": "i.updated",
}


@app.get("/issues", response_class=HTMLResponse)
def issues_list(
    request: Request, issue_type: str = "", status_category: str = "open",
    priority: str = "", q: str = "", page: int = 1,
    sort: str = "updated", dir: str = "desc",
) -> HTMLResponse:
    init_db(settings.db_path)
    per_page = 50
    offset = (max(1, page) - 1) * per_page
    filters, params = [], []
    if issue_type:
        filters.append("i.issue_type = ?"); params.append(issue_type)
    if status_category == "open":
        filters.append("i.status_category != 'done'")
    elif status_category == "done":
        filters.append("i.status_category = 'done'")
    if priority:
        filters.append("i.priority = ?"); params.append(priority)
    if q:
        filters.append("i.summary LIKE ?"); params.append(f"%{q}%")
    where = "WHERE " + " AND ".join(filters) if filters else ""

    sort_col = SORT_COLUMNS.get(sort, "i.updated")
    sort_dir = "ASC" if dir == "asc" else "DESC"

    with connect(settings.db_path) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM issues i {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT i.id, i.jira_key, i.summary, i.status, i.status_category,
                       i.issue_type, i.priority, i.resolution, i.components,
                       i.reporter, i.assignee, i.created, i.updated, i.url
                FROM issues i {where}
                ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?""",
            [*params, per_page, offset]
        ).fetchall()
        type_options = conn.execute("SELECT DISTINCT issue_type FROM issues ORDER BY issue_type").fetchall()
        priority_options = conn.execute("SELECT DISTINCT priority FROM issues WHERE priority != '' ORDER BY priority").fetchall()
        groups = conn.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
    return templates.TemplateResponse(request=request, name="issues.html", context={
        "issues": [dict(r) for r in rows],
        "type_options": [r["issue_type"] for r in type_options],
        "priority_options": [r["priority"] for r in priority_options],
        "groups": [dict(g) for g in groups],
        "total": total, "page": page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "filter_type": issue_type, "filter_status": status_category,
        "filter_priority": priority, "filter_q": q,
        "sort": sort, "sort_dir": dir,
    })


@app.get("/issues/{issue_internal_id}", response_class=HTMLResponse)
def issue_detail(request: Request, issue_internal_id: int, error: str = "", posted: bool = False) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_internal_id,)).fetchone()
        if row is None:
            return HTMLResponse("Issue not found", status_code=404)
        issue_comments = conn.execute("SELECT * FROM comments WHERE issue_id = ? ORDER BY created", (issue_internal_id,)).fetchall()
        memberships = conn.execute("SELECT g.id, g.name FROM groups g JOIN group_members gm ON gm.group_id = g.id WHERE gm.issue_id = ?", (issue_internal_id,)).fetchall()
        all_groups = conn.execute("SELECT id, name FROM groups ORDER BY name").fetchall()

    labels = [l.strip() for l in (row["labels"] or "").split(",") if l.strip()]
    components = [c.strip() for c in (row["components"] or "").split(",") if c.strip()]

    from .recommend import get_stored_recommendation
    stored = get_stored_recommendation(settings.db_path, issue_internal_id)
    rec, rec_meta = None, None
    if stored:
        rec_obj, rec_model, rec_created = stored
        rec = rec_obj.model_dump()
        rec_meta = {"model": rec_model, "created_at": rec_created}

    from .codegen import get_stored_fixes
    code_fixes, fix_meta = get_stored_fixes(settings.db_path, issue_internal_id)
    for fix in code_fixes:
        fix["diff_lines"] = _compute_diff(
            fix.get("original_content") or "",
            fix.get("fixed_content") or "",
            fix.get("file_path", "unknown"),
        )

    user_cfg = _get_user_config(request)

    return templates.TemplateResponse(request=request, name="issue_detail.html", context={
        "issue": dict(row), "labels": labels, "components": components,
        "comments": [dict(c) for c in issue_comments],
        "memberships": [dict(m) for m in memberships],
        "all_groups": [dict(g) for g in all_groups],
        "rec": rec, "rec_meta": rec_meta,
        "code_fixes": code_fixes, "fix_meta": fix_meta,
        "has_anthropic_key": bool(settings.anthropic_api_key),
        "has_github_token": bool(user_cfg["github_token"]),
        "has_jira_auth": bool(user_cfg["jira_email"] and user_cfg["jira_api_token"]),
        "github_repo": GITHUB_REPO,
        "jira_url": JIRA_URL,
        "error": error,
        "posted": posted,
    })


# ---------------------------------------------------------------------------
# Issue actions
# ---------------------------------------------------------------------------

@app.post("/issues/{issue_internal_id}/recommend")
def generate_issue_recommendation(issue_internal_id: int) -> RedirectResponse:
    if not settings.anthropic_api_key:
        return RedirectResponse(f"/issues/{issue_internal_id}?error=No+Anthropic+API+key", status_code=303)
    try:
        from .recommend import generate_recommendation
        generate_recommendation(settings.db_path, issue_internal_id, settings.anthropic_api_key, settings.classification_model)
    except Exception as e:
        return RedirectResponse(f"/issues/{issue_internal_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/issues/{issue_internal_id}", status_code=303)


@app.post("/issues/{issue_internal_id}/generate-fix")
def generate_fix(request: Request, issue_internal_id: int) -> RedirectResponse:
    import traceback
    try:
        cfg = _get_user_config(request)
        if not cfg["github_token"]:
            raise ValueError("No GitHub token configured.")
        if not settings.anthropic_api_key:
            raise ValueError("No Anthropic API key configured.")
        print(f"[generate-fix] issue={issue_internal_id} starting...", flush=True)
        from .codegen import generate_code_fix
        generate_code_fix(settings.db_path, issue_internal_id, settings.anthropic_api_key, cfg["github_token"], settings.classification_model)
        print(f"[generate-fix] issue={issue_internal_id} completed", flush=True)
    except Exception as e:
        print(f"[generate-fix] issue={issue_internal_id} ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return RedirectResponse(f"/issues/{issue_internal_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/issues/{issue_internal_id}", status_code=303)


def _build_jira_comment_text(issue_internal_id: int, request: Request) -> tuple[dict, str]:
    """Return (issue_row, comment_text) for a JIRA post preview."""
    with connect(settings.db_path) as conn:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_internal_id,)).fetchone()
        fix_meta = conn.execute("SELECT * FROM code_fix_meta WHERE issue_id = ?", (issue_internal_id,)).fetchone()

    if issue is None:
        raise ValueError("Issue not found")

    from .recommend import get_stored_recommendation
    stored = get_stored_recommendation(settings.db_path, issue_internal_id)
    if stored is None:
        raise ValueError("No recommendation to post. Generate one first.")

    rec, rec_model, rec_created = stored

    user = request.state.user
    reviewer_name = user.get("name", "aspen-triage") if user else "aspen-triage"
    reviewer_email = user.get("email", "aspen-triage@bywatersolutions.com") if user else "aspen-triage@bywatersolutions.com"

    lines = [
        f"Recommendation authored by {reviewer_name} <{reviewer_email}>",
        f"Assisted-by: Claude ({rec_model}) via aspen-triage",
        "",
        f"Complexity: {rec.complexity}",
        "",
        "Summary:",
        rec.summary,
        "",
        "Fix approach:",
        rec.fix_approach,
        "",
    ]

    if rec.affected_areas:
        lines.append("Affected areas: " + ", ".join(rec.affected_areas))
        lines.append("")
    if rec.likely_files:
        lines.append("Likely files:")
        for f in rec.likely_files:
            lines.append(f"  - {f}")
        lines.append("")
    if rec.key_guidelines:
        lines.append("Key guidelines: " + "; ".join(rec.key_guidelines))
        lines.append("")
    if rec.test_plan:
        lines.append("Test plan:")
        lines.append(rec.test_plan)
        lines.append("")

    if fix_meta:
        fix_meta_d = dict(fix_meta)
        if fix_meta_d.get("skip_reason"):
            lines.append("---")
            lines.append("AI code generation was NOT performed. Reason:")
            lines.append(fix_meta_d["skip_reason"])
            lines.append("")
        elif fix_meta_d.get("pr_url"):
            lines.append("---")
            lines.append(f"Draft PR: {fix_meta_d['pr_url']}")
            lines.append("")

    lines.append(f"Suggested branch: {rec.suggested_branch_name}")

    return dict(issue), "\n".join(lines)


@app.get("/issues/{issue_internal_id}/post-to-jira", response_class=HTMLResponse)
def preview_jira_post(request: Request, issue_internal_id: int, error: str = "") -> HTMLResponse:
    """Show an editable preview of the JIRA comment before posting."""
    cfg = _get_user_config(request)
    has_jira = bool(cfg["jira_email"] and cfg["jira_api_token"])

    try:
        issue, comment_text = _build_jira_comment_text(issue_internal_id, request)
    except Exception as e:
        return RedirectResponse(f"/issues/{issue_internal_id}?error={quote(str(e))}", status_code=303)

    return templates.TemplateResponse(request=request, name="post_preview.html", context={
        "issue": issue,
        "comment_text": comment_text,
        "action_url": f"/issues/{issue_internal_id}/post-to-jira",
        "back_url": f"/issues/{issue_internal_id}",
        "target_name": "JIRA",
        "target_link_text": issue["jira_key"],
        "target_link_url": issue["url"],
        "has_auth": has_jira,
        "error": error,
    })


@app.post("/issues/{issue_internal_id}/post-to-jira")
def post_recommendation_to_jira(request: Request, issue_internal_id: int, comment: str = Form(...)) -> RedirectResponse:
    """Post the (possibly edited) comment to JIRA."""
    try:
        cfg = _get_user_config(request)
        if not cfg["jira_email"] or not cfg["jira_api_token"]:
            raise ValueError("No JIRA credentials configured. Add them in Settings.")

        with connect(settings.db_path) as conn:
            issue = conn.execute("SELECT jira_key FROM issues WHERE id = ?", (issue_internal_id,)).fetchone()
        if issue is None:
            raise ValueError("Issue not found")

        if not comment.strip():
            raise ValueError("Comment cannot be empty.")

        from .qa_review import post_jira_comment
        post_jira_comment(JIRA_URL, cfg["jira_email"], cfg["jira_api_token"], issue["jira_key"], comment)

    except Exception as e:
        return RedirectResponse(f"/issues/{issue_internal_id}/post-to-jira?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/issues/{issue_internal_id}?posted=1", status_code=303)


@app.post("/issues/{issue_internal_id}/create-pr")
def create_pr(request: Request, issue_internal_id: int) -> RedirectResponse:
    try:
        cfg = _get_user_config(request)
        if not cfg["github_token"]:
            raise ValueError("No GitHub token configured.")
        from .codegen import create_pr_from_fixes
        create_pr_from_fixes(settings.db_path, issue_internal_id, cfg["github_token"], cfg["github_fork_owner"])
    except Exception as e:
        return RedirectResponse(f"/issues/{issue_internal_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/issues/{issue_internal_id}", status_code=303)


# ---------------------------------------------------------------------------
# Issue group membership
# ---------------------------------------------------------------------------

@app.post("/issues/{issue_internal_id}/add-to-group")
def add_issue_to_group(issue_internal_id: int, group_id: int = Form(...)) -> RedirectResponse:
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        try:
            conn.execute("INSERT INTO group_members (group_id, issue_id, added_at) VALUES (?, ?, ?)", (group_id, issue_internal_id, now))
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (now, group_id))
        except Exception:
            pass
    return RedirectResponse(f"/issues/{issue_internal_id}", status_code=303)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@app.get("/groups", response_class=HTMLResponse)
def groups_list(request: Request) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT g.*, COUNT(gm.id) AS member_count FROM groups g LEFT JOIN group_members gm ON gm.group_id = g.id GROUP BY g.id ORDER BY g.updated_at DESC"
        ).fetchall()
    return templates.TemplateResponse(request=request, name="groups.html", context={"groups": [dict(r) for r in rows]})


@app.post("/groups")
def create_group(name: str = Form(...), description: str = Form("")) -> RedirectResponse:
    init_db(settings.db_path)
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO groups (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name.strip(), description.strip(), now, now),
        )
    return RedirectResponse(f"/groups/{cursor.lastrowid}", status_code=303)


@app.get("/groups/{group_id}", response_class=HTMLResponse)
def group_detail(request: Request, group_id: int) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if group is None:
            return HTMLResponse("Group not found", status_code=404)
        members = conn.execute(
            """SELECT i.id, i.jira_key, i.summary, i.status, i.issue_type, i.url, gm.added_at
               FROM group_members gm JOIN issues i ON gm.issue_id = i.id
               WHERE gm.group_id = ? ORDER BY gm.added_at DESC""",
            (group_id,)
        ).fetchall()
    return templates.TemplateResponse(request=request, name="group_detail.html", context={
        "group": dict(group),
        "members": [dict(m) for m in members],
    })


@app.post("/groups/{group_id}/members/{issue_id}/remove")
def remove_group_member(group_id: int, issue_id: int) -> RedirectResponse:
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        conn.execute("DELETE FROM group_members WHERE group_id = ? AND issue_id = ?", (group_id, issue_id))
        conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (now, group_id))
    return RedirectResponse(f"/groups/{group_id}", status_code=303)
