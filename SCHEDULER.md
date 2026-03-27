# Scheduler instructions

When this repo is triggered by the scheduler, follow these steps in order.

## Step 1 — Run all scripts

Run each script in sequence:

```bash
python3 scripts/framer_templates.py
```

When new scripts are added to the repo, add them to this list.

## Step 2 — Review for improvements

After running the scripts, review the output and the code in `scripts/`. Consider:
- Parsing robustness (does the source output format still look correct?)
- New useful fields to track
- Edge cases or error handling gaps
- Any enhancements that fit the broader goal of the script

## Step 3 — Check for existing open PRs

Before implementing anything, use the GitHub MCP tools to list all open PRs in
`fredm00n/claude-automations`. If any open PR already addresses the improvement
you're considering (even partially or under a different name), skip that
improvement entirely and exit cleanly. Do not open duplicate PRs.

## Step 4 — Implement if worthwhile

If you find a clear, self-contained improvement with no existing open PR covering it:

1. Create a branch: `claude/improve-<script>-<short-description>`
2. Implement the change
3. Update or add tests in `tests/` for any modified or new functions (see Testing rules in CLAUDE.md)
4. Run tests locally and fix any failures: `python3 -m unittest discover -s tests -p "test_*.py" -v`
5. Commit with a descriptive message
6. Push and open a PR against main for human review
7. Update the script's **Deferred improvements** section in CLAUDE.md with anything
   considered but not implemented, and why — commit this to the same branch

If no improvements are needed, exit cleanly.
