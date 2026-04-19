# aspen-triage

Semantic triage tool for the [Aspen Discovery](https://aspendiscovery.org/) JIRA.

Harvests issues from https://aspen-discovery.atlassian.net, provides semantic search with AI-generated verdicts, per-issue fix recommendations using Aspen's security/QA guidelines, and AI code generation with one-click Draft PR creation on `Aspen-Discovery/aspen-discovery`.

## Features

- **Harvest** — JIRA REST API v3 with cursor pagination, 18 months default
- **Semantic search** — BAAI/bge-small-en-v1.5 embeddings
- **AI verdicts** — Claude tags each result: in_progress, resolved_done, likely_duplicate, etc.
- **Bug-number search** — Enter `DIS-1234` to jump straight to the issue
- **Issue grouping** — Manually cluster related issues
- **AI recommendations** — Per-issue fix plans using Aspen QA guidelines + known-issue list
- **Code generation** — Claude generates fixes shown as git-style diffs
- **GitHub PR creation** — One-click Draft PR with JIRA key in title/branch
- **Google OAuth** — Domain-restricted authentication
- **Sortable, filterable issue browser**

## Quick start

```bash
cp .env.example .env
# edit .env — set ASPEN_TRIAGE_JIRA_EMAIL, ASPEN_TRIAGE_JIRA_API_TOKEN,
# ASPEN_TRIAGE_ANTHROPIC_API_KEY, ASPEN_TRIAGE_GITHUB_TOKEN

pip install -e .
aspen-triage harvest
aspen-triage embed
aspen-triage serve

# Or with Docker
docker compose up -d
docker compose run --rm triage harvest
docker compose run --rm triage embed
```

## License

MIT
