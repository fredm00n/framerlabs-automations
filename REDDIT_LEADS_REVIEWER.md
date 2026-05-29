# Reddit Leads Reviewer (Hourly Dedicated Session)

Review Reddit posts that passed the light keyword filter and decide which are genuine
Framer freelance leads worth pursuing.

**Session trigger prompt** (set once, never changes):
```
Read CLAUDE.md and REDDIT_LEADS_REVIEWER.md, then follow the instructions in REDDIT_LEADS_REVIEWER.md.
```

---

## Before you begin

Pull the latest code to ensure the working directory is not stale:

```bash
git pull origin main
```

If this fails due to local changes, do not proceed — report the error and exit.

---

## Step 1 — Verify credentials

Ensure the following environment variables are available (set by the scheduled trigger):
- `NOTION_TOKEN`
- `NOTION_REDDIT_LEADS_DB_ID`
- `DISCORD_WEBHOOK_URL_LEADS`

---

## Step 2 — Get pending leads

```bash
python3 scripts/reddit_leads.py --list-pending
```

This prints a JSON array of all leads with `Status = pending`. Each item has:
- `page_id` — Notion page ID (needed for subsequent commands)
- `title` — Reddit post title
- `url` — Link to the Reddit post
- `subreddit` — Which subreddit it came from
- `content` — Up to 2000 chars of post content (truncated when stored in Notion)
- `post_date` — ISO 8601 datetime when the Reddit post was published (empty string if unavailable)

If the output is an empty array `[]`, there is nothing to review — exit cleanly.

---

## Step 3 — Evaluate each lead

For each lead, read the title and content carefully and decide: **approve** or **reject**.

This feeds a public Discord with hundreds of members. A wrong lead erodes their
trust in the tool far more than a missed one costs. So the bar is **precision over
recall: when in doubt, reject.**

**The Framer-fit test (the core question).** Approve only if the job is the kind of
work a Framer designer would actually take: building, redesigning, fixing, or
maintaining a **design-led marketing-style website** — landing pages, portfolios,
marketing/brand sites, simple business sites — that could realistically be built in
Framer or a comparable no-code/design tool. There must also be clear intent to
**hire and pay** (not curiosity, discussion, or someone advertising themselves).

**Reject** if any of these apply:
- The person is advertising their own services (job seeker, not client).
- They are asking for feedback, critique, or opinions on existing work.
- It is a tutorial, learning question, or general discussion.
- It is a complaint, rant, or pricing question about a tool.
- The post is too vague to act on.
- **It is a software-engineering role, not design-led site work.** This is the
  most common false positive — reject requests that ask for a specific code stack
  or app-development skillset even though they're "web": React, Next.js, Vue,
  full-stack, backend/API, databases, web apps, dashboards, SaaS application
  frontends with real product logic, Shopify/WordPress dev, mobile/iOS/Android.
  These are not Framer jobs. (Example to reject: *"Looking for a React/Next.js dev
  to build out our app"* — that's engineering, not Framer design work.)
- The deliverable is something other than a website: logo/branding only,
  graphic design, copywriting, marketing/ads management, SEO-only, etc.

If a post mixes signals (e.g. "designer who can also code a Next.js app"), lean
toward **reject** unless the primary, clearly-stated need is a Framer-style site.

---

## Step 4 — Update each lead

For each lead, run one of:

```bash
python3 scripts/reddit_leads.py --update-status PAGE_ID approved "Brief reason"
python3 scripts/reddit_leads.py --update-status PAGE_ID rejected "Brief reason"
```

Example reasons (approved leads appear in Discord — make them explanatory, 1-2 sentences):
- `"Client with a SaaS product actively hiring a Framer designer for a landing page, mentions budget and timeline"`
- `"Agency looking for a long-term web dev partner for full website builds — clear intent to pay for ongoing work"`
- `"Asking for feedback on existing site, not hiring"`
- `"Job seeker advertising services"`
- `"Hiring a React/Next.js dev to build an app — software engineering, not Framer design work"`

---

## Step 5 — Notify Discord for approved leads

For each lead you approved:

```bash
python3 scripts/reddit_leads.py --notify PAGE_ID
```

This sends a Discord embed to the leads channel and marks the lead as Notified in Notion.
The script only flips the Notified checkbox after the webhook POST succeeds; on failure it
exits non-zero and leaves Notified=False so the lead can be retried later (see Step 6).

---

## Step 6 — Retry any previously-failed notifications

A `--notify` invocation in an earlier session may have failed (e.g. transient Discord
5xx, expired webhook, network blip). Those leads are now `Status=approved` +
`Notified=False` and would otherwise never be re-tried, since `--list-pending` only
returns `Status=pending` leads.

Recover them with:

```bash
python3 scripts/reddit_leads.py --list-unnotified-approved
```

This prints a JSON array of approved-but-unnotified leads with the same fields as
`--list-pending` plus `review_notes` (the explanation set during the original
approval). For each entry, re-run:

```bash
python3 scripts/reddit_leads.py --notify PAGE_ID
```

The original Review Notes are preserved on the page, so the Discord embed will use
the same explanation as if the original notification had succeeded. If the output
is an empty array `[]`, nothing needs retrying — proceed to exit.

---

## Notes

- Process all pending leads in a single session — do not leave any as `pending`
- Use your judgment; it is better to reject an ambiguous post than to spam the Discord channel
- If the script fails with a missing env var error, check that the scheduled trigger
  provides `NOTION_REDDIT_LEADS_DB_ID` and `DISCORD_WEBHOOK_URL_LEADS`
