# Deferred observations

Observations logged by the self-improvement loop that did not meet the
Step 2b triage criteria (3+ recurrences or data loss). If the same issue
appears across multiple sessions it will cross the recurrence threshold
and be implemented.

---

- **2026-05-25** — Reddit RSS 500 outage (2026-05-18): 25/43 subreddit feeds
  returned HTTP 500 in a single run. All errors came from a single Reddit-side
  outage; the existing partial-failure alerting and retry logic handled it
  correctly. No data loss (leads re-appear in subsequent RSS fetches). Single
  occurrence — does not meet the recurrence threshold.

- **2026-05-27** — reddit_leads.py routes Notion PATCH/GET calls through the
  Reddit-specific SSL-bypass wrapper (ssl.CERT_NONE). _Resolved 2026-05-28:_
  the local `http_get`/`http_patch` wrappers now pick the SSL context per host
  (`_ssl_context_for`), applying the cert bypass only to `reddit.com` URLs so
  Notion calls keep default TLS verification. See deferred_improvements.md.

- **2026-06-03** — Self-improvement review found no production failures: the
  committed `logs/errors.jsonl` is empty. The only error entries visible in this
  VM's working tree were locally-generated sandbox artifacts (43 × HTTP 403
  "Blocked by egress policy" — the observe-only VM's own egress restriction, not
  a Reddit/production failure) and were discarded. Both parsers and the lead
  filter look healthy; full test suite passes (369 tests). No change warranted.

- **2026-06-09** — Self-improvement review found no production failures: the
  committed `logs/errors.jsonl` is still empty. The working tree again showed
  only sandbox artifacts (45 × HTTP 403 "Blocked by egress policy" from a single
  observe-only startup run spanning 07:07–07:08, plus a matching
  `state/alert_state-reddit_leads.json` bump) — discarded. Both parsers
  syntax-check OK and the full test suite passes. No change warranted.

- **2026-06-11** — Self-improvement review found no production failures: the
  committed `logs/errors.jsonl` is empty. No errors in the 7-day window, so no
  log trimming was needed. Both parsers syntax-check OK and the full test suite
  passes (369 tests). The lead filter and category inference look healthy. No
  broken parser, no recurring data-losing failure, and no substantive
  filter/subreddit/category improvement evidenced this session. No change
  warranted.

- **2026-07-07** — All 16 errors in the 7-day window are HTTP 529
  (`service_overload`) from a single Notion outage on 2026-06-30 (11:48–18:10
  UTC). The outage caused 4 missed lead saves in `reddit_leads.py` (sentinel
  writes also failed, so the same r/nocode post was retried at 13:31 and failed
  again) and 5 missed template saves + short-circuit in `framer_templates.py`.
  PR #116 (merged 2026-07-01) added 529 to the shared `_should_retry` handler
  before this session ran; no recurrence since. No current failures, no parser
  breakage, no new capability improvement evidenced. No change warranted.

- **2026-07-11** — Zero errors in the last 7 days. The error log contains only
  historical entries (through 2026-06-30), all pre-existing issues addressed by
  PRs #115 (Reddit cookie auth), #116 (Notion 529 retry), and #117 (July 2026
  RSC sectioned format). framer_templates.py parsed 52 templates successfully
  in observe-only mode; full test suite passes (426 tests). No broken parser, no
  recurring data-losing failure, no substantive new capability to add. No change
  warranted.

- **2026-07-20** — Zero errors in the last 7 days. The error log contains only
  historical entries (through 2026-06-30); all prior issues were addressed by
  PRs #115–#117. Both parsers are healthy: framer_templates.py handles the July
  2026 sectioned RSC layout ("items":[] arrays) and the reddit_leads.py RSS
  pipeline runs without rate-limit errors since the REDDIT_COOKIE fix. Full test
  suite passes. No broken parser, no recurring data-losing failure, no
  substantive new capability evidenced. No change warranted.

- **2026-07-25** — Zero errors in the last 7 days. The error log contains 2,965
  entries all from 2026-06-11 to 2026-06-30 (all older than 7 days); every issue
  in that window was already addressed by PRs #115–#117. Both parsers are healthy:
  framer_templates.py uses the July 2026 sectioned "items":[] format with multiple
  fallbacks; reddit_leads.py RSS pipeline runs cleanly with REDDIT_COOKIE auth.
  Full test suite passes (426 tests). No broken parser, no recurring data-losing
  failure, no substantive new capability evidenced. No change warranted.

