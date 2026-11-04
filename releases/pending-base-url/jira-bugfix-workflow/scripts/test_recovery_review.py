"""Regression cases from the independent production-workflow review."""
import copy
import io
import json
from pathlib import Path
from unittest.mock import patch

from test_productivity import Base, SimJira, issue, j


class ReviewedRecoveryTests(Base):
    def test_completed_handoff_rechecks_reporter(self):
        api = SimJira()
        opts = {'impact': 'scope', 'repo': str(self.root), 'fix_commits': ['abc1234']}
        with patch.object(j, '_request', side_effect=api), patch.object(j, '_verify_delivery', return_value={}):
            j._safe_resolve('T-1', opts)
            api.issues['T-1']['fields']['reporter'] = {'name': 'other-qa'}
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts)

    def test_missing_total_is_not_proof_of_complete_collection(self):
        with patch.object(j, '_request', side_effect=SystemExit(1)):
            result = j._read_collection('T-1', 'comment', {'startAt': 0, 'isLast': False, 'values': [{'id': '1'}]})
        self.assertFalse(result['complete'])
        self.assertIsNone(result['total'])

    def test_missing_total_can_follow_explicit_is_last(self):
        page = {'startAt': 1, 'isLast': True, 'values': [{'id': '2'}]}
        with patch.object(j, '_request', return_value=(200, page)) as request:
            result = j._read_collection('T-1', 'comment', {'startAt': 0, 'isLast': False, 'values': [{'id': '1'}]})
        self.assertTrue(result['complete'])
        self.assertIsNone(result['total'])
        self.assertEqual(result['fetched'], 2)
        self.assertEqual(request.call_count, 1)

    def test_missing_total_without_end_marker_stays_partial(self):
        result = j._read_collection('T-1', 'comment', {'startAt': 0, 'comments': [{'id': '1'}]})
        self.assertFalse(result['complete'])
        self.assertIsNone(result['total'])

    def test_resume_never_overwrites_changed_assignee_or_reporter(self):
        for mode in ('standalone', 'batch'):
            for changed in ('assignee', 'reporter'):
                with self.subTest(mode=mode, changed=changed):
                    api = SimJira()
                    rejects = [True]
                    def request(method, path, **kwargs):
                        if method == 'PUT' and path.endswith('/assignee') and rejects[0]:
                            rejects[0] = False
                            raise SystemExit(1)
                        return api(method, path, **kwargs)
                    opts = {'impact': 'scope', 'repo': str(self.root), 'fix_commits': ['abc1234']}
                    task = str(self.root / (mode + changed + '.json'))
                    with patch.dict(j.os.environ, {'JIRA_RUNS_DIR': str(self.root / (mode + changed))}), \
                            patch.object(j, '_request', side_effect=request), patch.object(j, '_verify_delivery', return_value={}):
                        if mode == 'batch':
                            j._batch_plan({'action': 'resolve', 'issues': [dict(opts, key='T-1')]}, task)
                            self.assertEqual(j._batch_run(task, True)['summary']['failed'], 1)
                        else:
                            with self.assertRaises(SystemExit):
                                j._safe_resolve('T-1', opts)
                        api.issues['T-1']['fields'][changed] = {'name': 'colleague'}
                        before = len(api.writes())
                        if mode == 'batch':
                            self.assertEqual(j._batch_run(task, True)['summary']['failed'], 1)
                        else:
                            with self.assertRaises(SystemExit):
                                j._safe_resolve('T-1', opts)
                        self.assertEqual(len(api.writes()), before)

    def test_final_readback_rejects_change_during_comment(self):
        api = SimJira()
        def request(method, path, **kwargs):
            result = api(method, path, **kwargs)
            if method == 'POST' and path.endswith('/comment'):
                api.issues['T-1']['fields']['assignee'] = {'name': 'colleague'}
            return result
        opts = {'impact': 'scope', 'repo': str(self.root), 'fix_commits': ['abc1234'], 'comment': 'handoff'}
        with patch.object(j, '_request', side_effect=request), patch.object(j, '_verify_delivery', return_value={}):
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts)

    def test_resolve_retry_ignores_transport_flags(self):
        api = SimJira()
        rejected = [True]
        def request(method, path, **kwargs):
            if method == 'PUT' and path.endswith('/assignee') and rejected[0]:
                rejected[0] = False
                raise SystemExit(1)  # explicit rejection, safe to retry remaining step
            return api(method, path, **kwargs)
        opts = {'impact': 'scope', 'repo': str(self.root), 'fix_commits': ['abc1234'], 'timeout': 30}
        with patch.object(j, '_request', side_effect=request), patch.object(j, '_verify_delivery', return_value={}):
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts)
            result = j._safe_resolve('T-1', dict(opts, timeout=120, config='other-file', key='1'))
        self.assertEqual(result['status'], 'done')
        self.assertEqual(api.issues['T-1']['fields']['assignee']['name'], 'qa')
        self.assertEqual(len(api.writes('/transitions')), 1)

    def test_resolve_business_changes_cannot_skip_pending_handoff(self):
        api = SimJira()
        api.fail_assign_once = True
        opts = {'impact': 'scope', 'repo': str(self.root), 'fix_commits': ['abc1234']}
        with patch.object(j, '_request', side_effect=api), patch.object(j, '_verify_delivery', return_value={}):
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', opts)
            with self.assertRaises(SystemExit):
                j._safe_resolve('T-1', dict(opts, comment='new note'))
        self.assertEqual(len(api.writes('/transitions')), 1)
        self.assertEqual(len(api.writes('/assignee')), 1)

    def test_standalone_start_unknown_then_updated_change_never_resends(self):
        api = SimJira()
        api.issues['T-1'] = issue(state='Initial')
        pending = [True]
        def request(method, path, **kwargs):
            if method == 'POST' and path.endswith('/transitions') and pending[0]:
                pending[0] = False
                api.calls.append((method, path, kwargs.get('body')))
                raise j.WriteUncertain('request accepted by network, result unknown')
            return api(method, path, **kwargs)
        with patch.object(j, '_request', side_effect=request):
            with self.assertRaises(SystemExit):
                j._safe_start('T-1')
            api.issues['T-1']['fields']['updated'] = 'later-comment'
            with self.assertRaises(SystemExit):
                j._safe_start('T-1')
        self.assertEqual(len(api.writes('/transitions')), 1)

    def test_changed_options_cannot_bypass_uncertain_effect(self):
        verified = [False]
        sends = []
        def send():
            sends.append('sent')
            raise j.WriteUncertain('unknown')
        with patch.object(j, '_current_user', return_value='tester'):
            with self.assertRaises(SystemExit):
                j._effect('T-1', 'start', {'updated': 'one'}, send, lambda: verified[0])
            with self.assertRaises(SystemExit):
                j._effect('T-1', 'start', {'updated': 'two'}, send, lambda: verified[0])
        self.assertEqual(sends, ['sent'])
