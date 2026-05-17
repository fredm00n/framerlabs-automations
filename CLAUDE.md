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
- **Notifications:** Discord one detail embed per template followed by a grouped summary embed (by category) at the end of the batch, so the recap sits at the bottom of the channel as a quick index; optionally posts to X/Twitter (skipped if credentials not set)
- **Category inference:** categories are inferred from template title/meta_title via keyword matching (e.g. "Restaurant" → Food & Dining, "SaaS" → SaaS & Tech). Categories are not available in the Framer RSC payload. The inferred category is stored as a `select` field in Notion.
- **First run:** seeds the DB silently — no Discord/X notifications
- **Fields tracked:** title, slug, URL, author, author URL, price, category, discovered datetime, published datetime
- **Pagination:** fetches up to 2 pages (40 templates) per run; pages are cumulative (`?page=N` returns items 1–N×20), stops early when a page yields fewer than 20 new items

**Deferred improvements:**
- RSC format is an internal Next.js mechanism — Framer could change the response structure without notice. When parsing yields < 5 templates a Discord alert is sent to `DISCORD_ALERTS_WEBHOOK_URL`, a `body_preview` (first 500 chars of the last page) and a `parse_errors` count (JSON parse failures from `_extract_json_object`) are logged to `logs/errors.jsonl` to aid diagnosis; a non-zero `parse_errors` with zero templates suggests a JSON structure change rather than an absent key; inspect the raw RSC payload and update `_extract_json_object` / the search key if needed
- Additional RSC search key variants — the script now automatically tries `"templateItem":` and `"marketplaceItem":` as fallbacks if the primary `"item":` key yields < 5 results, and uses whichever key produces the most templates. If Framer adopts a completely different key, `_find_candidate_rsc_keys` scans **all fetched RSC pages** (not just the last page) for JSON objects containing both `"id":` and `"slug":` and logs the deduplicated key names immediately preceding them as `candidate_keys` in `logs/errors.jsonl` (only when 0 templates and 0 parse errors, indicating a new unknown key rather than a JSON structure change). Add the reported key as a new entry in `_RSC_FALLBACK_KEYS` to restore parsing. If `candidate_keys` is also empty (no JSON objects with id+slug found), `_sample_rsc_line_prefixes` samples the first 10 distinct RSC flight-format **normalised line types** from all fetched pages and logs them as `rsc_line_types` — useful when Framer switches to a pure flight-format encoding with no inline JSON objects (as observed in the April 2026 outage). Chunk references (`I[chunk_id,...]`) are all normalised to the single type label `I[` by `_rsc_payload_type` so that the many distinct chunk IDs in a real RSC page do not exhaust the 10-entry quota with what is really one line type.
- Richer HTTP error reporting — when `save_to_notion` raises an `HTTPError` (e.g. 400 Bad Request), the Notion API response body (first 500 chars) is now captured and logged as `notion_response` in `logs/errors.jsonl`, matching the same pattern used by `reddit_leads.py`. Non-HTTP exceptions still log `slug` and `error` string only. The same diagnostic capture is now also applied to `post_to_x`: on `HTTPError` from the Twitter API the response body (first 500 chars) is logged as `twitter_response` along with the numeric `status` code, so an operator can distinguish between expired tokens (401), duplicate-content rejections (403), rate-limiting (429), and other failure classes that would otherwise all log only `"HTTP Error <code>: <reason>"`. Non-HTTP exceptions on the Twitter path keep the lighter `{error, tweet_length}` context since there is no HTTP body to capture.
- Category inference accuracy — keyword matching may miscategorise edge cases; an LLM-based approach could be added if accuracy becomes important. The inferred category is now persisted to Notion (as a `select` field) so miscategorisations are visible and correctable in the DB.
- Additional `CATEGORY_KEYWORDS` entries (e.g. `event`, `wedding`, `fintech`) — could reduce "Other" categorisations; skipped as the keyword list is already broad and new entries would need data from real Framer Marketplace templates to validate accuracy.
- Fetch failure alerting — if `fetch_framer_templates()` raises an exception (e.g. network error, HTTP error after retries), `main()` now catches it, logs it to `logs/errors.jsonl` at `error` severity, sends a Discord error alert to `DISCORD_ALERTS_WEBHOOK_URL`, and exits with code 1. Previously the exception propagated silently to GitHub Actions without any Discord notification.
- `get_seen_slugs()` failure alerting — implemented: `main()` now also wraps `get_seen_slugs()` so a Notion misconfiguration (deleted DB, revoked integration token, expired `NOTION_TOKEN`, wrong `NOTION_DATABASE_ID` secret) surfaces as a Discord alert and an error-log entry instead of crashing the script silently. On `urllib.error.HTTPError` the Notion response body (first 500 chars) and numeric `status` are logged as `notion_response`/`status` to distinguish 401 (token revoked/expired) from 404 (DB deleted or not shared) from transient 5xx; the alert message names both `NOTION_DATABASE_ID` and `NOTION_TOKEN` so the operator knows which secret to check. Non-HTTP exceptions (e.g. `URLError`, `TimeoutError`) take a lighter path with `{error}` context. In both cases `main()` exits non-zero so GitHub Actions surfaces the failure. Mirrors the existing `fetch_framer_templates` failure-alerting pattern and the `dedup_object_not_found` alerting in `reddit_leads.py` for the equivalent Notion-misconfiguration scenario.
- Read-timeout retries — implemented: `_should_retry` now also returns True for bare `TimeoutError` / `socket.timeout` exceptions raised during `response.read()`. These do not subclass `urllib.error.URLError`, so the previous `URLError`-only branch let them bypass retry entirely. The same fix is mirrored in `reddit_leads.py`.
- Unicode-aware Notion truncation — implemented: a shared `_truncate_for_notion` helper now slices the `Name`, `Slug`, `Author`, `Price`, and `Meta Title` fields in `save_to_notion` so the resulting string fits within Notion's 2000 UTF-16 code unit limit. Notion's `rich_text`/`title` validator counts UTF-16 code units, not Python code points, so a Python `[:2000]` slice on text with supplementary-plane characters (most emoji, 1 code point but 2 UTF-16 code units) can exceed the limit and trigger a 400 `validation_error`. The helper drops trailing code points until the UTF-16 encoding fits. Same pattern is mirrored in `reddit_leads.py`.
- Discord webhook HTTP error reporting — implemented: when `notify_discord_batch` or `_warn_discord` raises a `urllib.error.HTTPError` from the Discord API (e.g. revoked webhook, deleted channel, rate-limit, malformed payload), the response body (first 500 chars) is now captured and logged as `discord_response` alongside the numeric `status` code in `logs/errors.jsonl`. Without this, the log only contained `"HTTP Error <code>: <reason>"` which gives no signal about whether the cause is a revoked URL (401), a deleted/unknown webhook (404 — `code: 10015`), an invalid token (401 — `code: 50027`), rate-limiting (429 with retry-after), or a malformed-payload rejection (400) — only Discord's response body distinguishes them. Non-HTTP exceptions (e.g. `TimeoutError`, generic `Exception`) keep the existing lighter `{label, error}` (or `{error}`) context since there is no HTTP body to capture. Mirrors the diagnostic pattern already used by `save_to_notion`, `url_exists_in_notion`, and `post_to_x`. Same change applied to `reddit_leads.py`.
- `Retry-After` header honouring on HTTP 429 — implemented: `_retry` now inspects the `Retry-After` response header when an `HTTPError` with code 429 is caught, and sleeps at least that many seconds before the next attempt (clamped to `_RETRY_AFTER_MAX_SECONDS = 60` so a server advertising a very long backoff cannot consume most of the 15-minute cron window on one call). Both the RFC 7231 integer-seconds form (the common case for Discord/Notion/Twitter) and the HTTP-date form are parsed by `_parse_retry_after`; missing, malformed, or negative values silently fall back to the existing exponential schedule (2s → 4s → 8s). The header value is treated as a *minimum* — if our default backoff is already longer we keep the longer delay. Without this, a 429 with `Retry-After: 10` would be retried after 2s, almost certainly hitting the same rate-limit window and burning a retry attempt; with it, the retry is delayed enough to fall outside the limit. Only triggered on 429s — existing 5xx behaviour (pure exponential backoff) is preserved. Same change mirrored in `reddit_leads.py`.
- Tolerate page 2 fetch failure when page 1 succeeded — implemented: `fetch_from_rsc` now wraps each page fetch in a try/except. If page 1 fails the exception still propagates (no usable data) so `main()` fires its existing `fetch_framer_templates` Discord alert; but if page 1 succeeded and page 2 fails (network error, retries exhausted, HTTP 5xx after backoff, read timeout), the script logs a warning to `logs/errors.jsonl` with `page`, `error`, `page1_templates`, and (for `HTTPError`) `status` + `response_body` context, then breaks out of the loop and returns the page-1 templates. Previously a transient page-2 failure threw away all 20 valid page-1 templates and triggered the fatal `fetch_framer_templates failed` Discord alert, costing a discovery window for the newest 20 templates — exactly the ones a 15-min cron is most likely to find first. Mirrors the general principle used in `reddit_leads.py` (tolerate a subset of feed failures, escalate only when the majority fail). The page-2 warning still surfaces in `logs/errors.jsonl` so a recurring page-2 outage is visible to the self-improvement session.
- Discord webhook inter-message rate-limit pacing — implemented: `notify_discord_batch` now sleeps `_DISCORD_INTER_MESSAGE_DELAY = 0.5s` between successive webhook POSTs (no sleep before the first message). Discord enforces tight per-route rate limits on webhook endpoints — empirical limits hover around 5 messages per 2s and a sliding cap (often ~30 per 60s). Sending a 20+ template batch back-to-back reliably trips a 429, after which `_retry` has to honour the server-supplied `Retry-After` (typically a few seconds) on each subsequent message — burning far more cumulative wall-clock time than the proactive pacing itself. The delay continues even after a failed message so a transient failure on message N cannot let message N+1 be sent without pacing and trip a 429 anyway. The constant is exposed at module scope so tests can patch it to 0 to keep unit tests fast, and a dedicated test asserts the default is `> 0` so a regression cannot silently re-introduce the thrashing. Pacing is one-sided (no equivalent change in `reddit_leads.py`) because that script only sends a single Discord embed per `--notify` invocation — the per-message rate limit cannot trigger from one POST.
- Discord embed field limits in `_build_embed` — implemented: the per-template embed `title` is now sliced to `_DISCORD_EMBED_TITLE_LIMIT = 256` and `description` to `_DISCORD_EMBED_DESCRIPTION_LIMIT = 4096`, matching Discord's documented per-field caps. A Framer template title or `meta_title` exceeding those caps would otherwise be rejected with HTTP 400 (`Invalid Form Body`) and drop the entire notification for that template — and because `notify_discord_batch` iterates message-by-message, a single bad template only kills its own message, but it still loses a discovery the operator never sees. Mirrors `lead['title'][:256]` already used by `notify_discord_lead` in `reddit_leads.py`. Additionally, `published_at` is now ISO 8601-validated via `datetime.fromisoformat` before being set as the embed `timestamp` — a malformed value (e.g. a residual `$D` prefix from a future RSC format change, or any non-ISO sentinel) silently omits `timestamp` instead of 400-ing the webhook, mirroring the same defensive guard already used in `notify_discord_lead`. The summary embed is unaffected: its title is a generated count string and the description is already capped via the existing `< 3900` truncation loop.
- Markdown-link escaping in Discord embeds — implemented: `_escape_md_link_text` and `_escape_md_link_url` helpers now backslash-escape `[`/`]`/`\` in the link-text segment and `)`/`\` in the URL segment of every Discord markdown link the script builds. Without escaping, a template whose title contains `]` (e.g. `"Brand [Pro]"`) or whose URL contains `)` (most realistically a `demo_url` with a parenthesised path segment) would break the markdown link in Discord — the renderer is non-greedy with `]`/`)` so the first unescaped occurrence terminates the link, dumping the remainder of the title/URL into the description as visible plain text alongside a stray `](url)` or `(url`. The escape is applied in three places: the per-template `_build_embed` author markdown link (when an `author_slug` is present), its `Live Demo` link (`demo_url` is uncontrolled and could contain `)`), and the per-template lines in `_build_summary_embed` (`- [{title}]({url})`). Author/price in the summary line are left unescaped because they appear outside the markdown link where `]` is harmless; titles in the per-template embed `title` field (not inside a link) are likewise not escaped because Discord renders the field as plain text. Only `[`/`]`/`\`/`)` are escaped — other markdown metacharacters (`*`/`_`/`` ` ``/`~`) would be rare in template/author names and escaping them too would visibly clutter the description with backslashes for the common clean case.

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
- `post_date` in reviewer output — implemented: `get_pending_leads()` now returns `post_date` (the ISO 8601 post creation time from the Notion `Post Date` field, or empty string if unset) so the reviewer session can see how old a lead is and deprioritise stale posts
- Unicode-aware content truncation — implemented: a shared `_truncate_for_notion` helper now slices `Name` (title), `Content`, and `Review Notes` so the resulting string fits within Notion's 2000 UTF-16 code unit limit. Notion's `length should be ≤ 2000` validator counts UTF-16 code units, not Python code points, so a Python `[:2000]` slice on text containing supplementary-plane characters (e.g. most emoji, which are 1 code point but 2 UTF-16 code units) could exceed Notion's limit and trigger a 400 (observed on 2026-04-29 for r/smallbusiness with `length should be ≤ 2000, instead was 2001`). The helper drops trailing code points until the UTF-16 encoding fits, so the entire lead is preserved instead of being permanently sentinel-blacklisted
- Read-timeout retries — implemented: `_should_retry` now also returns True for bare `TimeoutError` / `socket.timeout` exceptions. These are raised by `response.read()` after a connection has already been established and do **not** subclass `urllib.error.URLError`, so the previous `URLError`-only branch let them bypass retry entirely; this matches the recurring `"The read operation timed out"` warnings observed in `logs/errors.jsonl` for Notion dedup checks. The same fix is applied to `framer_templates.py` since the two scripts share the same retry helper signature
- Notion 404 retries — considered: the dedup-check 404s observed in `logs/errors.jsonl` for `url_exists_in_notion` are unusual (the same DB id works for queries seconds later) and likely transient Notion outages; not added to `_should_retry` because 404 conventionally signals "this resource doesn't exist" and silently retrying it would mask genuine misconfiguration (deleted DB, integration without access). The existing dedup-check error isolation already skips the post safely on 404 and the next run picks it up
- Richer dedup-check HTTP error logging — implemented: when `url_exists_in_notion` raises a `urllib.error.HTTPError` (e.g. the recurring 404s observed in `logs/errors.jsonl`), `main()` now reads the Notion API response body (truncated to 500 chars) and logs it as `notion_response`, alongside the numeric `status` code. Mirrors the same diagnostic pattern already used by `save_lead_to_notion`. Without this, a 404 log only contained `"HTTP Error 404: Not Found"` which gives no signal about whether the cause is a deleted DB, a revoked integration token, or a transient Notion outage — only the response body distinguishes them. Non-HTTP exceptions (e.g. `TimeoutError`) keep the original lighter `{url, error}` context since there is no HTTP body to capture
- `--notify` CLI: only mark Notified after Discord succeeds — implemented: `notify_discord_lead` now returns `True`/`False` (it still swallows the exception so existing callers do not need a try/except). The `--notify` CLI handler checks the return value and only calls `mark_notified` when the webhook POST actually succeeded, exiting non-zero on failure. Previously a failed Discord webhook (e.g. transient 5xx, expired webhook) silently flipped the Notion `Notified` checkbox so the lead never reached the channel and was never retried on a subsequent reviewer run. The `if __name__ == '__main__'` block was extracted into a `cli(args)` function so the dispatch can be unit-tested directly without `runpy.run_path` re-importing the module and breaking `unittest.mock` patches
- Dedup-failure Discord alerting — implemented: `main()` now tracks two dedup-failure counters (`dedup_object_not_found_errors` for Notion `object_not_found` 404s, and a general `dedup_errors` for everything else) plus a capped sample list of `r/<sub> HTTP <code>` / `r/<sub> <ExceptionName>` entries. After the loop, a single `object_not_found` triggers an ERROR-level Discord alert mentioning the `NOTION_REDDIT_LEADS_DB_ID` secret (because that case almost always means the configured DB is deleted, renamed, or no longer shared with the integration — every dedup check for the rest of the run will fail the same way and no leads will be saved). Other dedup failures only fire a WARNING when at least 5 occur in a single run, since one transient timeout is normal noise. Without this, the recurring 404s observed in `logs/errors.jsonl` on 2026-05-04 (DB ID `3f50084a-30c6-47a5-be13-90f9673e8569` returning `object_not_found` for every dedup query) silently halted lead saving with no operator-visible signal until lead flow dried up
- Unnotified-approved lead recovery — implemented: `get_unnotified_approved_leads(db_id)` queries Notion for pages with `Status='approved'` AND `Notified=False`, exposed via the `--list-unnotified-approved` CLI command. The `--notify` CLI already exits non-zero on Discord failure and leaves the `Notified` checkbox unset, but `--list-pending` filters on `Status='pending'` only — so an approved lead whose webhook POST transiently failed (Discord 5xx, expired webhook, network blip) was previously orphaned forever (Status=approved, Notified=False, never re-listed, never re-tried). The reviewer session now runs this command after handling pending leads and re-issues `--notify PAGE_ID` for each entry; the original Review Notes set during approval are preserved on the page so the retry produces the same Discord embed
- Discord embed lead-age timestamp — implemented: `get_lead_by_id` now also returns the stored `Post Date` (the original Reddit publish time saved by Phase 1), and `notify_discord_lead` sets it as the Discord embed `timestamp` so the leads channel renders a human-readable "X hours/days ago" indicator under each embed. Without this, an operator scanning the channel had no signal whether a lead was 2 hours old (highly actionable) or 5 days old (likely already taken) without clicking through to Reddit. A malformed/empty `post_date` is silently dropped via an `isoformat` parse check, so a bad value cannot 400 the webhook for the whole batch — mirroring the same defensive ISO 8601 guard used in `save_lead_to_notion`. Leads stored before Post Date was tracked still notify cleanly (no `timestamp` field, no error)
- Discord webhook HTTP error reporting — implemented: when `notify_discord_lead` or `_warn_discord` raises a `urllib.error.HTTPError` from the Discord API, the response body (first 500 chars) is now captured and logged as `discord_response` alongside the numeric `status` code. Without this, a failed webhook (revoked URL, deleted channel, rate-limit, malformed payload) only logged `"HTTP Error <code>: <reason>"` which gives no signal about whether the cause is a revoked webhook (401 — `code: 50027`), a deleted webhook (404 — `code: 10015`), rate-limiting (429 with retry-after), or a malformed-payload rejection (400). For `notify_discord_lead`, the existing `url` and `subreddit` context fields are preserved so an operator can correlate a webhook failure with the specific lead it was trying to deliver. Non-HTTP exceptions keep the original lighter context (`{error, url, subreddit}` for `notify_discord_lead`, `{error}` for `_warn_discord`) since there is no HTTP body to capture. Mirrors the same diagnostic pattern already used by `save_lead_to_notion`, `url_exists_in_notion`, and `post_to_x`/`save_to_notion` in `framer_templates.py`. Same change applied to `framer_templates.py` (`notify_discord_batch` and `_warn_discord`)
- `Retry-After` header honouring on HTTP 429 — implemented: `_retry` now inspects the `Retry-After` response header when an `HTTPError` with code 429 is caught and sleeps at least that many seconds before the next attempt (clamped to `_RETRY_AFTER_MAX_SECONDS = 60`). Particularly useful for the Reddit RSS endpoints — fetching 43 feeds back-to-back with `_INTER_FEED_DELAY = 1.5s` already triggers occasional rate-limiting, and Reddit's 429s typically carry a `Retry-After` in the tens of seconds. Without this, the retry would fire after 2s, almost certainly hit the same rate-limit window, and burn through retry attempts without recovering; with it, the retry is delayed enough to fall outside the limit. Both the RFC 7231 integer-seconds form and the HTTP-date form are parsed by `_parse_retry_after`; missing, malformed, or negative values fall back to the existing exponential schedule. The header value is treated as a *minimum* — if our default backoff is already longer we keep the longer delay. Only 429s trigger the special path — 5xx retries still use pure exponential backoff to keep behaviour stable. Same change mirrored in `framer_templates.py`.
- `--update-status` typo guard — implemented: `update_lead_status` now validates the `status` argument against a fixed `_VALID_STATUSES = frozenset({'pending', 'approved', 'rejected', 'failed'})` set and raises `ValueError` *before* any Notion request is issued. The `--update-status` CLI handler catches the `ValueError` and exits with status code 2 + a stderr message naming the bad value and the accepted set. Without this, a reviewer typo like `approve` (missing `d`) or `rejcted` would not raise — Notion silently creates a brand-new select option for any unrecognised name, and the affected page would no longer match the `Status='pending'` filter used by `--list-pending`, nor the `Status='approved'` filter used by `--list-unnotified-approved` / `--notify`, nor the `Status='rejected'` reviewer-history filter — orphaning the lead from every downstream query. Catching the typo at the source is much cheaper than recovering from it later (the page would need to be edited manually in the Notion UI to fix the bogus select option). The matching is case-sensitive because Notion select option names are case-sensitive: `Approved` would not match the existing `approved` option either. `_VALID_STATUSES` is a `frozenset` so an accidental `.add` cannot widen the accepted values without code review.

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
