"""Default JIRA address regressions; no real network or user credentials."""
import argparse
import contextlib
import io
from unittest.mock import patch

from test_productivity import Base, j


class DefaultBaseUrlTests(Base):
    DEFAULT = 'https://ticket.quectel.com'

    def config(self, suffix, base=None):
        path = self.root / (suffix + '.yaml')
        text = 'username: fixture-user\npassword: fixture-only\nprojects: T\nactive_project: T\n'
        if base is not None:
            text += 'base_url: ' + base + '\n'
        path.write_text(text, encoding='utf-8')
        return path

    def test_loading_missing_or_empty_base_uses_default(self):
        for i, base in enumerate((None, '')):
            with self.subTest(base=base):
                self.assertEqual(j.load_creds(str(self.config(str(i), base)))['base'], self.DEFAULT)

    def test_loading_explicit_base_preserves_it(self):
        self.assertEqual(j.load_creds(str(self.config('other', 'https://other.invalid/jira')))['base'],
                         'https://other.invalid/jira')

    def initialize(self, path, base=None, interactive=False):
        args = argparse.Namespace(check=False, base_url=base, username='fixture-user',
                                  password='fixture-only', token=None, timeout=None,
                                  insecure=False, projects=None, use=None)
        with patch.object(j, 'resolve_creds_path', return_value=str(path)), \
                patch.object(j.sys.stdin, 'isatty', return_value=interactive), \
                patch.object(j, '_prompt', side_effect=AssertionError('unexpected address prompt')), \
                patch.object(j, '_request', return_value=(200, {'name': 'fixture-user'})), \
                patch.object(j, '_init_projects'), contextlib.redirect_stdout(io.StringIO()):
            j.cmd_init(args)
        return j._read_flat(str(path))

    def test_init_defaults_without_address_prompt(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                path = self.root / ('fresh-' + str(interactive) + '.yaml')
                self.assertEqual(self.initialize(path, interactive=interactive)['base_url'], self.DEFAULT)

    def test_init_existing_address_wins_default(self):
        path = self.config('existing', 'https://existing.invalid/jira')
        self.assertEqual(self.initialize(path)['base_url'], 'https://existing.invalid/jira')

    def test_init_explicit_argument_wins_existing_address(self):
        path = self.config('override', 'https://existing.invalid/jira')
        self.assertEqual(self.initialize(path, 'https://chosen.invalid/jira')['base_url'],
                         'https://chosen.invalid/jira')

    def test_init_check_defaults_without_rewriting_config(self):
        path = self.config('check')
        before = path.read_bytes()
        output = io.StringIO()
        with patch.object(j, '_request', return_value=(200, {'name': 'fixture-user', 'displayName': 'Fixture'})), \
                patch.object(j, '_project_cfg_dir', return_value=str(self.root / 'projects')), \
                contextlib.redirect_stdout(output):
            j._init_check(str(path))
        self.assertEqual(j.C['base'], self.DEFAULT)
        self.assertIn(self.DEFAULT, output.getvalue())
        self.assertEqual(path.read_bytes(), before)
