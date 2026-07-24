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

- **2026-07-24** — Zero errors in the last 7 days (error log has only historical
  entries through 2026-06-30; all addressed by PRs #115–#117). framer_templates.py
  parsed 52 templates in observe-only mode: 3 fell into "Other" (MaestroClass
  "High Ticket Experience Sales Page", Assemble "Premium Event, Conference &
  Meetup", Timeline "A clean timeline feed template."). Adding "event" and
  "conference" keywords could rescue one of those, but 3/52 other-rate is not
  substantive enough to justify a PR alone. Full test suite passes (426 tests).
  Open PR #118 (scheduler observation from 2026-07-12) is still unmerged — no
  new work to add to it. No change warranted.
