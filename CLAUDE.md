# framerlabs-automations

A public community repository of automations run by Claude on a scheduled task runner.
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

- **Tier 1 — GitHub Actions cron** (every 15 minutes): Runs Python monitoring scripts automatically. No LLM needed, no token cost. Defined in `.github/workflows/`. Secrets are stored as GitHub Actions repository secrets.
- **Tier 2a — Claude Code VM — self-improvement** (1x/day): Reviews code and `logs/errors.jsonl` for improvements, checks for existing open PRs, and implements self-contained fixes or enhancements. Defined in `SCHEDULER.md`.
- **Tier 2b — Claude Code VM — leads reviewer** (hourly, Haiku): Reviews pending Reddit leads with reasoning, approves or rejects each one, and notifies Discord for approved leads. Defined in `REDDIT_LEADS_REVIEWER.md`.

### Runtime
Scripts are written in **Python 3** (stdlib only, no pip dependencies).
Node.js is available but has no DNS access in the scheduler VM — do not use it for network calls.

### State persistence
Each script uses a **Notion database** to track state between runs.
The Notion REST API is called directly (Bearer token auth) — no MCP, no ORM.
Notion DB IDs are stored as GitHub Actions repository secrets.

### Secrets
Stored as GitHub Actions repository secrets. For local development, create a `.env` file
at the repo root using `.env.example` as a template — it is gitignored and never committed.
Never log or echo secret values.

### Error logging
Scripts append structured errors to `logs/errors.jsonl` (one JSON object per line).
Each entry has: `timestamp` (ISO 8601 UTC), `script` name, `severity` (`warning` or `error`),
`message`, and optional `context` dict. GitHub Actions commits this file after each run.
The Tier 2a self-improvement session reads it locally (via `git pull`) to identify recurring
issues and propose fixes. Critical errors are also sent to `DISCORD_ALERTS_WEBHOOK_URL`
for immediate visibility.

Log rotation: the self-improvement session removes entries older than 7 days after reading
them, then commits the trimmed file.

### Notifications
- **Data notifications** (`DISCORD_WEBHOOK_URL_TEMPLATES`, `DISCORD_WEBHOOK_URL_LEADS`): new discoveries. Templates send a grouped summary embed (templates organised by inferred category) followed by one detail embed per template. Each script has its own webhook/channel.
- **X/Twitter** (`TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_TOKEN_SECRET`): templates are also posted as a tweet (max 280 chars) with a category summary and template list. Silently skipped if credentials are not configured.
- **System alerts** (`DISCORD_ALERTS_WEBHOOK_URL`): system-level warnings and errors (e.g. RSC parse failure, unexpected API errors). Separate channel so operational issues don't get lost in data traffic. All scripts must use `DISCORD_ALERTS_WEBHOOK_URL` for system alerts, not the data webhooks.

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
- **Notifications:** Discord grouped summary embed (by category) + one detail embed per template; optionally posts to X/Twitter (skipped if credentials not set)
- **Category inference:** categories are inferred from template title/meta_title via keyword matching (e.g. "Restaurant" → Food & Dining, "SaaS" → SaaS & Tech). Categories are not available in the Framer RSC payload. The inferred category is stored as a `select` field in Notion.
- **First run:** seeds the DB silently — no Discord/X notifications
- **Fields tracked:** title, slug, URL, author, author URL, price, category, discovered datetime, published datetime
- **Pagination:** fetches up to 2 pages (40 templates) per run; pages are cumulative (`?page=N` returns items 1–N×20), stops early when a page yields fewer than 20 new items

**Deferred improvements:**
- RSC format is an internal Next.js mechanism — Framer could change the response structure without notice. When parsing yields < 5 templates a Discord alert is sent to `DISCORD_ALERTS_WEBHOOK_URL`, a `body_preview` (first 500 chars of the last page) and a `parse_errors` count (JSON parse failures from `_extract_json_object`) are logged to `logs/errors.jsonl` to aid diagnosis; a non-zero `parse_errors` with zero templates suggests a JSON structure change rather than an absent key; inspect the raw RSC payload and update `_extract_json_object` / the search key if needed
- Additional RSC search key variants — the script now automatically tries `"templateItem":` and `"marketplaceItem":` as fallbacks if the primary `"item":` key yields < 5 results, and uses whichever key produces the most templates. If Framer adopts a completely different key, `_find_candidate_rsc_keys` scans the RSC body for JSON objects containing both `"id":` and `"slug":` and logs the key names immediately preceding them as `candidate_keys` in `logs/errors.jsonl` (only when 0 templates and 0 parse errors, indicating a new unknown key rather than a JSON structure change). Add the reported key as a new entry in `_RSC_FALLBACK_KEYS` to restore parsing.
- Richer HTTP error reporting — when `save_to_notion` raises an `HTTPError` (e.g. 400 Bad Request), the Notion API response body (first 500 chars) is now captured and logged as `notion_response` in `logs/errors.jsonl`, matching the same pattern used by `reddit_leads.py`. Non-HTTP exceptions still log `slug` and `error` string only.
- Category inference accuracy — keyword matching may miscategorise edge cases; an LLM-based approach could be added if accuracy becomes important. The inferred category is now persisted to Notion (as a `select` field) so miscategorisations are visible and correctable in the DB.
- Additional `CATEGORY_KEYWORDS` entries (e.g. `event`, `wedding`, `fintech`) — could reduce "Other" categorisations; skipped as the keyword list is already broad and new entries would need data from real Framer Marketplace templates to validate accuracy.

