"""Production workflow tests. No real JIRA writes; isolated filesystem/Git fixtures."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('jira_under_test', ROOT / 'jira.py')
j = importlib.util.module_from_spec(spec)
spec.loader.exec_module(j)
SCRATCH = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'hermes/cache/scratch'
SCRATCH.mkdir(parents=True, exist_ok=True)


def issue(key='T-1', state='Working', assignee='tester', reporter='qa'):
    cat = {'Initial': 'new', 'Working': 'indeterminate', 'ST Check': 'indeterminate', 'Closed': 'done'}[state]
    return {'key': key, 'fields': {
        'summary': 'Example issue', 'description': 'Actual steps', 'updated': '2026-10-27T01:00:00Z',
        'status': {'id': state, 'name': state, 'statusCategory': {'key': cat}},
        'assignee': {'name': assignee}, 'reporter': {'name': reporter},
        'resolution': {'name': 'Fixed'} if state == 'Closed' else None,
        'comment': {'startAt': 0, 'total': 1, 'comments': [{'id': '1', 'body': 'old', 'author': {'name': 'qa'}}]},
        'attachment': [],
    }, 'changelog': {'startAt': 0, 'total': 1, 'histories': [{'id': '1', 'items': []}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jira-productivity-', dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'JIRA_RUNS_DIR': str(self.root / 'runs')})
        env.start()
        self.addCleanup(env.stop)
        j.C = {'base': 'http://mock.invalid', 'user': 'tester', 'pw': 'test', 'timeout': 2,
               'path': str(self.root / 'creds.yaml'), 'active': 'T', 'projects': ['T']}
        j._nearby = {}


class TriageTests(Base):
    def test_one_bundle_paginates_comments_and_exposes_history_truncation(self):
        d = issue()
        d['fields']['comment']['total'] = 2
        d['changelog']['total'] = 3
        calls = []

        def request(method, path, params=None, **kw):
            calls.append((method, path, params))
            self.assertEqual(method, 'GET')
            if path.endswith('/changelog'):
                raise SystemExit(1)  # Server variant without history pagination endpoint
            if path.endswith('/comment'):
                return 200, {'startAt': 1, 'total': 2, 'comments': [
                    {'id': '2', 'body': 'new QA request', 'author': {'name': 'qa'}}]}
            if path.endswith('/transitions'):
                return 200, {'transitions': [{'id': '11', 'name': '解决', 'to': {'name': 'ST Check'}}]}
            return 200, copy.deepcopy(d)

        with patch.object(j, '_request', side_effect=request):
            result = j._collect_triage(['T-1'])
        row = result['issues'][0]
        self.assertEqual(row['comments']['fetched'], 2)
        self.assertTrue(row['comments']['complete'])
        self.assertFalse(row['history']['complete'])
        self.assertEqual(row['history']['total'], 3)
        self.assertIn('new QA request', str(row))
        self.assertFalse(result['complete'])
        self.assertEqual(sum(p == '/rest/api/2/issue/T-1' for _, p, _ in calls), 1)

    def test_key_scope_and_format_fail_before_requests(self):
        with patch.object(j, '_request') as request:
            for keys in [['T-1', 'BPM-2'], ['T-01'], ['T-1 OR project=OTHER'], ['t-1']]:
                with self.subTest(keys=keys), self.assertRaises(SystemExit):
                    j._validated_keys(keys)
            request.assert_not_called()
        self.assertEqual(j._validated_keys(['T-1', 'T-1', 'T-2']), ['T-1', 'T-2'])

    def test_partial_failure_keeps_other_issues_and_errors(self):
        def request(method, path, **kw):
            if 'T-2' in path:
                raise SystemExit(1)
            if path.endswith('/transitions'):
                return 200, {'transitions': []}
            return 200, issue()
        with patch.object(j, '_request', side_effect=request):
            result = j._collect_triage(['T-1', 'T-2'])
        self.assertEqual(result['requested'], 2)
        self.assertEqual(result['count'], 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'][0]['key'], 'T-2')


class DeliveryTests(Base):
    def git(self, *args, cwd=None, ok=True):
        p = subprocess.run(['git', *args], cwd=cwd or self.repo, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def setUp(self):
        super().setUp()
        self.repo = self.root / 'work'
        self.repo.mkdir()
        self.remote = self.root / 'remote.git'
        self.git('init', '--bare', str(self.remote))
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Offline Tester')
        (self.repo / 'fix.txt').write_text('first', encoding='utf-8')
        self.git('add', 'fix.txt')
        self.git('commit', '-m', 'T-1 fix')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-u', 'origin', 'main')

    def test_fresh_remote_contains_explicit_fix(self):
        result = j._verify_delivery(str(self.repo), [self.sha])
        self.assertEqual(result['remote_sha'], self.sha)
        self.assertEqual(result['fix_commits'], [self.sha])
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_gate_does_not_overwrite_other_sessions_fetch_head(self):
        fetch_head = self.repo / '.git' / 'FETCH_HEAD'
        fetch_head.write_text('another session owns this file\n', encoding='utf-8')
        j._verify_delivery(str(self.repo), [self.sha])
        self.assertEqual(fetch_head.read_text(encoding='utf-8'), 'another session owns this file\n')
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/jira-bugfix/checks'), '')

    def test_missing_fix_dirty_and_unpushed_each_block(self):
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), [])
        (self.repo / 'fix.txt').write_text('second', encoding='utf-8')
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), [self.sha])
        self.git('add', 'fix.txt')
        self.git('commit', '-m', 'T-2 unpublished')
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), [self.git('rev-parse', 'HEAD')])

    def test_fresh_fetch_detects_remote_rewind_and_missing_commit(self):
        (self.repo / 'fix.txt').write_text('second', encoding='utf-8')
        self.git('add', 'fix.txt')
        self.git('commit', '-m', 'T-2 fix')
        second = self.git('rev-parse', 'HEAD')
        self.git('push', 'origin', 'main')
        self.git('update-ref', 'refs/heads/main', self.sha, cwd=self.remote)
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), [second])
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), ['not-a-sha'])

    def test_fetch_error_and_foreign_commit_block(self):
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), ['a' * 40])
        self.git('remote', 'set-url', 'origin', str(self.root / 'does-not-exist.git'))
        with self.assertRaises(SystemExit):
            j._verify_delivery(str(self.repo), [self.sha])


class SimJira:
    """Stateful service substitute, including a committed write with lost response."""
    def __init__(self):
        self.issues = {k: issue(k) for k in ('T-1', 'T-2')}
        self.comments = {k: [] for k in self.issues}
        self.blobs = {}
        self.calls = []
        self.lose = None
        self.fail_assign_once = False
        self.corrupt_field = False

    def __call__(self, method, path, params=None, body=None, **kw):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == '/rest/api/2/myself':
            return 200, {'name': 'tester', 'displayName': 'Test'}
        if path in self.blobs:
            return 200, self.blobs[path]
        key = path.split('/')[5] if path.startswith('/rest/api/2/issue/') else None
        d = self.issues[key]
        f = d['fields']
        if method == 'GET':
            if path.endswith('/comment'):
                return 200, {'comments': copy.deepcopy(self.comments[key]), 'total': len(self.comments[key]), 'startAt': 0}
            if '/comment/' in path:
                return 200, copy.deepcopy(next(c for c in self.comments[key] if str(c['id']) == path.rsplit('/', 1)[1]))
            if path.endswith('/transitions'):
                state = f['status']['name']
                ts = []
                if state == 'Initial':
                    ts = [{'id': '41', 'name': '接受', 'to': issue(state='Working')['fields']['status'], 'fields': {}}]
                if state == 'Working':
                    ts = [{'id': '11', 'name': '解决', 'to': issue(state='ST Check')['fields']['status'],
                           'fields': {'customfield_1': {'name': '影响范围', 'required': True, 'schema': {'type': 'string'}}}}]
                return 200, {'transitions': ts}
            return 200, copy.deepcopy(d)
        if method == 'POST' and path.endswith('/transitions'):
            state = 'Working' if body['transition']['id'] == '41' else 'ST Check'
            f['status'] = issue(state=state)['fields']['status']
            f.update(copy.deepcopy(body.get('fields') or {}))
            if self.corrupt_field:
                f['customfield_1'] = 'server ignored requested value'
        elif method == 'PUT' and path.endswith('/assignee'):
            if self.fail_assign_once:
                self.fail_assign_once = False
                raise j.WriteUncertain('network lost before assign')
            f['assignee'] = copy.deepcopy(body)
        elif method == 'POST' and path.endswith('/comment'):
            c = {'id': str(len(self.comments[key]) + 1), 'body': body['body'], 'author': {'name': 'tester'}}
            self.comments[key].append(c)
        else:
            raise AssertionError((method, path))
        if self.lose and self.lose in path:
            self.lose = None
            raise j.WriteUncertain('response lost AFTER commit')
        return 201, copy.deepcopy(self.comments[key][-1]) if path.endswith('/comment') else None

    def writes(self, suffix=''):
        return [x for x in self.calls if x[0] != 'GET' and x[1].endswith(suffix)]


class ApiCase(Base):
    def setUp(self):
        super().setUp()
        self.api = SimJira()
        self.api_patch = patch.object(j, '_request', side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)


class IdempotencyTests(ApiCase):
    def test_working_start_and_qa_resolve_skip_without_write(self):
        self.assertEqual(j._safe_start('T-1')['status'], 'skipped')
        self.api.issues['T-1']['fields']['status'] = issue(state='ST Check')['fields']['status']
        self.assertEqual(j._safe_resolve('T-1', {})['status'], 'skipped')
        self.assertEqual(self.api.writes(), [])

    def test_dry_run_can_probe_fields_without_delivery_evidence(self):
        with patch.object(j, '_verify_delivery') as gate:
            result = j._safe_resolve('T-1', {'impact': 'scope'}, dry_run=True)
        gate.assert_not_called()
        self.assertEqual(result['status'], 'planned')
        self.assertFalse(result['delivery']['verified'])
        self.assertIn('customfield_1', result['fields'])
        self.assertEqual(self.api.writes(), [])

    def test_dry_run_missing_fields_prints_screen_before_rejecting(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            j._safe_resolve('T-1', {}, dry_run=True)
        self.assertIn('customfield_1', output.getvalue())
        self.assertIn('影响范围', output.getvalue())
        self.assertEqual(self.api.writes(), [])

    def test_comment_repeat_and_lost_response_do_not_duplicate(self):
        self.api.lose = '/comment'
        first = j._safe_comment('T-1', 'exact body')
        second = j._safe_comment('T-1', 'exact body')
        self.assertIn(first['status'], ('done', 'reconciled'))
        self.assertEqual(second['status'], 'skipped')
        self.assertEqual(len(self.api.writes('/comment')), 1)

    def test_resolve_without_delivery_never_writes(self):
        with self.assertRaises(SystemExit):
            j._safe_resolve('T-1', {'impact': 'scope'})
        self.assertEqual(self.api.writes(), [])

    def test_lost_transition_reply_is_reconciled_and_reporter_is_used(self):
        self.api.lose = '/transitions'
        with patch.object(j, '_verify_delivery', return_value={'verified': True}):
            result = j._safe_resolve('T-1', {'impact': 'scope', 'repo': 'unused', 'fix_commits': ['abc1234']})
            repeated = j._safe_resolve('T-1', {'impact': 'scope', 'repo': 'unused', 'fix_commits': ['abc1234']})
        self.assertEqual(result['status'], 'done')
        self.assertEqual(repeated['status'], 'skipped')
        self.assertEqual(len(self.api.writes('/transitions')), 1)
        self.assertEqual(self.api.issues['T-1']['fields']['assignee']['name'], 'qa')

    def test_readback_wrong_nonempty_field_is_not_success(self):
        self.api.corrupt_field = True
        with patch.object(j, '_verify_delivery', return_value={'verified': True}), self.assertRaises(SystemExit):
            j._safe_resolve('T-1', {'impact': 'scope', 'repo': 'unused', 'fix_commits': ['abc1234']})
        self.assertEqual(len(self.api.writes('/transitions')), 1)
        self.assertEqual(len(self.api.writes('/assignee')), 0)

    def test_duplicate_attachment_compares_content_not_only_name(self):
        f = self.root / 'shot.png'
        f.write_bytes(b'ABCD')
        self.api.issues['T-1']['fields']['attachment'] = [
            {'id': '8', 'filename': 'shot.png', 'size': 4, 'content': 'http://mock.invalid/a/8'}]
        self.api.blobs['http://mock.invalid/a/8'] = b'ABCD'
        result = j._safe_upload('T-1', str(f))
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(self.api.writes(), [])
        self.api.blobs['http://mock.invalid/a/8'] = b'DCBA'
        self.assertIsNone(j._attachment_match('T-1', 'shot.png', b'ABCD'))


class TransportTests(Base):
    def test_post_timeout_is_not_retried(self):
        with patch.object(j.urllib.request, 'urlopen', side_effect=OSError('reset')) as send, self.assertRaises(j.WriteUncertain):
            j._request('POST', '/rest/api/2/issue/T-1/comment', body={'body': 'x'})
        self.assertEqual(send.call_count, 1)

    def test_post_server_error_is_not_retried(self):
        err = j.urllib.error.HTTPError('http://mock.invalid', 503, 'busy', {}, io.BytesIO(b'{}'))
        with patch.object(j.urllib.request, 'urlopen', side_effect=err) as send, self.assertRaises(j.WriteUncertain):
            j._request('POST', '/rest/api/2/issue/T-1/transitions', body={})
        self.assertEqual(send.call_count, 1)

    def test_get_still_retries(self):
        with patch.object(j.urllib.request, 'urlopen', side_effect=OSError('reset')) as send, \
                patch.object(j.time, 'sleep'), self.assertRaises(SystemExit):
            j._request('GET', '/rest/api/2/issue/T-1')
        self.assertEqual(send.call_count, 3)


class RecoveryTests(ApiCase):
    def test_resume_after_assign_response_loss_never_retransitions(self):
        checkpoint = {}
        self.api.fail_assign_once = True
        opts = {'impact': 'scope', 'repo': 'unused', 'fix_commits': ['abc1234']}
        with patch.object(j, '_verify_delivery', return_value={'verified': True}):
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts, checkpoint=checkpoint)
            # Uncertain assignment may still be processed late by Jira.
            self.api.issues['T-1']['fields']['assignee'] = {'name': 'qa'}
            result = j._safe_resolve('T-1', opts, checkpoint=checkpoint)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(len(self.api.writes('/transitions')), 1)
        self.assertEqual(len(self.api.writes('/assignee')), 1)

    def test_standalone_resume_uses_saved_checkpoint(self):
        self.api.fail_assign_once = True
        opts = {'impact': 'scope', 'repo': 'unused', 'fix_commits': ['abc1234']}
        with patch.object(j, '_verify_delivery', return_value={'verified': True}):
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts)
            # Still not assigned: do not silently claim skipped/success just because state is QA.
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts)
        self.assertEqual(len(self.api.writes('/transitions')), 1)


class BatchTests(Base):
    def setUp(self):
        super().setUp()
        self.api = SimJira()
        self.api.issues['T-2']['fields']['reporter'] = {'name': 'qa2'}
        self.p = patch.object(j, '_request', side_effect=self.api)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.g = patch.object(j, '_verify_delivery', return_value={'verified': True})
        self.g.start()
        self.addCleanup(self.g.stop)
        self.task = str(self.root / 'task.json')
        self.manifest = {'action': 'resolve', 'repo': str(self.root), 'issues': [
            {'key': 'T-1', 'impact': 'one', 'fix_commits': ['abc1234']},
            {'key': 'T-2', 'impact': 'two', 'fix_commits': ['abc1234']}]}

    def test_plan_and_default_run_are_read_only(self):
        j._batch_plan(self.manifest, self.task)
        result = j._batch_run(self.task, execute=False)
        self.assertEqual(result['summary']['planned'], 2)
        self.assertEqual(self.api.writes(), [])
        self.assertEqual(json.loads(Path(self.task).read_text(encoding='utf-8'))['items'][0]['status'], 'pending')

    def test_execute_and_resume_no_duplicate_reporter_per_issue(self):
        j._batch_plan(self.manifest, self.task)
        first = j._batch_run(self.task, execute=True)
        before = len(self.api.writes())
        again = j._batch_run(self.task, execute=True)
        self.assertEqual(first['summary']['done'], 2)
        self.assertEqual(again['summary']['done'], 2)
        self.assertEqual(len(self.api.writes()), before)
        self.assertEqual(self.api.issues['T-1']['fields']['assignee']['name'], 'qa')
        self.assertEqual(self.api.issues['T-2']['fields']['assignee']['name'], 'qa2')

    def test_failure_isolated_then_resume_after_remote_reconciliation(self):
        j._batch_plan(self.manifest, self.task)
        self.api.fail_assign_once = True
        first = j._batch_run(self.task, execute=True)
        self.assertEqual(first['summary']['failed'], 1)
        self.assertEqual(first['summary']['done'], 1)
        self.api.issues['T-1']['fields']['assignee'] = {'name': 'qa'}
        again = j._batch_run(self.task, execute=True)
        self.assertEqual(again['summary']['done'], 2)
        self.assertEqual(len(self.api.writes('/transitions')), 2)

    def test_duplicate_scope_instance_actor_and_lock_block(self):
        dup = copy.deepcopy(self.manifest)
        dup['issues'][1]['key'] = 'T-1'
        with self.assertRaises(SystemExit):
            j._batch_plan(dup, self.task)
        j._batch_plan(self.manifest, self.task)
        j.C['base'] = 'https://other.invalid'
        with self.assertRaises(SystemExit):
            j._batch_run(self.task, execute=True)
        j.C['base'] = 'http://mock.invalid'
        with patch.object(j, '_current_user', return_value='someone_else'), self.assertRaises(SystemExit):
            j._batch_run(self.task, execute=True)
        with j._file_lock(self.task + '.lock'), self.assertRaises(SystemExit):
            j._batch_run(self.task, execute=True)
        self.assertEqual(self.api.writes(), [])

    def test_reopened_done_item_never_replays_old_task(self):
        j._batch_plan(self.manifest, self.task)
        j._batch_run(self.task, execute=True)
        self.api.issues['T-1'] = issue(state='Initial')
        before = len(self.api.writes())
        result = j._batch_run(self.task, execute=True)
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(len(self.api.writes()), before)

    def test_git_gate_blocks_before_automatic_start(self):
        self.api.issues['T-1'] = issue(state='Initial')
        j._batch_plan(self.manifest, self.task)
        with patch.object(j, '_verify_delivery', side_effect=SystemExit(1)):
            result = j._batch_run(self.task, execute=True)
        self.assertEqual(result['summary']['failed'], 2)
        self.assertEqual(self.api.writes(), [])


class SafetyEdgesTests(Base):
    def test_missing_embedded_history_is_not_called_complete(self):
        result = j._read_collection('T-1', 'changelog', None)
        self.assertFalse(result['complete'])

    def test_corrupt_effect_record_cannot_authorize_write(self):
        api = SimJira()
        with patch.object(j, '_request', side_effect=api):
            j._safe_comment('T-1', 'persist me')
            effect = next((self.root / 'runs/effects').glob('*.json'))
            effect.write_text('{}', encoding='utf-8')
            api.comments['T-1'] = []
            with self.assertRaises(SystemExit):
                j._safe_comment('T-1', 'persist me')
        self.assertEqual(len(api.writes('/comment')), 1)

    def test_plan_with_typo_or_wrong_types_does_not_make_requests(self):
        with patch.object(j, '_request') as request:
            for manifest in [
                {'action': 'start', 'issues': [{'key': 'T-1', 'comment': 42}]},
                {'action': 'resolve', 'issues': [{'key': 'T-1', 'repo': 42, 'fix_commits': ['abc1234']}]},
            ]:
                with self.subTest(manifest=manifest), self.assertRaises(SystemExit):
                    j._batch_plan(manifest, str(self.root / 't.json'))
            request.assert_not_called()

    def test_completed_comment_deleted_is_not_success_on_second_resume(self):
        api = SimJira()
        task = str(self.root / 'comment-task.json')
        manifest = {'action': 'resolve', 'repo': str(self.root), 'issues': [
            {'key': 'T-1', 'impact': 'x', 'fix_commits': ['abc1234'], 'comment': 'hand-off note'}]}
        with patch.object(j, '_request', side_effect=api), patch.object(j, '_verify_delivery', return_value={}):
            j._batch_plan(manifest, task)
            j._batch_run(task, True)
            api.comments['T-1'] = []
            before = len(api.writes())
            j._batch_run(task, True)
            again = j._batch_run(task, True)
        self.assertEqual(again['summary']['failed'], 1)
        self.assertEqual(len(api.writes()), before)

    def test_assignment_for_new_repair_cycle_is_not_blocked_by_old_success(self):
        api = SimJira()
        task1, task2 = str(self.root / 'cycle1.json'), str(self.root / 'cycle2.json')
        manifest = {'action': 'resolve', 'repo': str(self.root), 'issues': [
            {'key': 'T-1', 'impact': 'x', 'fix_commits': ['abc1234']}]}
        with patch.object(j, '_request', side_effect=api), patch.object(j, '_verify_delivery', return_value={}):
            j._batch_plan(manifest, task1)
            j._batch_run(task1, True)
            api.issues['T-1'] = issue()
            api.issues['T-1']['fields']['updated'] = '2026-10-28T01:00:00Z'
            j._batch_plan(manifest, task2)
            again = j._batch_run(task2, True)
        self.assertEqual(again['summary']['done'], 1)
        self.assertEqual(len(api.writes('/assignee')), 2)

    def test_failed_completed_task_cannot_reassign_on_next_resume(self):
        api = SimJira()
        manifest = {'action': 'resolve', 'repo': str(self.root), 'issues': [
            {'key': 'T-1', 'impact': 'x', 'fix_commits': ['abc1234']}]}
        task = str(self.root / 'task.json')
        with patch.object(j, '_request', side_effect=api), patch.object(j, '_verify_delivery', return_value={}):
            j._batch_plan(manifest, task)
            j._batch_run(task, True)
            api.issues['T-1']['fields']['assignee'] = {'name': 'other-tester'}
            before = len(api.writes())
            j._batch_run(task, True)
            j._batch_run(task, True)
        self.assertEqual(len(api.writes()), before)


class WindowlessTests(Base):
    def test_git_child_uses_no_window_on_windows(self):
        reply = subprocess.CompletedProcess([], 0, stdout='ok', stderr='')
        with patch.object(j.subprocess, 'run', return_value=reply) as child:
            j._git(str(self.root), 'status', '--porcelain')
        expected = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.assertEqual(child.call_args.kwargs.get('creationflags', 0), expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
