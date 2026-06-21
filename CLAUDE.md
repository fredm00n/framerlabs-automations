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
Shared utilities (HTTP retry, Notion helpers, alert suppression, dotenv) live in `scripts/shared.py` — both scripts import from it.
Node.js is available but has no DNS access in the scheduler VM — do not use it for network calls.

### Side-effect gating
The monitoring scripts run in two places: the production GitHub Actions cron and
the self-improvement cloud routine (which runs them to observe behaviour while
developing changes). **External side effects — Notion writes, Discord posts, and
tweets — only happen when `GITHUB_ACTIONS=true` (set automatically by Actions) or
`ENABLE_SIDE_EFFECTS=1` is set.** Any other run (a cloud VM, a local checkout) is
observe-only: it still fetches and reads, and prints `[observe-only] would …`
lines so an agent can see what *would* happen, but it never writes to the
production Notion DB or posts to the public Discord channels. This is enforced by
`shared.side_effects_enabled()`, checked inside `warn_discord` and every Notion-write
/ notification function in `main()`. The hourly reviewer's CLI commands
(`--update-status`, `--notify`) are deliberately **not** gated — they are explicit
operator actions that must work from the reviewer's runtime.

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
The commit step runs with `if: always()` so a non-zero script exit (e.g. a Notion
`SystemExit`) still persists the very errors that caused it. Pushes use exponential-backoff
retries (2s → 4s → 8s → 16s → 32s, up to 5 attempts); each attempt first runs
`git rebase --abort` so a rebase left in progress by a failed attempt cannot doom the rest.
Both guard against a race with the parallel workflow's push silently dropping log entries.
The Tier 2a self-improvement session reads it locally (via `git pull`) to identify recurring
issues and propose fixes. Critical errors are also sent to `DISCORD_ALERTS_WEBHOOK_URL`
for immediate visibility (only from the production cron — sandbox runs are observe-only,
see Side-effect gating above).

Log rotation: the self-improvement session removes entries older than 7 days after reading
them, then commits the trimmed file.

### Alert state
Cross-run Discord alert suppression is persisted in script-specific files under `state/`
(e.g. `state/alert_state-reddit_leads.json`), committed alongside `logs/errors.jsonl`.
Each file maps a stable alert key (e.g. `reddit_leads:dedup_notion_likely_down`) to the
ISO 8601 timestamp of the last successful send. `_warn_discord(message, dedup_key=...)`
checks this state before posting and skips the alert if the same key was sent within
`_ALERT_SUPPRESS_MINUTES` (60 min). Per-script paths avoid `git push` races between
workflows. State is recorded only on a successful Discord POST so a transient 5xx does
not suppress the next attempt when the webhook recovers.

### Notifications
- **Data notifications** (`DISCORD_WEBHOOK_URL_TEMPLATES`, `DISCORD_WEBHOOK_URL_LEADS`): new discoveries. Templates send one detail embed per template followed by a grouped summary embed at the end (templates organised by inferred category) — the recap sits at the bottom of the channel as a quick index of the batch. Each script has its own webhook/channel.
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

GitHub Actions runs this automatically on every pull request (see `.github/workflows/tests.yml`). A PR with failing CI must not be merged.

**Maintenance rules — apply to every PR, no exceptions:**

- **New script added** → create `tests/test_<name>.py` in the same PR
- **Existing function modified** → update or extend its tests in the same PR
- **New fields, API calls, or parsing logic added** → add tests covering the new paths
- Tests must pass locally before pushing: run the discover command above

---

## Scripts

