"""Tests for scripts/log_observation.py."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import log_observation  # noqa: E402
from log_observation import (  # noqa: E402
    changed_paths,
    current_branch,
    format_observation,
    push_with_retry,
    trim_log,
)

_NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


def _entry(when: datetime, message: str = 'boom') -> str:
    return json.dumps({
        'timestamp': when.isoformat(),
        'script': 'reddit_leads',
        'severity': 'error',
        'message': message,
    }) + '\n'


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class _FakeGit:
    """Records git invocations and replays a scripted sequence of results."""

    def __init__(self, results=None, stdout=''):
        self.calls = []
        self.results = list(results or [])
        self.stdout = stdout

    def __call__(self, args, check=True):
        self.calls.append(args)
        if self.results:
            code, out = self.results.pop(0)
        else:
            code, out = 0, self.stdout
        return type('R', (), {'returncode': code, 'stdout': out, 'stderr': ''})()


class TestFormatObservation(unittest.TestCase):

    def test_matches_the_files_existing_bullet_shape(self):
        self.assertEqual(
            format_observation('Nothing broken.', '2026-09-19'),
            '\n- **2026-09-19** — Nothing broken.\n',
        )

    def test_strips_surrounding_whitespace_from_the_note(self):
        self.assertEqual(
            format_observation('  padded  \n', '2026-09-19'),
            '\n- **2026-09-19** — padded\n',
        )


class TestTrimLog(unittest.TestCase):

    def test_keeps_entries_inside_the_window(self):
        recent = _entry(_NOW - timedelta(days=2))
        self.assertEqual(trim_log([recent], _NOW, 7), [recent])

    def test_drops_entries_outside_the_window(self):
        stale = _entry(_NOW - timedelta(days=30))
        self.assertEqual(trim_log([stale], _NOW, 7), [])

    def test_boundary_entry_is_kept(self):
        edge = _entry(_NOW - timedelta(days=7) + timedelta(seconds=1))
        self.assertEqual(trim_log([edge], _NOW, 7), [edge])

    def test_keeps_lines_it_cannot_date(self):
        """An entry we cannot date may be the bug; dropping it hides it."""
        rows = [
            'not json at all\n',
            json.dumps({'message': 'no timestamp'}) + '\n',
            json.dumps({'timestamp': 'yesterday-ish'}) + '\n',
            _entry(_NOW - timedelta(days=30)),
        ]
        self.assertEqual(trim_log(rows, _NOW, 7), rows[:3])

    def test_naive_timestamps_are_read_as_utc(self):
        naive = json.dumps({
            'timestamp': (_NOW - timedelta(days=1)).replace(tzinfo=None).isoformat(),
        }) + '\n'
        self.assertEqual(trim_log([naive], _NOW, 7), [naive])

    def test_blank_lines_are_discarded(self):
        recent = _entry(_NOW)
        self.assertEqual(trim_log(['\n', recent, '   \n'], _NOW, 7), [recent])

    def test_order_is_preserved(self):
        rows = [_entry(_NOW - timedelta(days=d)) for d in (1, 3, 30, 2)]
        self.assertEqual(trim_log(rows, _NOW, 7), [rows[0], rows[1], rows[3]])


class TestGitHelpers(unittest.TestCase):

    def test_current_branch_reads_the_symbolic_name(self):
        git = _FakeGit(stdout='main\n')
        self.assertEqual(current_branch(run=git), 'main')
        self.assertEqual(git.calls, [['rev-parse', '--abbrev-ref', 'HEAD']])

    def test_changed_paths_strips_the_porcelain_status_columns(self):
        git = _FakeGit(stdout=' M deferred_observations.md\n?? logs/errors.jsonl\n')
        self.assertEqual(
            changed_paths(['deferred_observations.md', 'logs/errors.jsonl'], run=git),
            ['deferred_observations.md', 'logs/errors.jsonl'],
        )

    def test_changed_paths_is_empty_when_nothing_moved(self):
        self.assertEqual(changed_paths(['a', 'b'], run=_FakeGit(stdout='')), [])


class TestPushWithRetry(unittest.TestCase):

    def test_succeeds_on_the_first_attempt(self):
        # abort, pull, push
        git = _FakeGit([(1, ''), (0, ''), (0, '')])
        self.assertTrue(push_with_retry(run=git, sleep=lambda _: None))
        self.assertEqual([c[0] for c in git.calls], ['rebase', 'pull', 'push'])

    def test_retries_a_lost_push_race(self):
        git = _FakeGit([
            (1, ''), (0, ''), (1, ''),   # attempt 1: push rejected
            (0, ''), (0, ''), (0, ''),   # attempt 2: clean
        ])
        slept = []
        self.assertTrue(push_with_retry(run=git, sleep=slept.append))
        self.assertEqual(slept, [2])

    def test_aborts_a_stale_rebase_before_every_attempt(self):
        """A rebase left in progress dooms every later pull if not aborted."""
        git = _FakeGit([(1, ''), (1, ''), (0, ''), (0, ''), (0, '')])
        push_with_retry(run=git, sleep=lambda _: None)
        self.assertEqual(git.calls[0], ['rebase', '--abort'])
        self.assertEqual(git.calls[2], ['rebase', '--abort'])

    def test_gives_up_after_the_last_attempt_without_sleeping_again(self):
        git = _FakeGit([(0, ''), (1, '')] * 3)
        slept = []
        self.assertFalse(push_with_retry(run=git, sleep=slept.append, attempts=3))
        self.assertEqual(slept, [2, 4])


class TestMain(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.obs = os.path.join(self.tmp.name, 'deferred_observations.md')
        self.log = os.path.join(self.tmp.name, 'errors.jsonl')
        with open(self.obs, 'w') as f:
            f.write('# Deferred observations\n')
        with open(self.log, 'w') as f:
            f.writelines([_entry(_NOW - timedelta(days=30)), _entry(_NOW)])

    def _argv(self, *extra):
        return ['log_observation.py', 'Nothing broken today.',
                '--observations-path', self.obs, '--log-path', self.log, *extra]

    def test_dry_run_changes_nothing_on_disk(self):
        before = (_read(self.obs), _read(self.log))
        with patch.object(sys, 'argv', self._argv('--dry-run')):
            self.assertEqual(log_observation.main(), 0)
        self.assertEqual((_read(self.obs), _read(self.log)), before)

    def test_appends_the_bullet_and_trims_the_log(self):
        git = _FakeGit(stdout='main\n')
        with patch.object(sys, 'argv', self._argv('--no-push')), \
                patch.object(log_observation, '_git', git), \
                patch.object(log_observation, 'current_branch', lambda: 'main'), \
                patch.object(log_observation, 'changed_paths', lambda p: p):
            self.assertEqual(log_observation.main(), 0)
        self.assertIn('Nothing broken today.', _read(self.obs))
        self.assertEqual(len(_read(self.log).splitlines()), 1)

    def test_refuses_an_empty_note(self):
        with patch.object(sys, 'argv', ['log_observation.py', '   ']):
            self.assertEqual(log_observation.main(), 2)

    def test_writes_the_files_but_will_not_commit_off_main(self):
        with patch.object(sys, 'argv', self._argv()), \
                patch.object(log_observation, 'current_branch', lambda: 'claude/wip'):
            self.assertEqual(log_observation.main(), 1)
        self.assertIn('Nothing broken today.', _read(self.obs))

    def test_missing_log_file_is_not_an_error(self):
        os.remove(self.log)
        with patch.object(sys, 'argv', self._argv('--dry-run')):
            self.assertEqual(log_observation.main(), 0)


if __name__ == '__main__':
    unittest.main()
