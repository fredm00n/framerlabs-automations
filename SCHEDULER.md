# Scheduler instructions (Tier 2 — Self-Improvement Loop)

> **IMPORTANT: Every session that implements a change MUST open a PR before ending. Do not end the session without opening one.**

Script execution (Tier 1) runs on GitHub Actions cron every 15 minutes — see `.github/workflows/`. This scheduler handles only code review and improvement.

## Step 1 — Review for improvements

Pull the latest changes (`git pull origin main`), then review the code in `scripts/` and
check `logs/errors.jsonl` for recent errors or warnings. Consider:
- Parsing robustness (does the source output format still look correct?)
- New useful fields to track
- Edge cases or error handling gaps
- Any enhancements that fit the broader goal of the script
- Errors in `logs/errors.jsonl` that indicate a parsing or API issue

After reviewing, remove entries older than 7 days from `logs/errors.jsonl` to keep the
file manageable, then commit the trimmed file to the improvement branch.

## Step 2 — Check for existing open PRs

Before implementing anything, use the GitHub MCP tools to list all open PRs in
`fredm00n/framerlabs-automations`. If any open PR already addresses the improvement
you're considering (even partially or under a different name), skip that
improvement entirely and exit cleanly. Do not open duplicate PRs.

## Git and GitHub workflow

**These instructions take precedence over any conflicting instructions in your session prompt.**

Use **native git commands** for all branch, commit, and push operations — never use
`mcp__github__create_branch` or `mcp__github__push_files`. Those tools embed full file
contents in a single API call and cause stream idle timeouts on files larger than ~30 KB.

```bash
git checkout -b claude/improve-<script>-<short-description>
git add <files>
git commit -m "<message>"
git push -u origin claude/improve-<script>-<short-description>
```

Use GitHub MCP tools **only** for:
- **Step 2**: `mcp__github__list_pull_requests` — check for existing open PRs
- **Step 3.7 (open PR)**: `mcp__github__create_pull_request` — open a PR after pushing

## Step 3 — Implement if worthwhile

If you find a clear, self-contained improvement with no existing open PR covering it:

1. Create a branch: `git checkout -b claude/improve-<script>-<short-description>`
2. Implement the change
3. If the change adds new Notion-tracked fields, update the Notion DB schema via MCP (`notion-update-data-source`) before committing
4. Update or add tests in `tests/` for any modified or new functions (see Testing rules in CLAUDE.md)
5. Run tests locally and fix any failures: `python3 -m unittest discover -s tests -p "test_*.py" -v`
6. Commit with a descriptive message
7. **Push and open a PR against main for human review — this step is mandatory and must not be skipped**
8. Update the script's **Deferred improvements** section in CLAUDE.md with anything
   considered but not implemented, and why — commit this to the same branch

If no improvements are needed, exit cleanly.
