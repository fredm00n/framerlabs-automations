# Reddit Leads Reviewer (Daily Dedicated Session)

Review Reddit posts that passed the light keyword filter and decide which are genuine
Framer freelance leads worth pursuing.

**Session trigger prompt** (set once, never changes):
```
Read CLAUDE.md and REDDIT_LEADS_REVIEWER.md, then follow the instructions in REDDIT_LEADS_REVIEWER.md.
```

---

## Step 1 — Load credentials

Use the `Read` tool to open `.env` and extract:
- `NOTION_TOKEN`
- `NOTION_REDDIT_LEADS_DB_ID`
- `DISCORD_WEBHOOK_URL_LEADS`

Set them as environment variables using `export` in Bash, or pass them inline to each command.

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
- `content` — First 1000 chars of post content

If the output is an empty array `[]`, there is nothing to review — exit cleanly.

---

## Step 3 — Evaluate each lead

For each lead, read the title and content carefully and decide: **approve** or **reject**.

**Approve** if the post clearly shows someone wanting to hire a web designer/developer:
- They are looking to pay someone to build, redesign, fix, or maintain a website or Framer project
- There is clear intent to hire (not just curiosity or discussion)
- The work could realistically be done in Framer (websites, landing pages, portfolios, marketing sites)

**Reject** if:
- The person is advertising their own services (job seeker, not client)
- They are asking for feedback, critique, or opinions on existing work
- It is a tutorial, learning question, or general discussion
- It is a complaint, rant, or pricing question about a tool
- They want to hire but for something unrelated to web/Framer work (e.g., logo design only, mobile app, backend)
- The post is too vague to act on

---

## Step 4 — Update each lead

For each lead, run one of:

```bash
python3 scripts/reddit_leads.py --update-status PAGE_ID approved "Brief reason"
python3 scripts/reddit_leads.py --update-status PAGE_ID rejected "Brief reason"
```

Example reasons:
- `"Hiring a Framer developer for landing page, has budget"`
- `"Asking for feedback on existing site, not hiring"`
- `"Job seeker advertising services"`

---

## Step 5 — Notify Discord for approved leads

For each lead you approved:

```bash
python3 scripts/reddit_leads.py --notify PAGE_ID
```

This sends a Discord embed to the leads channel and marks the lead as Notified in Notion.

---

## Notes

- Process all pending leads in a single session — do not leave any as `pending`
- Use your judgment; it is better to reject an ambiguous post than to spam the Discord channel
- If the script fails with a missing env var error, check that `.env` contains
  `NOTION_REDDIT_LEADS_DB_ID` and `DISCORD_WEBHOOK_URL_LEADS`