---

### `scripts/reddit_leads.py`
Monitors Reddit RSS feeds across 43 subreddits for potential Framer freelance leads.

**Two-phase design:**
- **Phase 1 — Light filter** (this script, runs every 15 min via GitHub Actions): Fetches RSS feeds,
  applies keyword-based filtering, deduplicates against Notion, saves candidates as `"pending"`.
  No Discord notifications, no LLM reasoning.
- **Phase 2 — Claude review** (hourly dedicated session on Haiku, see `REDDIT_LEADS_REVIEWER.md`):
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

**Partial failure alerting:** If >50% of subreddit feeds fail to fetch (e.g. Reddit rate-limiting or a partial network issue), a warning is sent to `DISCORD_ALERTS_WEBHOOK_URL`. If all feeds fail, an error-level alert is sent instead.

**Deferred improvements:**
- Smarter dedup — currently one Notion API call per filtered post; could batch with OR filters
  once Notion supports them natively
- Score/rank leads — could add a rough confidence score before saving to help the reviewer
  prioritise; skipped as Claude's reasoning handles prioritisation naturally
- Shared utilities refactor — `load_dotenv`, `_retry`, `_should_retry`, `http_get`, `http_post`
  are duplicated between `framer_templates.py` and `reddit_leads.py`; could be extracted to a
  shared module, but intentional isolation keeps scripts independently runnable without import
  dependencies; skipped to avoid a larger multi-file refactor with no immediate correctness benefit
- Expanded `_JOB_SEEKER_SIGNALS` — additional phrases like `"open to work"` could reduce false positives; skipped as Claude's Phase 2 review already filters these out reliably, and adding overly broad exclusions risks dropping genuine leads
- Persistent 400 tracking across runs — implemented: on any non-retriable HTTP error (e.g. 400 Bad Request) when saving a lead, `save_failed_sentinel_to_notion` writes a minimal page with `Status: failed` and the URL to Notion so future dedup checks skip the URL; the `_is_valid_iso8601_date` guard prevents invalid `post_date` values from causing 400s in the first place
- Dedup-check error isolation — implemented: `url_exists_in_notion` is now in its own `try/except` block in `main()`, separate from `save_lead_to_notion`; a transient Notion error during the dedup check is logged as a warning and the post is skipped, instead of being misclassified as a save error and incorrectly writing a failed-sentinel that would permanently blacklist the URL

---

## Environment variables

| Variable | Description |
|---|---|
| `NOTION_TOKEN` | Notion integration token (`ntn_xxx`) |
| `NOTION_DATABASE_ID` | ID of the Framer Templates Notion DB |
| `NOTION_REDDIT_LEADS_DB_ID` | ID of the Reddit Leads Notion DB |
| `DISCORD_WEBHOOK_URL_TEMPLATES` | Discord webhook for new template notifications |
| `DISCORD_WEBHOOK_URL_LEADS` | Discord webhook for approved Framer leads (separate channel) |
| `DISCORD_ALERTS_WEBHOOK_URL` | Discord webhook for system-level errors and warnings (separate channel) |
| `TWITTER_API_KEY` | Twitter/X API consumer key (optional — X posting skipped if not set) |
| `TWITTER_API_SECRET` | Twitter/X API consumer secret |
| `TWITTER_ACCESS_TOKEN` | Twitter/X user access token |
| `TWITTER_ACCESS_TOKEN_SECRET` | Twitter/X user access token secret |

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
- **Keep CLAUDE.md accurate**: when a PR changes behavior described in this file (e.g. notification format, architecture, script capabilities), update the relevant sections in the same PR. This file is for current instructions, not history — don't add changelog entries.

## Adding a new script

1. Create `scripts/<name>.py`
2. Create a Notion DB under the Claude Automations parent page
3. Add the DB ID as a GitHub Actions repository secret
4. Add a `"<name>": "python3 scripts/<name>.py"` entry to `package.json` scripts
5. Add the script to the GitHub Actions cron workflow (`.github/workflows/framer-monitor.yml` or a new workflow file if it needs a different schedule)
6. Create `tests/test_<name>.py` covering the script's core functions

**Notion DB schema:** When adding a new tracked field to a script, update the Notion DB schema in the same PR via MCP (use the `notion-update-data-source` tool). Do not add runtime schema-sync logic — schema updates belong at implementation time, not on every run.