### `scripts/framer_templates.py`
Monitors [Framer Marketplace](https://www.framer.com/community/marketplace/templates/?sort=newest) for new templates.

- **Source:** Framer's Next.js RSC endpoint — fetched directly with `Rsc: 1` header, returns structured component data (no headless browser needed). The **actual newest-templates grid** (the list a visitor sees under "Newest") is embedded in the RSC stream as a client-query cache: a JSON array under a `"data"` key whose elements are full template objects (`{"type":"template",…}`), newest-first (~120 items). The parser reads this array (`_parse_rsc_data_array`). A *separate* set of 12 curated `"resource":{…}` **featured** blocks is also parsed and merged (deduplicated by slug). **Parsing only the featured `"resource"` blocks — as the code did right after the June 2026 redesign — silently misses every newly published template**, since the featured carousel rarely changes; the `"data"` array is the authoritative newest feed.
- **Marketplace URLs (June 2026 upgrade):** The marketplace moved under a `/community/` path prefix. The script builds every link it emits against `https://www.framer.com/community/marketplace/...` (centralised in `_MARKETPLACE_*` constants). The old `/marketplace/...` paths only 301-redirect for templates/listings and **404 for author profiles**, so the `/community/` paths must be used directly.
- **State:** Notion DB `Framer Templates` (ID in `NOTION_DATABASE_ID`)
- **Notifications:** Discord one detail embed per template followed by a grouped summary embed (by category) at the end of the batch, so the recap sits at the bottom of the channel as a quick index; optionally posts to X/Twitter (skipped if credentials not set). **Per-run notify cap:** all new templates are saved to Notion, but at most `_MAX_NOTIFY_PER_RUN` (10) — the newest, since the feed is newest-first — are announced to Discord/X; any older remainder is silently backfilled. This prevents a notification flood when a backlog accumulates (after an outage, or the first run after a parser change that surfaces many previously missed templates).
- **Category inference:** categories are inferred from template title/meta_title via keyword matching (e.g. "Restaurant" → Food & Dining, "SaaS" → SaaS & Tech). Keywords are matched as whole words (`\b`-anchored, precompiled regexes) so short keywords like `'ai'` and `'app'` don't match inside unrelated words (e.g. "retail", "email", "wrapper"). Categories are not available in the Framer RSC payload. The inferred category is stored as a `select` field in Notion.
- **First run:** seeds the DB silently — no Discord/X notifications (the notify cap does not apply; the whole feed is backfilled silently)
- **Fields tracked:** title, slug, URL, author, author URL, price, category, discovered datetime, published datetime
- **Pagination:** attempts up to 2 pages per run and stops early when a page yields fewer than 12 new items. Since the June 2026 upgrade the RSC endpoint ignores the `?page=N` param and always returns the same payload, so in practice each run parses the full embedded newest grid (~120 templates) from page 1; page 2 adds 0 new items and triggers the early stop. The page-loop is retained as-is and would auto-recover if Framer restores cumulative paging.
- **RSC format (June 2026+):** Framer changed the RSC payload structure. Each template carries `attributes.price` (a **number**, or `null` for free — unlike the featured blocks' string price), `attributes.previewUrl`, `author.name/slug`, `media[0].url` for thumbnail, `introduction` for meta title, and plain ISO 8601 `publishedAt` (no `$D` prefix). Field mapping is shared by both parse paths via `_new_format_template`. Price is normalised by `_format_price` to a display string — `"$79"` for paid, `"Free"` for free — so the numeric/`null` data-array price renders with a currency symbol (not a bare `79`) and free templates show `"Free"` rather than a blank. The pre-2026 `"item":` key and field layout are kept as an automatic fallback so the parser recovers if Framer rolls back the format change.

**Deferred improvements** (still open):
- RSC format fragility — Framer could change the response structure without notice; the `"data"` array key, the featured `"resource"` blocks, and the legacy `"item"` keys are all tried, plus diagnostics, but a completely new encoding would need manual intervention
- Category inference accuracy — whole-word keyword matching now avoids substring false positives, but legitimate ambiguity (a title matching multiple categories) still resolves by `CATEGORY_KEYWORDS` order; an LLM-based approach could help
- Additional `CATEGORY_KEYWORDS` entries — could reduce "Other" categorisations

See [deferred_improvements.md](./deferred_improvements.md) for full historical context on all implemented and deferred items.

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
- `python3 scripts/reddit_leads.py --list-unnotified-approved` — prints JSON of leads that were approved in a previous session but whose Discord notification failed (Status=approved + Notified=False); used to retry `--notify` so they are not silently lost
- `python3 scripts/reddit_leads.py --update-status PAGE_ID STATUS NOTES` — approve/reject
- `python3 scripts/reddit_leads.py --notify PAGE_ID` — send Discord embed + mark notified

**Reddit cookie auth (avoids HTTP 429):** Reddit serves anonymous/cookieless RSS from a brutal anti-scraper bucket (~1 request/60s per IP), so fetching 43 feeds in a burst from a GitHub Actions datacenter IP produces a wall of HTTP 429s. Setting the `REDDIT_COOKIE` env var (a logged-out browser `edgebucket`+`loid` cookie) moves requests into the normal visitor bucket (~100 requests/10 min); it keys on the `loid`, not the IP, so it works from any runner — no proxy, VPS, or OAuth needed. The cookie is injected as a `Cookie` header by `http_get` **only for `reddit.com` hosts** (`_reddit_cookie_header` / `_is_reddit_host`), never on the Notion API calls that share the same wrapper. The variable is optional — unset, the script runs exactly as before (and will 429 under load). It is stored as a GitHub Actions secret and **never committed** (this repo is public). `loid` is multi-year; if 429s return, re-harvest from a logged-out browser (DevTools → Network → any reddit.com request → Request Headers → Cookie) and update the secret.

**Partial failure alerting:** If >50% of subreddit feeds fail to fetch (e.g. Reddit rate-limiting or a partial network issue), a warning is sent to `DISCORD_ALERTS_WEBHOOK_URL`. If all feeds fail, an error-level alert is sent instead.

**Deferred improvements** (still open):
- Smarter dedup — one Notion API call per filtered post; could batch with OR filters
- Score/rank leads — a confidence score could help the reviewer prioritise
- Expanded `_JOB_SEEKER_SIGNALS` — more phrases could reduce false positives
- Notion 404 retries — considered but not added (would mask genuine misconfiguration)

See [deferred_improvements.md](./deferred_improvements.md) for full historical context on all implemented and deferred items.

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
| `REDDIT_COOKIE` | Logged-out Reddit cookie (`edgebucket` + `loid`) injected on `reddit_leads.py` RSS fetches to avoid HTTP 429 throttling (optional — script still runs if unset). See the reddit_leads.py section. |
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
