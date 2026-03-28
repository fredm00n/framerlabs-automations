# framerlabs-automations

Community automations for the Framer ecosystem — powered by Python, GitHub Actions, and Claude AI.

## What this is

A set of scripts that monitor things useful to the Framer community, persist state in Notion, and post to Discord. The architecture has two tiers:

- **Tier 1 — GitHub Actions cron**: Python scripts run every 2 hours. No LLM, no token cost. Just fast, cheap data collection.
- **Tier 2 — Daily Claude sessions**: Claude reads the collected data and applies reasoning to filter, approve, and act on it. Claude also reviews its own code and ships improvements as PRs — more on that below.

---

## Scripts

### Framer Templates Monitor
Watches the [Framer Marketplace](https://www.framer.com/marketplace/templates/?sort=recent) for new templates and posts them to Discord as they appear.

Fetches directly from Framer's Next.js RSC endpoint — no headless browser, no scraping library.

### Reddit Leads Monitor
Monitors 43 subreddits for posts from people looking to hire Framer designers or developers, and surfaces them in a Discord channel.

This one runs in two phases:

**Phase 1 — Light filter (every 2h, GitHub Actions, no LLM)**
Fetches Reddit RSS feeds, applies a keyword filter tuned per subreddit category (hiring boards, design communities, startups, etc.), and saves candidates to a Notion database as `pending`. Fast and cheap.

**Phase 2 — Claude review (daily, reasoning enabled)**
A dedicated Claude Code session reads all pending leads from Notion, evaluates each one with full reasoning (is this a genuine hire request? is there budget? does it fit Framer work?), marks them `approved` or `rejected`, and posts only the approved ones to Discord.

This two-phase approach eliminates the false positives you get from pure keyword matching, while keeping the LLM cost low by only running reasoning once a day on a small filtered set.

---

## The self-improvement loop

The most interesting part of this repo is `SCHEDULER.md`.

Once a day, a Claude Code session wakes up, reads that file, and follows the instructions:
1. Check recent GitHub Actions run logs for errors or regressions
2. Review the codebase for improvements
3. If something is worth fixing, open a PR

Claude writes the code, writes the tests, and opens the PR. A human reviews and merges. Over time the scripts get better without manual intervention.

The instructions Claude follows are just a markdown file. That file is in this repo. You can read it, fork it, and point it at your own codebase.

---

## Stack

- **Runtime**: Python 3, stdlib only — no pip dependencies
- **State**: Notion REST API (direct HTTP, no SDK)
- **Notifications**: Discord webhooks
- **CI/CD**: GitHub Actions
- **AI**: Claude (Anthropic) — for lead reasoning and self-improvement

---

## Running it yourself

1. Fork this repo
2. Create the required Notion databases (see `CLAUDE.md` for schema)
3. Add the secrets listed in `.env.example` as GitHub Actions repository secrets
4. Enable GitHub Actions on your fork

For the Claude review sessions (lead reviewer + self-improvement loop), you'll need a Claude Code subscription and a scheduled trigger pointing at this repo. The prompts are in `SCHEDULER.md` and `REDDIT_LEADS_REVIEWER.md`.

---

## Community

Built for the [Framer Labs](https://github.com/fredm00n/framerlabs) community.
Questions and PRs welcome.
