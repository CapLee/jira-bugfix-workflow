"""Direct login regressions: local dialogs and loopback JIRA, no live credentials."""
import argparse
import base64
import contextlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from test_productivity import Base, j


class DirectLoginTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.root / 'jira-api-creds.yaml'
        self.status = 200
        self.user = 'fixture-user'
        self.seen = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.seen.append((self.path, self.headers.get('Authorization')))
                if self.path.startswith('/rest/api/2/myself'):
                    data = {'name': owner.user, 'displayName': 'Fixture User'}
                elif self.path == '/rest/api/2/project':
                    data = [{'key': 'T', 'name': 'Fixture Project'}]
                else:
                    data = {'total': 0, 'issues': []}
                body = json.dumps(data).encode('utf-8')
                self.send_response(owner.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def args(self, **changes):
        values = dict(config=str(self.path), profile=None, check=False, dialog=True,
                      base_url=self.base, username=None, password=None, token=None,
                      timeout=5, insecure=False, projects=None, use=None)
        values.update(changes)
        return argparse.Namespace(**values)

    def initialize(self, credentials):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(j, '_credentials_dialog', return_value=credentials, create=True) as dialog, \
                patch.object(j.sys.stdin, 'isatty', return_value=False), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            j.cmd_init(self.args())
        return dialog, output.getvalue() + errors.getvalue()

    def test_dialog_verifies_and_saves_basic_credentials(self):
        dialog, output = self.initialize(('fixture-user', 'fixture-password'))
        dialog.assert_called_once()
        config = j._read_flat(str(self.path))
        self.assertEqual(config['username'], 'fixture-user')
        self.assertEqual(config['password'], 'fixture-password')
        self.assertEqual(config['base_url'], self.base)
        self.assertFalse(config.get('token'))
        self.assertNotIn('fixture-password', output)
        expected = 'Basic ' + base64.b64encode(b'fixture-user:fixture-password').decode()
        self.assertTrue(self.seen)
        self.assertTrue(all(auth == expected for _, auth in self.seen))

    def test_special_password_round_trips_without_normalization(self):
        for password in ('  fixture # : "quoted" \\ tail  ', '#fixture', '<fixture>',
                         '"fixture"', "'fixture'", 'fixture\\nvalue', 'fixture\nvalue'):
            with self.subTest(style=repr(password)):
                self.initialize(('fixture-user', password))
                self.assertEqual(j._read_flat(str(self.path))['password'], password)

    def test_cancel_keeps_existing_config_and_sends_no_request(self):
        self.path.write_text('username: old-user\npassword: old-password\nactive_project: T\n', encoding='utf-8')
        before = self.path.read_bytes()
        with self.assertRaises(SystemExit):
            self.initialize(None)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.seen, [])

    def test_rejected_credentials_never_overwrite_existing_config(self):
        self.path.write_text('username: old-user\npassword: old-password\nactive_project: T\n', encoding='utf-8')
        before = self.path.read_bytes()
        previous = j.C
        self.status = 401
        with self.assertRaises(SystemExit):
            self.initialize(('fixture-user', 'fixture-password'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIs(j.C, previous)

    def test_wrong_identity_does_not_create_config(self):
        self.user = 'different-user'
        with self.assertRaises(SystemExit):
            self.initialize(('fixture-user', 'fixture-password'))
        self.assertFalse(self.path.exists())

    def test_dialog_replaces_bearer_token_with_basic_auth(self):
        self.path.write_text('username: old-user\npassword: old-password\ntoken: old-token\n', encoding='utf-8')
        self.initialize(('fixture-user', 'fixture-password'))
        self.assertFalse(j.load_creds(str(self.path))['token'])
        self.assertTrue(all(auth.startswith('Basic ') for _, auth in self.seen))

    def test_dialog_keeps_project_list_and_active_project(self):
        self.path.write_text('# project note\nprojects: T,BPM\nactive_project: BPM\n'
                             'default_jql: project = {project} AND resolution = Unresolved\n', encoding='utf-8')
        self.initialize(('fixture-user', 'fixture-password'))
        config = j._read_flat(str(self.path))
        self.assertEqual(config['projects'], 'T,BPM')
        self.assertEqual(config['active_project'], 'BPM')
        self.assertIn('# project note', self.path.read_text(encoding='utf-8'))
        self.assertEqual(config['default_jql'], 'project = {project} AND resolution = Unresolved')

    def test_noninteractive_init_without_credentials_uses_dialog(self):
        with patch.object(j, '_credentials_dialog', return_value=('fixture-user', 'fixture-password')) as dialog, \
                patch.object(j.sys.stdin, 'isatty', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            j.cmd_init(self.args(dialog=False))
        dialog.assert_called_once()
        self.assertEqual(j.load_creds(str(self.path))['user'], 'fixture-user')

    def test_saved_credentials_do_not_open_another_dialog(self):
        self.initialize(('fixture-user', 'fixture-password'))
        with patch.object(j, '_credentials_dialog', side_effect=AssertionError('repeat dialog')), \
                contextlib.redirect_stdout(io.StringIO()):
            j.C = j.load_creds(str(self.path))
            j.cmd_whoami(argparse.Namespace())
            j.cmd_projects(argparse.Namespace(query=None))
        self.assertTrue(any(path == '/rest/api/2/project' for path, _ in self.seen))

    def test_missing_config_cannot_fall_back_to_plugin_environment(self):
        with patch.dict(j.os.environ, {'JIRA_TOKEN': 'expired-fixture', 'JIRA_URL': 'https://other.invalid',
                                      'JIRA_TOKEN_STORE': str(self.root / 'never-read.json')}), \
                contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            j.load_creds(str(self.path))
        self.assertIn('init --dialog', errors.getvalue())
        self.assertNotIn('插件', errors.getvalue())
        self.assertEqual(self.seen, [])

    def test_parallel_config_change_is_not_overwritten(self):
        def prompt(*args):
            self.path.write_text('username: parallel-user\npassword: parallel-password\n', encoding='utf-8')
            return 'fixture-user', 'fixture-password'
        with patch.object(j, '_credentials_dialog', side_effect=prompt), \
                patch.object(j.sys.stdin, 'isatty', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            j.cmd_init(self.args())
        self.assertEqual(j._read_flat(str(self.path))['username'], 'parallel-user')

    def test_dialog_does_not_prompt_for_projects_in_a_terminal(self):
        with patch.object(j, '_credentials_dialog', return_value=('fixture-user', 'fixture-password')), \
                patch.object(j.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=AssertionError('unexpected terminal prompt')), \
                contextlib.redirect_stdout(io.StringIO()):
            j.cmd_init(self.args())
        self.assertFalse(j._read_flat(str(self.path)).get('active_project'))

    def test_dialog_ignores_account_and_password_in_remote_errors(self):
        self.status = 401
        self.user = 'fixture-password'
        output = io.StringIO()
        with patch.object(j, '_credentials_dialog', return_value=('fixture-user', 'fixture-password')), \
                patch.object(j.sys.stdin, 'isatty', return_value=False), \
                patch.object(j, '_request', side_effect=lambda *a, **k: j.die('server echoed fixture-password')), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                self.assertRaises(SystemExit):
            j.cmd_init(self.args())
        self.assertNotIn('fixture-password', output.getvalue())
        self.assertFalse(self.path.exists())

    def test_no_plugin_code_remains_in_runtime(self):
        import inspect
        runtime = inspect.getsource(j)
        self.assertNotIn('quectel-cli', runtime)
        self.assertNotIn('_plugin_credentials', runtime)
        self.assertNotIn('+token-create', runtime)
