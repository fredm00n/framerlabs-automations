# claude-automations

A private repository of automations run by Claude on a scheduled task runner.
Each script monitors something, persists state in Notion, and notifies via Discord.

## Session modes

There are three types of sessions:

| Mode | Trigger | Instructions |
|---|---|---|
| **Scheduled — self-improvement** | Initial prompt: `"Read CLAUDE.md and SCHEDULER.md, then follow the instructions in SCHEDULER.md."` | Follow SCHEDULER.md |
| **Scheduled — leads review** | Initial prompt: `"Read CLAUDE.md and REDDIT_LEADS_REVIEWER.md, then follow the instructions in REDDIT_LEADS_REVIEWER.md."` | Follow REDDIT_LEADS_REVIEWER.md |
| **Manual** | Any other prompt / interactive chat | Ignore SCHEDULER.md and REDDIT_LEADS_REVIEWER.md entirely |

**Rule**: Only follow a scheduler file when your initial prompt explicitly tells you to. Never initiate the self-improvement loop or lead review during a manual session.

---

## Architecture

### Two-Tier Execution

- **Tier 1 — GitHub Actions cron** (every 2 hours): Runs Python monitoring scripts automatically. No LLM needed, no token cost. Defined in `.github/workflows/framer-monitor.yml`. Secrets are stored as GitHub Actions repository secrets.
- **Tier 2 — Claude Code VM** (1x/day): Reviews code and recent GitHub Actions run logs for improvements, checks for existing open PRs, and implements self-contained fixes or enhancements. Defined in `SCHEDULER.md`.

### Runtime
Scripts are written in **Python 3** (stdlib only, no pip dependencies).
Node.js is available but has no DNS access in the scheduler VM — do not use it for network calls.

### State persistence
Each script uses a **Notion database** to track state between runs.
The Notion REST API is called directly (Bearer token auth) — no MCP, no ORM.
Notion DB IDs live in `.env`.

### Secrets
Stored as GitHub Actions repository secrets. For local development, create a `.env` file
at the repo root using `.env.example` as a template — it is gitignored and never committed.
Never log or echo secret values.

### Notifications
- **Data notifications** (`DISCORD_WEBHOOK_URL`): new discoveries, one message per item, not batched.
- **System alerts** (`DISCORD_ALERTS_WEBHOOK_URL`): system-level warnings and errors (e.g. RSC parse failure, unexpected API errors). Separate channel so operational issues don't get lost in data traffic. All scripts must use `DISCORD_ALERTS_WEBHOOK_URL` for system alerts, not `DISCORD_WEBHOOK_URL`.

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
- RSC format is an internal Next.js mechanism — Framer could change the response structure without notice. When parsing yields < 5 templates a Discord alert is sent to `DISCORD_ALERTS_WEBHOOK_URL`; inspect the raw RSC payload and update `_extract_json_object` / the `"item":{"id":` search key
- HTTP retry logic — transient network errors cause the whole run to abort; could add simple exponential backoff. Not added to keep stdlib-only code simple; scheduler will retry on next scheduled run
- Richer HTTP error reporting — printing the response body on Notion API errors (4xx/5xx) would aid debugging; skipped as the existing error messages are sufficient for now

---

### `scripts/reddit_leads.py`
Monitors Reddit RSS feeds across 43 subreddits for potential Framer freelance leads.

**Two-phase design:**
- **Phase 1 — Light filter** (this script, runs every 2h via GitHub Actions): Fetches RSS feeds,
  applies keyword-based filtering, deduplicates against Notion, saves candidates as `"pending"`.
  No Discord notifications, no LLM reasoning.
- **Phase 2 — Claude review** (daily dedicated session, see `REDDIT_LEADS_REVIEWER.md`):
  Reads pending leads from Notion, evaluates each with reasoning, marks approved/rejected,
  notifies Discord only for approved leads.

