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

## Scripts

### `scripts/framer_templates.py`
Monitors [Framer Marketplace](https://www.framer.com/marketplace/templates/?sort=recent) for new templates.

- **Source:** defuddle.md renders the page and returns clean Markdown (Framer loads templates client-side, so raw HTML has no template data)
- **State:** Notion DB `Framer Templates` (ID in `NOTION_DATABASE_ID`)
- **Notifications:** Discord webhook on each new template
- **First run:** seeds the DB silently — no Discord spam
- **Fields tracked:** title, slug, URL, author, price, discovered date

**Deferred improvements:**
- Categories/tags — Framer may expose these but they weren't visible in defuddle's markdown output; worth re-checking if the page structure changes
- Pagination — defuddle renders what Framer shows on initial load; if the marketplace lazy-loads beyond the first batch, older items on a given run could be missed (low risk since we sort=recent and run periodically)
- Existing Notion records lack the `Thumbnail` URL property — only new records saved after this change will include it. A one-time backfill via the Notion API would populate old rows, but was skipped as low priority.
- Template thumbnail URL — not captured currently; could enrich Discord notifications (open PR: #8)
- Pagination — defuddle renders what Framer shows on initial load; if the marketplace lazy-loads beyond the first batch, older items on a given run could be missed (low risk since we sort=recent and run periodically)
- HTTP retry logic — transient network errors (defuddle, Notion, Discord) cause the whole run to abort; could add simple exponential backoff. Not added to keep stdlib-only code simple; scheduler will retry on next scheduled run
- Retry logic for transient HTTP failures — a simple retry with backoff on `urllib.error.URLError` would improve resilience; skipped to keep the script minimal
- Richer HTTP error reporting — printing the response body on Notion API errors (4xx/5xx) would aid debugging; skipped as the existing error messages are sufficient for now
- Author slug capture — the regex processes author URLs but discards the slug portion; could be stored as a separate field for deduplication or linking

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
5. Set up a scheduled task pointing at the new script
