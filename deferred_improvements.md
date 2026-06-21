# Deferred improvements — historical context

> This file contains the full rationale and implementation history for each improvement
> made by the self-improvement loop. It exists as a reference for agents working on
> these scripts — you do not need to read it unless you are actively investigating
> or extending one of the items below.

## framer_templates.py

### Still deferred (not yet implemented)

- **RSC format fragility** — Framer's RSC payload is an internal Next.js mechanism that could change without notice. When parsing yields < 5 templates, alerts and diagnostics fire. The script already tries fallback keys and scans for candidate keys, but a completely new encoding (e.g. pure flight-format with no inline JSON) would require manual intervention.
- **Category inference accuracy** — keyword matching may still miscategorise genuinely ambiguous titles (a title containing keywords from multiple categories resolves by `CATEGORY_KEYWORDS` order). An LLM-based approach could be added if accuracy matters. The inferred category is persisted to Notion so miscategorisations are visible and correctable. _Substring false positives resolved 2026-05-29 — see Implemented below._
- **Additional `CATEGORY_KEYWORDS` entries** (e.g. `event`, `wedding`, `fintech`) could reduce "Other" categorisations; skipped as new entries need validation against real Framer templates.

### Implemented (context for future reference)

- Whole-word category keyword matching (`infer_category`) — keywords are now precompiled into `\b`-anchored regexes (`_compile_category_patterns` / `_CATEGORY_PATTERNS`) instead of naive substring `in` checks. Short keywords like `'ai'` and `'app'` were matching inside unrelated words ("ret**ai**l", "em**ai**l", "h**app**y", "wr**app**er"), silently filing e.g. a "Retail Store" template under "SaaS & Tech" — the wrong category was persisted to the Notion `Category` select and shown under the wrong heading in the Discord/X recap. Multi-word and punctuation keywords (`'real estate'`, `'e-commerce'`, `'bar & grill'`, `'coming soon'`, `'non-profit'`) and the first-match-wins ordering are unchanged. Mirrors the word-boundary fix already used by `_has_word_start_phrase` in `reddit_leads.py`.
- RSC fallback key detection and `_find_candidate_rsc_keys` diagnostic
- `_sample_rsc_line_prefixes` for flight-format diagnosis
- HTTP error response body capture on Notion saves, Twitter posts, Discord webhooks
- `Retry-After` header honouring on 429s (integer-seconds + HTTP-date)
- Read-timeout retries for bare `TimeoutError`/`socket.timeout`
- Unicode-aware Notion truncation (`truncate_for_notion`)
- Discord embed field limits (title 256, description 4096)
- ISO 8601 validation on `published_at` before setting embed timestamp
- Markdown-link escaping in Discord embeds (`_escape_md_link_text`, `_escape_md_link_url`)
- Discord webhook inter-message rate-limit pacing (0.5s between POSTs)
- Cross-run alert suppression via `state/alert_state-framer_templates.json`
- Consecutive save-failure short-circuit (3 failures → bail out)
- Page 2 fetch failure tolerance (use page 1 results if page 2 fails)
- `get_seen_slugs()` failure alerting for Notion misconfiguration
- Fetch failure alerting in `main()`

---

## reddit_leads.py

### Still deferred (not yet implemented)

- **Smarter dedup** — currently one Notion API call per filtered post; could batch with OR filters once Notion supports them natively.
- **Score/rank leads** — a rough confidence score could help the reviewer prioritise; skipped as Claude's reasoning handles this naturally.
- **Expanded `_JOB_SEEKER_SIGNALS`** — additional phrases like `"open to work"` could reduce false positives; skipped as Phase 2 review catches these.
- **Notion 404 retries** — the dedup-check 404s are unusual but retrying 404 would mask genuine misconfiguration. Existing error isolation handles this safely.

### Implemented (context for future reference)

- Reddit cookie auth (`REDDIT_COOKIE` env var) — a logged-out browser `edgebucket`+`loid` cookie is injected as a `Cookie` header on reddit.com RSS fetches (`_reddit_cookie_header` / `_is_reddit_host`), moving requests out of Reddit's ~1/60s-per-IP anonymous bucket into the ~100/10min logged-out visitor bucket. The `loid` keys on the cookie, not the IP, so it fixes the recurring HTTP 429 fetch failures from GitHub Actions' datacenter IP — no proxy, VPS, or OAuth needed. Host-gated so the cookie never reaches the Notion API that shares the `http_get` wrapper; optional (script runs unchanged if unset); stored as a GH Actions secret (repo is public — never committed). `loid` is multi-year; re-harvest from a logged-out browser if 429s return.
- Inter-feed pacing — `_INTER_FEED_DELAY` (1.5s) sleep between subreddit fetches, plus a browser `_REDDIT_USER_AGENT` (Reddit 403s the default UA). Secondary 429 defense alongside the cookie above; previously listed as the deferred "Rate limiting" item.
- Persistent 400 tracking via `save_failed_sentinel_to_notion`
- Dedup-check error isolation (separate try/except from save)
- `post_date` in reviewer output
- Unicode-aware content truncation (`truncate_for_notion`)
- Read-timeout retries for bare `TimeoutError`/`socket.timeout`
- Richer dedup-check HTTP error logging (Notion response body capture)
- `--notify` CLI: only mark Notified after Discord succeeds
- `--update-status` typo guard (validate against `_VALID_STATUSES`)
- Dedup-failure Discord alerting with `object_not_found` vs generic thresholds
- Unnotified-approved lead recovery (`--list-unnotified-approved` CLI)
- Discord embed lead-age timestamp from `Post Date`
- Discord webhook HTTP error response body capture
- `Retry-After` header honouring on 429s
- `'rate my'` word-boundary fix (no longer matches inside `'migrate my'`)
- Fetch-failure alert samples (first 5 causes in Discord alert)
- `body_preview` capture on XML `ParseError`
- HTTPError `body_preview` truncation aligned to 500 chars
- Early-exit on Notion `object_not_found` dedup failure
- Generalised dedup-failure short-circuit (3 consecutive failures)
- Notion preflight check before feed loop
- Cross-run alert suppression via `state/alert_state-reddit_leads.json`
- Per-host SSL context selection (`_ssl_context_for`) — the cert-verification bypass (`ssl.CERT_NONE`) is now applied only to `reddit.com` RSS fetches, which the scheduler VM cannot validate. Notion API calls (`get_lead_by_id`, `update_lead_status`, `mark_notified`) keep default TLS verification, so the `NOTION_TOKEN` Bearer credential and lead data are no longer exposed over an unverified connection. Previously the local `http_get`/`http_patch` wrappers applied the bypass unconditionally, inconsistent with the shared `http_post` Notion calls which already verified.