- **2026-08-15** — Zero errors in the last 7 days (error log is empty). Both parsers
  are healthy; framer_templates.py and reddit_leads.py import cleanly. Full test
  suite passes (431 tests). No broken parser, no recurring data-losing failure, no
  substantive new capability evidenced. No change warranted.

- **2026-08-24** — All 121 errors in the last 7 days are Reddit HTTP 403 (116) /
  429 (2) fetch failures in reddit_leads.py, and all come from three transient
  Reddit anti-scraper blocks on a single day (2026-08-20 at 02:00, 11:45, 13:15
  UTC). Each burst tripped the partial-failure alert to Discord ("All subreddit
  feeds failed", "Majority of subreddit feeds failed (30/43)", "(42/43)"). The
  shared `_should_retry` handler already retries 403 (added in PR #125), so these
  entries represent runs where all 4 retry attempts were exhausted during the
  block window. No errors in the 4 days since, so the anti-scraper block cleared
  on its own. framer_templates.py parsed 120 templates and detected 4 new in an
  observe-only run; full test suite passes (432 tests). No broken parser, no
  recurring data-losing failure, no substantive new capability evidenced. No
  change warranted.

- **2026-08-31** — Zero errors in the last 7 days (the 121 entries from 2026-08-20
  are all older than 7 days). GitHub Actions shows continuous successful runs for
  both Framer Monitor and Reddit Leads Monitor workflows through today. The
  2026-08-20 Reddit anti-scraper block that caused the prior 403/429 flood fully
  cleared; no recurrence. All 432 tests pass. No broken parser, no recurring
  data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-06** — Zero errors in the last 7 days (all 121 log entries remain from
  2026-08-20, now 17 days old). No new errors since the prior Aug 31 session. No
  open PRs. All 432 tests pass. No broken parser, no recurring data-losing failure,
  no substantive new capability evidenced. No change warranted.

- **2026-09-20** — All 121 errors in the log are Reddit HTTP 403/429 fetch failures from 2026-08-20 (31 days ago), already reviewed by the 2026-09-06 session. Those were transient anti-scraper blocks that cleared within the same day; no errors since. The log entries are outside the 7-day retention window and are trimmed now. framer_templates.py and reddit_leads.py both import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-21** — Zero errors in the last 7 days — the error log is empty (121 old entries from 2026-08-20 trimmed). Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-22** — Zero errors in the last 7 days — error log is empty. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-23** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.
- **2026-09-24** — Zero errors in the last 7 days — error log is empty. No open PRs. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-25** — Zero errors in the last 7 days — error log is empty. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-26** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-27** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-28** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-29** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-09-30** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-01** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-02** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-03** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import and run cleanly (framer_templates fetched 64 templates, 0 new; reddit_leads parsed without error). No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-04** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-05** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-06** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-06** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-07** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly (framer_templates fetched 64 templates, 0 new; reddit_leads parsed without error). No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-08** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-09** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts running cleanly: framer_templates.py fetched 64 templates (1 new found during observe-only run); reddit_leads.py imports without error. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-10** — Zero errors in the last 7 days — error log is empty. No open PRs. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-11** — One transient HTTP 429 warning for r/forhire in logs/errors.jsonl — retry logic handled it, no data loss. No broken parser, no recurring failures, no substantive new capability evidenced. No change warranted.

- **2026-10-12** — One transient HTTP 429 warning for r/forhire in logs/errors.jsonl (self-healed, no data loss). Both scripts import cleanly; framer_templates.py fetched 64 templates with 2 new found (observe-only). No broken parser, no recurring data-losing failures, no substantive new capability evidenced. No change warranted.

- **2026-10-13** — Reviewed logs/errors.jsonl (1 entry: transient HTTP 429 on r/forhire from 2026-10-10, self-healed by existing retry logic — not a bug). Skimmed framer_templates.py and reddit_leads.py — no parser breakage, no recurring data-losing failures. No action needed.

- **2026-10-14** — One transient HTTP 429 warning for r/forhire in logs/errors.jsonl (from 2026-10-10, already seen in previous session) — retry logic handled it, no data loss. Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-15** — One transient HTTP 429 warning for r/forhire in logs/errors.jsonl (from 2026-10-10, same entry noted in prior sessions — self-healed by existing retry logic, no data loss). Skimmed framer_templates.py and reddit_leads.py — no parser breakage, no recurring data-losing failures. No open PRs found. No change warranted.

- **2026-10-16** — One transient HTTP 429 warning for r/forhire in logs/errors.jsonl (from 2026-10-10, same entry noted across multiple prior sessions — self-healed by existing retry logic, no data loss). Both scripts import cleanly. No broken parser, no recurring data-losing failure, no substantive new capability evidenced. No change warranted.

- **2026-10-17** — Verified the newly merged resources-API source (PR #132) in observe-only: framer_templates.py fetched 120 templates, 8 new, no api_fallback or stale_feed warning — the popular-grid breakage is fixed. Error log holds only the 2026-10-10 transient r/forhire 429 (self-healed). 488 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-18** — Error log held only the 2026-10-10 transient r/forhire 429 (self-healed, now past the 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 7 new, no api_fallback or stale_feed — source healthy after PR #132. reddit_leads.py sandbox run 429s without REDDIT_COOKIE (expected locally, local log changes discarded). 488 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-19** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 8 new, no api_fallback or stale_feed — source healthy. 488 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-20** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 8 new, no api_fallback or stale_feed — source healthy. 488 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-21** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 11 new, no api_fallback or stale_feed — source healthy. Framer Monitor cron runs are all successful but GitHub fires the schedule only every ~2-5h, not every 15 min; the 120-item feed still covers that gap and overflow beyond the 10/run notify cap is backfilled to Notion, so no data loss. Side note: running the test suite locally rewrites state/alert_state-reddit_leads.json (a test hits the real alert-state path) — local-only, never committed, not data-losing. 489 tests pass. No open PRs. No change warranted.

- **2026-10-22** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 3 new, no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-23** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 5 new, no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-24** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 10 new, no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-25** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 0 new, no api_fallback or stale_feed; newest publishedAt 2026-10-25T06:34Z, so the feed is fresh and 0 new means the production cron had already saved the batch. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-26** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 4 new, no api_fallback or stale_feed — source healthy. 489 tests pass. No open PRs. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-27** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 19 new, no api_fallback or stale_feed — source healthy; 19 new reflects the ~hours since the last production cron run (last Framer Monitor run 02:11Z, all recent runs successful), well within the 120-item feed window, overflow backfilled to Notion, so no data loss. 489 tests pass. No open PRs. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-28** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 17 new, no api_fallback or stale_feed — source healthy; overflow beyond the 10/run notify cap is backfilled, so no data loss. 489 tests pass. No open PRs. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-29** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 15 new, no api_fallback or stale_feed — source healthy; overflow beyond the 10/run notify cap is backfilled, so no data loss. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-30** — Error log empty (0 entries in 7-day window). framer_templates.py in observe-only: resources API returned 120 templates, 1 new, no api_fallback or stale_feed — source healthy. reddit_leads.py observe-only run fetched feeds (local 429s expected without REDDIT_COOKIE). Recent scheduled Framer Monitor and Reddit Leads Monitor runs all succeeded. 489 tests pass. No open PRs. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-10-31** — Error log: 2 entries in 7-day window, both reddit_leads HTTP 429 warnings (r/css, r/javascript) from a single run on 2026-10-30 07:07Z — isolated and transient, 2 of 43 feeds, not recurring. framer_templates.py in observe-only: resources API returned 120 templates, 11 new (10 notify + 1 backfill), no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-01** — Error log: 2 entries in 7-day window, both reddit_leads HTTP 429 warnings (r/css, r/javascript) from a single run on 2026-10-30 — isolated and transient, not recurring. framer_templates.py in observe-only: resources API returned 120 templates, 5 new, no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-02** — Error log: 2 entries in 7-day window, both reddit_leads HTTP 429 warnings (r/css, r/javascript) from a single run on 2026-10-30 — isolated and transient, not recurring. framer_templates.py in observe-only: resources API returned 120 templates, 12 new (10 notify + 2 backfill), no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-03** — Error log: 2 entries in 7-day window, both reddit_leads HTTP 429 warnings (r/css, r/javascript) from a single run on 2026-10-30 — isolated and transient, not recurring. framer_templates.py in observe-only: resources API returned 120 templates, 16 new (10 notify + 6 backfill), no api_fallback or stale_feed — source healthy. 489 tests pass. Minor note: running the test suite rewrites state/alert_state-reddit_leads.json in the working tree (dedup_notion_likely_down timestamp) — a test-isolation leak, but it never reaches production (CI does not commit it; log_observation commits only its own paths), so below the bar. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-04** — Error log: 2 entries in 7-day window, both reddit_leads HTTP 429 warnings (r/css, r/javascript) from a single run on 2026-10-30 — isolated and transient, not recurring. framer_templates.py in observe-only: resources API returned 120 templates, 20-21 new (10 notify + backfill), no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-05** — Error log: 2 entries in 7-day window, both reddit_leads HTTP 429 warnings (r/css, r/javascript) from one run on 2026-10-30 — transient, not recurring. framer_templates.py observe-only: API returned 120 templates, 41 new, no api_fallback/stale_feed. The rising daily 'new' count (5→12→16→20→41) was checked against Actions history: all prod runs succeed, but GitHub throttles the */15 cron to roughly every 2-6h (last Framer Monitor run 01:41 UTC, 5.5h before this check), and exactly 41 templates were published after 01:41 — so it is the cron gap, not lost saves; the 120-item window (~18h) still covers it. Owner note (below the bar, design choice): with ~40 templates per throttled run, the 10/run notify cap means most templates are saved but never announced on Discord/X. 489 tests pass. No change warranted.

- **2026-11-06** — Error log: 2 entries (reddit_leads HTTP 429 r/css, r/javascript from one run on 2026-10-30), aging out of the 7-day window — transient, not recurring. framer_templates.py observe-only: resources API returned 120 templates, 13-14 new (10 notify + 3 backfill), no api_fallback or stale_feed — source healthy. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-07** — Error log: 2 entries (reddit_leads HTTP 429 r/css, r/javascript from one run on 2026-10-30), aging out of the 7-day window — transient, not recurring. framer_templates.py observe-only: resources API returned 120 templates, 12 new (10 notify + 2 backfill), no api_fallback or stale_feed — source healthy. Recent scheduled Framer Monitor and Reddit Leads Monitor runs all succeeded. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-08** — Error log: 0 entries in the 7-day window (the 2026-10-30 reddit_leads 429s have aged out). framer_templates.py observe-only: resources API returned 120 templates, 22 new (10 notify + 12 backfill), no api_fallback or stale_feed — source healthy; the new count matches the ~6h gap since the last prod Framer Monitor run (01:38 UTC). Recent scheduled Framer Monitor and Reddit Leads Monitor runs all succeeded. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-09** — Error log: 0 entries in the 7-day window. framer_templates.py observe-only: resources API returned 120 templates, 48 new (10 notify + 38 backfill), no api_fallback or stale_feed — source healthy; the new count matches the gap since the last prod Framer Monitor run (02:11 UTC), well inside the 120-item window. Recent scheduled Framer Monitor and Reddit Leads Monitor runs all succeeded. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-10** — Error log: 0 entries in the 7-day window. framer_templates.py observe-only: resources API returned 120 templates, 29 new (10 notify + 19 backfill), no api_fallback or stale_feed — source healthy; new count consistent with the gap since the last prod Framer Monitor run (02:41 UTC). Recent scheduled Framer Monitor and Reddit Leads Monitor runs all succeeded. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-11** — Error log: 0 entries in the 7-day window. framer_templates.py observe-only: resources API returned 120 templates, 40 new (10 notify + 30 backfill), no api_fallback or stale_feed — source healthy; new count consistent with the throttled cron gap, well inside the 120-item window. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-12** — Error log: 0 entries in the 7-day window. framer_templates.py observe-only: resources API returned 120 templates, 28 new (10 notify + 18 backfill), no api_fallback or stale_feed — source healthy; new count consistent with the throttled cron gap (last prod Framer Monitor run 04:13 UTC). Recent scheduled Framer Monitor and Reddit Leads Monitor runs all succeeded. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-13** — Error log: 0 entries in the 7-day window. framer_templates.py observe-only: resources API returned 120 templates, 31 new (10 notify + 21 backfill), no api_fallback or stale_feed — source healthy; new count consistent with the throttled cron gap, well inside the 120-item window. 489 tests pass. No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.

- **2026-11-14** — Error log: 0 entries in the 7-day window. framer_templates.py observe-only: resources API returned 120 templates, 16 new (10 notify + 6 backfill), no api_fallback or stale_feed — source healthy. 489 tests pass (test run again bumped state/alert_state-reddit_leads.json locally, already noted 2026-11-03; discarded). No broken parser, no recurring data-losing failure, no substantive capability gap. No change warranted.
