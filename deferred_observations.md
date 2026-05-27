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
  Reddit-specific SSL-bypass wrapper (ssl.CERT_NONE). `get_lead_by_id`,
  `update_lead_status`, and `mark_notified` all hit Notion with certificate
  verification disabled. Functionally harmless (Notion uses well-known certs)
  but unnecessarily broadens the SSL bypass beyond its original Reddit RSS
  scope. No errors observed, no data loss.