- **Source:** Reddit RSS feeds (`https://www.reddit.com/r/{subreddit}/.rss`)
- **State:** Notion DB `Reddit Leads` (ID in `NOTION_REDDIT_LEADS_DB_ID`)
- **Notifications:** Discord webhook `DISCORD_WEBHOOK_URL_LEADS` (approved leads only, sent by reviewer session)
- **Dedup:** Per-URL Notion query, only for posts that passed the light filter (~10–30/run)

**Subreddit categories and filter logic:**
- *Hiring subreddits* (`forhire`, `hiring`, `DesignJobs`, `freelance`, `HungryArtists`, `jobbit`):
  pass if any web/design signal present
- *Design/tech subreddits* (`framer`, `figma`, `webdev`, `web_design`, etc.):
  pass if framer + hiring signal, OR hiring + payment signal
- *No-code subreddits* (`nocode`, `Webflow`, `Bubble`, etc.):
  pass if hiring + web signal + payment signal
- *Business subreddits* (`startups`, `SaaS`, `Entrepreneur`, etc.):
  pass if website/landing page + hiring signal
- *Marketing/industry subreddits*: pass if website + hiring signal
- **Always exclude**: tutorials, feedback requests, complaints, framer pricing questions, job seekers

**Fields tracked:** Name, URL, Subreddit (select), Content, Status (select: pending/approved/rejected),
Post Date, Discovered, Review Notes, Notified (checkbox)

**CLI interface** (used by reviewer session):
- `python3 scripts/reddit_leads.py --list-pending` — prints JSON of pending leads
- `python3 scripts/reddit_leads.py --update-status PAGE_ID STATUS NOTES` — approve/reject
- `python3 scripts/reddit_leads.py --notify PAGE_ID` — send Discord embed + mark notified

**Deferred improvements:**
- Rate limiting — Reddit may throttle requests if many subreddits are fetched in rapid succession;
  could add a small delay between feeds. Not added to keep it simple; failures are logged.
- Smarter dedup — currently one Notion API call per filtered post; could batch with OR filters
  once Notion supports them natively
- Score/rank leads — could add a rough confidence score before saving to help the reviewer
  prioritise; skipped as Claude's reasoning handles prioritisation naturally

---

## Environment variables (`.env`)

| Variable | Description |
|---|---|
| `NOTION_TOKEN` | Notion integration token (`ntn_xxx`) |
| `NOTION_DATABASE_ID` | ID of the Framer Templates Notion DB |
| `NOTION_REDDIT_LEADS_DB_ID` | ID of the Reddit Leads Notion DB |
| `DISCORD_WEBHOOK_URL_TEMPLATES` | Discord webhook for new template notifications |
| `DISCORD_WEBHOOK_URL_LEADS` | Discord webhook for approved Framer leads (separate channel) |
| `DISCORD_ALERTS_WEBHOOK_URL` | Discord webhook for system-level errors and warnings (separate channel) |

---

## Notion workspace

Parent page: [Claude Automations](https://www.notion.so/fredmoon/Claude-Automations-32f1f1c5c0b48095b4f2d993cedf2ad2)

Each script gets its own Notion database as a sub-page under this parent.

---

## Scheduled task

Script execution (Tier 1) is handled by GitHub Actions cron — see `.github/workflows/framer-monitor.yml`. The Claude Code VM scheduler (Tier 2) handles only the self-improvement loop. See [SCHEDULER.md](./SCHEDULER.md) for the full operational instructions.

**Note**: Only follow SCHEDULER.md when the initial prompt explicitly instructs it (see [Session modes](#session-modes) above).

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
3. Add the DB ID to `.env` and as a GitHub Actions repository secret
4. Add a `"<name>": "python3 scripts/<name>.py"` entry to `package.json` scripts
5. Add the script to the GitHub Actions cron workflow (`.github/workflows/framer-monitor.yml` or a new workflow file if it needs a different schedule)
6. Create `tests/test_<name>.py` covering the script's core functions

**Notion DB schema:** When adding a new tracked field to a script, update the Notion DB schema in the same PR via MCP (use the `notion-update-data-source` tool). Do not add runtime schema-sync logic — schema updates belong at implementation time, not on every run.
