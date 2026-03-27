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
- Categories/tags — Framer may expose these but they weren't visible in defuddle's markdown output (the `## Categories` heading appears but is empty); worth re-checking if the page structure changes
- Thumbnail in Notion — thumbnail URL is extracted but not stored in Notion; would require adding a `Thumbnail` URL property to the Notion DB schema first
- Pagination — defuddle renders what Framer shows on initial load; if the marketplace lazy-loads beyond the first batch, older items on a given run could be missed (low risk since we sort=recent and run periodically)
- Remix/purchase count — not exposed in defuddle's markdown output

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

The scheduler runs this repo periodically. Current setup: one task per script.
Future plan: a conductor script (`run.py`) that checks per-script interval config and runs what's due — enabling one scheduled task to manage the whole ecosystem.

### Scheduler prompt template
When setting up a scheduled task for a script, use this pattern:

```
Pull latest main from fredm00n/claude-automations.
Run: python3 scripts/<script_name>.py

Review the output and the script code. Look for improvements:
- Parsing robustness
- New useful fields to track
- Edge cases
- Any enhancements that fit the goal

If improvements are found:
1. Create a branch: claude/improve-<script>-<random>
2. Implement changes
3. Commit, push, open a PR against main for review
4. Update the script's **Deferred improvements** section in CLAUDE.md with anything considered but not implemented, and why — commit this to the same branch

If no improvements needed, exit cleanly.
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
