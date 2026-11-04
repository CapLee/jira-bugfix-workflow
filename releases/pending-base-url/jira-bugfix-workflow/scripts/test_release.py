"""Portable distribution and fail-closed TLS regression tests (offline)."""
import contextlib
import io
import os
from pathlib import Path
from unittest.mock import patch
from test_productivity import Base, j


class ReleaseTests(Base):
    def test_certificate_failure_does_not_disable_validation(self):
        j.C['base'] = 'https://mock.invalid'
        for method in ('GET', 'POST'):
            with self.subTest(method=method):
                error = j.ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED')
                with patch.object(j, '_http_open', side_effect=error) as send, \
                        patch.object(j.time, 'sleep'), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    j._request(method, '/rest/api/2/myself')
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.kwargs['context'].verify_mode, j.ssl.CERT_REQUIRED)

    def test_default_state_is_bundled_not_another_agent_profile(self):
        base = Path(j.__file__).resolve().parent.parent
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Path(j._default_creds_path()), base / 'config' / 'jira-api-creds.yaml')
            self.assertEqual(Path(j._profiles_dir()), base / 'config' / 'profiles')
            self.assertEqual(Path(j._project_cfg_dir()), base / 'config' / 'projects')
            self.assertEqual(Path(j._runs_dir()), base / 'data' / 'runs')

    def test_explicit_insecure_setting_has_warning(self):
        j.C.update(base='https://mock.invalid', insecure=True)
        class Reply:
            status = 200
            def read(self): return b'{}'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        output = io.StringIO()
        with patch.object(j, '_http_open', return_value=Reply()) as send, contextlib.redirect_stderr(output):
            j._request('GET', '/rest/api/2/myself')
        self.assertEqual(send.call_args.kwargs['context'].verify_mode, j.ssl.CERT_NONE)
        self.assertIn('显式', output.getvalue())
        self.assertIn('证书', output.getvalue())
