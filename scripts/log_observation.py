"""
Persist one self-improvement-loop observation to the repo.

The Tier 2a self-improvement session (SCHEDULER.md) ends most runs having found
nothing worth a PR. What it *did* look at is the only memory the next session
has, so it must be written down and pushed — a note left uncommitted in the VM
dies with the VM, and the loop starts cold again the next day.

This script removes the judgement call. One command does the whole chore:

    python3 scripts/log_observation.py "Zero errors in the last 7 days. ..."

1. Appends a dated bullet to deferred_observations.md.
2. Drops entries older than --retention-days from logs/errors.jsonl.
3. Commits both paths and pushes to main, retrying through the push race with
   the two GitHub Actions crons that also push to main every 15 minutes.

This is the one sanctioned direct push to main (see CLAUDE.md, Development
workflow) — it touches no code, only the loop's own notes, so routing it
through a PR would mean a human merging a chore commit every day.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

_OBSERVATIONS_PATH = 'deferred_observations.md'
_LOG_PATH = 'logs/errors.jsonl'
_RETENTION_DAYS = 7
_BRANCH = 'main'

# Matches the retry ladder in .github/workflows/*.yml: both crons push to main
# every 15 minutes, so a bare push loses the race often enough to matter.
_PUSH_ATTEMPTS = 5


def format_observation(note: str, today: str) -> str:
    """Render one observation as the dated bullet the file already uses.

    Args:
        note:  The session's findings, as free prose.
        today: ISO 8601 date (YYYY-MM-DD).

    Returns:
        The bullet, newline-terminated, ready to append.
    """
    return f'\n- **{today}** — {note.strip()}\n'


def trim_log(lines: list[str], now: datetime, retention_days: int) -> list[str]:
    """Drop log entries older than the retention window.

    A line whose timestamp cannot be read is kept, not dropped: an entry we
    cannot date is more likely a bug worth seeing than noise worth deleting.

    Args:
        lines:          Raw lines from logs/errors.jsonl.
        now:            Timezone-aware reference time.
        retention_days: Age in days beyond which an entry is dropped.

    Returns:
        The lines to keep, in their original order.
    """
    cutoff = now - timedelta(days=retention_days)
    kept = []
    for line in lines:
        if not line.strip():
            continue
        try:
            stamp = datetime.fromisoformat(json.loads(line)['timestamp'])
        except (ValueError, KeyError, TypeError):  # JSONDecodeError is a ValueError
            kept.append(line)
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp >= cutoff:
            kept.append(line)
    return kept


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command from the repo root."""
    return subprocess.run(
        ['git'] + args,
        capture_output=True,
        text=True,
        check=check,
    )


def current_branch(run=_git) -> str:
    """Return the checked-out branch name."""
    return run(['rev-parse', '--abbrev-ref', 'HEAD']).stdout.strip()


def changed_paths(paths: list[str], run=_git) -> list[str]:
    """Return the subset of `paths` git sees as modified or untracked."""
    result = run(['status', '--porcelain', '--'] + paths)
    changed = []
    for line in result.stdout.splitlines():
        # Porcelain v1: two status columns, a space, then the path.
        path = line[3:].strip()
        if path:
            changed.append(path)
    return changed


def push_with_retry(run=_git, sleep=time.sleep, attempts: int = _PUSH_ATTEMPTS) -> bool:
    """Rebase onto origin/main and push, retrying through lost races.

    Aborts any rebase left in progress by a previous attempt first — otherwise
    the next pull fails immediately with "a rebase is in progress" and dooms
    every remaining attempt (the same failure the workflows hit in May 2026).

    Returns:
        True once a push succeeds, False if every attempt failed.
    """
    for attempt in range(1, attempts + 1):
        run(['rebase', '--abort'], check=False)
        pulled = run(['pull', '--rebase', '--autostash', 'origin', _BRANCH], check=False)
        if pulled.returncode == 0:
            pushed = run(['push', 'origin', _BRANCH], check=False)
            if pushed.returncode == 0:
                return True
        print(f'[log_observation] push attempt {attempt} failed, retrying...', file=sys.stderr)
        if attempt < attempts:
            sleep(2 ** attempt)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('note', help="The session's findings, as free prose.")
    parser.add_argument(
        '--observations-path', default=_OBSERVATIONS_PATH,
        help='Path to the observations file.')
    parser.add_argument(
        '--log-path', default=_LOG_PATH,
        help='Path to the JSONL error log to trim.')
    parser.add_argument(
        '--retention-days', type=int, default=_RETENTION_DAYS,
        help='Drop log entries older than this many days.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print what would change and touch neither the files nor git.')
    parser.add_argument(
        '--no-push', action='store_true',
        help='Write and commit locally, but do not push.')
    args = parser.parse_args()

    if not args.note.strip():
        print('[log_observation] Refusing to log an empty observation.', file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    bullet = format_observation(args.note, today)

    log_lines = []
    if os.path.exists(args.log_path):
        with open(args.log_path) as f:
            log_lines = f.readlines()
    kept = trim_log(log_lines, now, args.retention_days)
    dropped = len(log_lines) - len(kept)

    if args.dry_run:
        print(f'[dry-run] would append to {args.observations_path}:{bullet}', end='')
        print(f'[dry-run] would drop {dropped} log entries older than '
              f'{args.retention_days} days ({len(kept)} remain)')
        return 0

    with open(args.observations_path, 'a') as f:
        f.write(bullet)
    if dropped:
        with open(args.log_path, 'w') as f:
            f.writelines(kept)
    print(f'Logged observation for {today}; dropped {dropped} stale log entries.')

    branch = current_branch()
    if branch != _BRANCH:
        print(f'[log_observation] On branch {branch!r}, not {_BRANCH!r} — wrote the'
              ' files but did not commit. Re-run from main to publish.', file=sys.stderr)
        return 1

    # Commit by pathspec so an unrelated dirty file in the VM cannot ride along.
    paths = [args.observations_path, args.log_path]
    to_commit = changed_paths(paths)
    if not to_commit:
        print('[log_observation] Nothing changed; no commit made.')
        return 0
    _git(['add', '--'] + to_commit)
    _git(['commit', '-m', f'chore: scheduler observation log {today} [skip ci]',
          '--'] + to_commit)

    if args.no_push:
        print('Committed locally; --no-push, so nothing was pushed.')
        return 0
    if not push_with_retry():
        print(f'[log_observation] Push failed after {_PUSH_ATTEMPTS} attempts.',
              file=sys.stderr)
        return 1
    print(f'Pushed to {_BRANCH}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
