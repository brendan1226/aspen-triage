"""GitHub operations: fetch file, fork, branch, commit, PR."""

import base64
import time
from dataclasses import dataclass

import httpx

GITHUB_API = "https://api.github.com"


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aspen-triage/0.0.1",
        },
        timeout=30.0,
    )


def fetch_file(
    owner: str, repo: str, path: str, ref: str | None = None, token: str | None = None
) -> tuple[str, str]:
    headers: dict = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["X-GitHub-Api-Version"] = "2022-11-28"
    params: dict = {}
    if ref:
        params["ref"] = ref
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def get_default_branch(owner: str, repo: str, token: str) -> str:
    with _client(token) as client:
        resp = client.get(f"/repos/{owner}/{repo}")
        resp.raise_for_status()
        return resp.json().get("default_branch", "main")


def ensure_fork(upstream_owner: str, repo: str, fork_owner: str, token: str) -> str:
    with _client(token) as client:
        resp = client.get(f"/repos/{fork_owner}/{repo}")
        if resp.status_code == 200:
            return f"{fork_owner}/{repo}"
        client.post(f"/repos/{upstream_owner}/{repo}/forks")
        for _ in range(12):
            time.sleep(5)
            check = client.get(f"/repos/{fork_owner}/{repo}")
            if check.status_code == 200:
                return f"{fork_owner}/{repo}"
        raise RuntimeError(f"Fork {fork_owner}/{repo} not ready after 60s")


def get_branch_sha(owner: str, repo: str, branch: str, token: str) -> str:
    with _client(token) as client:
        resp = client.get(f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
        resp.raise_for_status()
        return resp.json()["object"]["sha"]


def sync_fork(fork_owner: str, repo: str, branch: str, token: str) -> None:
    with _client(token) as client:
        client.post(
            f"/repos/{fork_owner}/{repo}/merge-upstream",
            json={"branch": branch},
        )


def create_branch(owner: str, repo: str, branch_name: str, from_sha: str, token: str) -> None:
    with _client(token) as client:
        resp = client.post(
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": from_sha},
        )
        if resp.status_code == 422 and "Reference already exists" in resp.text:
            return
        resp.raise_for_status()


def commit_file(
    owner: str, repo: str, branch: str, path: str, content: str,
    message: str, token: str, file_sha: str | None = None,
) -> str:
    with _client(token) as client:
        if file_sha is None:
            existing = client.get(
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": branch},
            )
            if existing.status_code == 200:
                file_sha = existing.json()["sha"]

        body: dict = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if file_sha:
            body["sha"] = file_sha

        resp = client.put(f"/repos/{owner}/{repo}/contents/{path}", json=body)
        resp.raise_for_status()
        return resp.json()["commit"]["sha"]


@dataclass
class PRResult:
    url: str
    number: int
    html_url: str


def create_pull_request(
    upstream_owner: str, repo: str, title: str, body: str,
    head: str, base: str, token: str, draft: bool = True,
) -> PRResult:
    with _client(token) as client:
        resp = client.post(
            f"/repos/{upstream_owner}/{repo}/pulls",
            json={
                "title": title, "body": body,
                "head": head, "base": base, "draft": draft,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return PRResult(
            url=data["url"],
            number=data["number"],
            html_url=data["html_url"],
        )
