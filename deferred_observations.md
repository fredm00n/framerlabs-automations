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

- **2026-07-10** — Zero errors in the 7-day window (last log entry: 2026-06-30).
  All 426 tests pass. No parser breakage, no recurring data-losing failure.
  The July 2026 RSC sectioned format (`"items":` arrays) is covered by PR #117
  (merged 2026-07-08); the 529 retry fix from PR #116 is holding. No open PRs.
  No change warranted.
