# claude-automations

A private repository of automations run by Claude on a scheduled task runner.
Each script monitors something, persists state in Notion, and notifies via Discord.

## Architecture

### Runtime
Scripts are written in **Python 3** (stdlib only, no pip dependencies).
Node.js is available but has no DNS access in the scheduler VM — do not use it for network calls.

### State persistence
Each script uses a **Notion database** to track state between runs.
The Notion REST API is called directly (Bearer token auth) — no MCP, no ORM.
Notion DB IDs live in `.env`.

### Secrets
Stored in `.env` at the repo root (committed — this is a private personal repo).
Never log or echo secret values. If the repo ever goes public, rotate all secrets first.

### Notifications
New discoveries are sent to a **Discord webhook** — one message per item, not batched.

---

## Running scripts locally

```bash
python3 scripts/framer_templates.py
```

No install step needed — stdlib only.

---

## Testing

Tests live in `tests/` and use Python's built-in `unittest` — no install step needed.

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

GitHub Actions runs this automatically on every push and pull request (see `.github/workflows/tests.yml`). A PR with failing CI must not be merged.

**Maintenance rules — apply to every PR, no exceptions:**

- **New script added** → create `tests/test_<name>.py` in the same PR
- **Existing function modified** → update or extend its tests in the same PR
- **New fields, API calls, or parsing logic added** → add tests covering the new paths
- Tests must pass locally before pushing: run the discover command above

---

## Scripts

### `scripts/framer_templates.py`
Monitors [Framer Marketplace](https://www.framer.com/marketplace/templates/?sort=recent) for new templates.

- **Source:** Framer's Next.js RSC endpoint — fetched directly with `Rsc: 1` header, returns structured component data including all templates sorted by recent (no headless browser needed)
- **State:** Notion DB `Framer Templates` (ID in `NOTION_DATABASE_ID`)
- **Notifications:** Discord webhook on each new template
- **First run:** seeds the DB silently — no Discord spam
- **Fields tracked:** title, slug, URL, author, price, discovered datetime, published datetime
- **Pagination:** fetches up to 2 pages (40 templates) per run; pages are cumulative (`?page=N` returns items 1–N×20), stops early when a page yields fewer than 20 new items

**Deferred improvements:**
- Categories/tags — previously noted as present in the RSC payload, but an inspection of the live payload (2026-03-27) found no category/tag fields at the item level. The RSC format may have changed, or categories may be on a different endpoint. Not pursued until confirmed present.
- Existing Notion records lack the `Thumbnail` and `Published` properties — only new records saved after their respective PRs include them. Backfill via Notion API is possible but skipped as low priority
- RSC format is an internal Next.js mechanism — Framer could change the response structure without notice. If parsing breaks (< 5 templates warning fires), inspect the raw RSC payload and update `_extract_json_object` / the `"item":{"id":` search key
- HTTP retry logic — transient network errors cause the whole run to abort; could add simple exponential backoff. Not added to keep stdlib-only code simple; scheduler will retry on next scheduled run
- Richer HTTP error reporting — printing the response body on Notion API errors (4xx/5xx) would aid debugging; skipped as the existing error messages are sufficient for now

---

## Environment variables (`.env`)

| Variable | Description |
|---|---|
| `NOTION_TOKEN` | Notion integration token (`ntn_xxx`) |
| `NOTION_DATABASE_ID` | ID of the Framer Templates Notion DB |
| `DISCORD_WEBHOOK_URL` | Discord webhook for new template notifications |

---

## Notion workspace

Parent page: [Claude Automations](https://www.notion.so/fredmoon/Claude-Automations-32f1f1c5c0b48095b4f2d993cedf2ad2)

Each script gets its own Notion database as a sub-page under this parent.

---

## Scheduled task

The scheduler runs this repo periodically. See [SCHEDULER.md](./SCHEDULER.md) for the full operational instructions (which scripts to run, self-improvement loop, PR conventions).

**Scheduler UI prompt** (set once, never changes):
```
Read CLAUDE.md and SCHEDULER.md, then follow the instructions in SCHEDULER.md.
```

---

## Development workflow

- All changes go through PRs — never push directly to `main`
- Branch naming: `claude/<description>-<random-suffix>`
- Each logical improvement = one PR
- The scheduler auto-creates PRs when it finds improvements; human reviews and merges

## Adding a new script

1. Create `scripts/<name>.py`
2. Create a Notion DB under the Claude Automations parent page
3. Add the DB ID to `.env`
4. Add a `"<name>": "python3 scripts/<name>.py"` entry to `package.json` scripts
5. Add `python3 scripts/<name>.py` to the Step 1 list in `SCHEDULER.md`
6. Create `tests/test_<name>.py` covering the script's core functions
7. Set up a scheduled task pointing at the new script

**Notion DB schema:** When adding a new tracked field to a script, update the Notion DB schema in the same PR via MCP (use the `notion-update-data-source` tool). Do not add runtime schema-sync logic — schema updates belong at implementation time, not on every run.
