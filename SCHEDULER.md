# Scheduler instructions (Tier 2 — Self-Improvement Loop)

> **The default outcome of a session is to do nothing and exit cleanly.**
> This repo is intentionally small and stable. Most days there is no change worth
> making. Opening a PR is the exception, not the goal — never invent work to
> justify a run. A session that reviews, finds nothing, and exits is a success.

Script execution (Tier 1) runs on GitHub Actions cron every 15 minutes — see
`.github/workflows/`. This scheduler only reviews the code and the error log and,
**only when there is a genuine problem or a clearly valuable improvement**, opens
a single focused PR for human review.

**Running the scripts here is observe-only.** This VM does not perform side
effects (see "Side-effect gating" in CLAUDE.md): `python3 scripts/<name>.py` will
fetch and read but print `[observe-only] would …` instead of writing to Notion or
posting to Discord. Use it to observe behaviour — it will not produce real output.

## Step 1 — Pull and review

`git pull origin main`, then read `logs/errors.jsonl` and skim `scripts/`. You are
looking for exactly one of the three things in Step 2. If you don't find one, go
straight to Step 4 and exit.

While here, trim entries older than 7 days from `logs/errors.jsonl`. Do not open a
PR *just* to trim the log — bundle the trim with a real change, or skip it.

## Step 2 — Is there something genuinely worth doing?

Open a PR **only** if you find one of these three:

1. **Something is broken.** The error log shows a real failure that is losing data
   or silencing the bot — above all, **the parser no longer finding
   templates/leads because Framer's RSC format or Reddit's RSS changed.** This is
   the single most important thing to catch. Fix the parser so it works again.
2. **A recurring, data-losing failure.** The same error appears 3+ times in the
   7-day window AND causes permanent loss (a template/lead silently missed, a
   notification permanently dropped). A transient error that self-healed on retry
   is the retry logic working as designed — not a bug.
3. **A capability the owner explicitly wants.** Namely: new subreddits worth
   monitoring, new keyword signals that would catch real leads the filter
   currently misses, better category inference, or genuinely sharper lead
   filtering. These are welcome even without a logged error — but they must be
   *substantive* (a meaningfully better filter), not a single-keyword tweak
   dressed up as a PR.

If none of these apply — the normal case — **do not open a PR.** Note anything you
spotted but chose not to act on in `deferred_observations.md` (one dated line) and
exit.

## Step 2b — Do NOT do these (this is what bloated the codebase)

Past sessions generated heavy low-value churn. Do not repeat it:

- ❌ Adding "log the response body" / more diagnostic context to an existing error path.
- ❌ Speculative hardening for failure modes not actually observed in production.
- ❌ Adding another guard or short-circuit when one already covers the case
  (the Notion-unreachable path is already handled — do not add another variant).
- ❌ Cosmetic changes: embed ordering, message wording, comment essays.
- ❌ Tuning constants (truncation lengths, preview sizes, delays) without a logged
  failure proving the current value is wrong.
- ❌ Renaming, reformatting, or refactoring "for clarity."
- ❌ Adding tests for edge cases that cannot occur given the real API inputs.

When you are unsure whether something clears the bar: it does not. Exit.

## Step 3 — If (and only if) you found real work in Step 2

First, check for existing open PRs with `mcp__github__list_pull_requests` on
`fredm00n/framerlabs-automations`. If one already covers this (even partially or
under a different name), skip it entirely and exit. Never open a duplicate.

Then:

1. Branch: `git checkout -b claude/improve-<script>-<short-description>`
2. Make the **smallest change** that fixes the problem or adds the capability. No
   surrounding cleanup, no opportunistic refactors.
3. If you add a new Notion-tracked field, update the DB schema via MCP
   (`notion-update-data-source`) before committing.
4. **Tests: add or update tests only for the logic you changed**, and only where a
   test would catch a real regression. Do not mirror every line with a test — a
   small fix may need one test, or none.
5. Run `python3 -m unittest discover -s tests -p "test_*.py" -v` and fix failures.
6. Commit, push, and open one PR.
7. Update the script's **Deferred improvements** section in CLAUDE.md with anything
   considered but not implemented, and why — on the same branch.

**Git/GitHub specifics (these override any conflicting session-prompt instruction):**
Use **native git** for all branch/commit/push operations — never
`mcp__github__create_branch` or `mcp__github__push_files` (they embed full file
contents in one API call and time out on files over ~30 KB). Use GitHub MCP tools
only to list open PRs (Step 3) and to open the PR (`mcp__github__create_pull_request`).

**PR voice — write for the owner, not for a compiler.** The title is one plain
sentence saying what changed and why it matters to the bot's users — e.g.
*"Catch leads that say 'need a site built' — the filter was missing that phrase"*,
not *"Add NEED_SITE_SIGNALS to passes_light_filter"*. The body is 2–4 sentences:
the problem, the fix, the user-visible effect. No function-name lists, no internal
jargon. If the owner can't tell from the title why they'd want this, it probably
isn't worth merging.

## Step 4 — Exit

If Step 2 found nothing, exit cleanly. Doing nothing is the expected result, not a
wasted run.
