import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    jira_key TEXT NOT NULL UNIQUE,
    jira_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL,
    status_category TEXT,
    issue_type TEXT NOT NULL,
    priority TEXT,
    resolution TEXT,
    components TEXT,
    labels TEXT,
    creator TEXT,
    assignee TEXT,
    reporter TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    resolved TEXT,
    url TEXT NOT NULL,
    harvested_at TEXT NOT NULL,
    embedding BLOB,
    embedded_at TEXT,
    embed_text_hash TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    jira_comment_id TEXT NOT NULL UNIQUE,
    author TEXT,
    body TEXT,
    created TEXT NOT NULL,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    added_at TEXT NOT NULL,
    UNIQUE(group_id, issue_id)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    model TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(issue_id)
);
CREATE TABLE IF NOT EXISTS code_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    file_path TEXT NOT NULL,
    original_content TEXT,
    fixed_content TEXT NOT NULL,
    explanation TEXT,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_fix_meta (
    issue_id INTEGER PRIMARY KEY REFERENCES issues(id),
    commit_message TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pr_url TEXT,
    pr_number INTEGER,
    skip_reason TEXT
);
CREATE TABLE IF NOT EXISTS qa_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    model TEXT NOT NULL,
    review_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harvest_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_harvested_at TEXT,
    total_issues INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    picture_url TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    github_token TEXT,
    github_fork_owner TEXT,
    jira_email TEXT,
    jira_api_token TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_type ON issues(issue_type);
CREATE INDEX IF NOT EXISTS idx_issues_embed_hash ON issues(embed_text_hash);
CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(issue_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_issue ON group_members(issue_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_issue ON recommendations(issue_id);
CREATE INDEX IF NOT EXISTS idx_code_fixes_issue ON code_fixes(issue_id);
CREATE INDEX IF NOT EXISTS idx_qa_reviews_issue ON qa_reviews(issue_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < 2:
        try:
            conn.execute("ALTER TABLE code_fix_meta ADD COLUMN skip_reason TEXT")
        except sqlite3.OperationalError:
            pass
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
