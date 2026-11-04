"""Transport security regressions: loopback HTTP only, no credentials or subprocesses."""
import contextlib
import http.client
import http.server
import threading
import unittest
from unittest.mock import patch

from test_productivity import Base, j


@contextlib.contextmanager
def http_origin():
    """A real urllib endpoint that records requests and serves programmable routes."""
    seen = []
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            size = int(self.headers.get('Content-Length', 0))
            seen.append({'path': self.path, 'authorization': self.headers.get('Authorization'),
                         'body': self.rfile.read(size), 'method': self.command})
            status, headers, body = routes.get(self.path, (200, {}, b'attachment'))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_HEAD = do_GET
        do_POST = do_GET
        do_PUT = do_GET
        do_DELETE = do_GET

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen, routes
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class AuthorizationTests(Base):
    def setUp(self):
        super().setUp()
        # Do not allow machine proxy settings to send these fixtures off-host.
        proxy = patch.object(j.urllib.request, 'getproxies', return_value={})
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_absolute_attachment_url_never_receives_jira_credentials(self):
        with http_origin() as (origin, _, _), http_origin() as (cdn, seen, _):
            j.C['base'] = origin
            for token in ('', 'fixture-token'):
                with self.subTest(auth='Bearer' if token else 'Basic'):
                    j.C['token'] = token
                    self.assertEqual(j._request('GET', cdn + '/file', raw=True), (200, b'attachment'))
                    self.assertIsNone(seen[-1]['authorization'])

    def test_redirect_chain_strips_auth_at_the_first_origin_change(self):
        with http_origin() as (origin, source, routes), http_origin() as (cdn, target, cdn_routes):
            j.C.update(base=origin, token='fixture-token')
            for code in (301, 302, 303, 307, 308):
                with self.subTest(status=code):
                    routes['/start'] = (code, {'Location': '/same-origin'}, b'')
                    routes['/same-origin'] = (code, {'Location': cdn + '/hop'}, b'')
                    cdn_routes['/hop'] = (code, {'Location': '/file'}, b'')
                    self.assertEqual(j._request('GET', '/start', raw=True), (200, b'attachment'))
                    self.assertEqual([r['authorization'] for r in source[-2:]],
                                     ['Bearer fixture-token', 'Bearer fixture-token'])
                    self.assertEqual([r['authorization'] for r in target[-2:]], [None, None])

    def test_https_redirect_downgrade_is_blocked_before_second_hop(self):
        # Replace TLS framing only: urlopen, HTTPErrorProcessor and the redirect
        # chain are real urllib, talking exclusively to these loopback servers.
        def https_over_loopback(handler, req):
            self.assertEqual(j.urlparse(req.full_url).hostname, '127.0.0.1')
            return handler.do_open(http.client.HTTPConnection, req)

        with http_origin() as (plain_origin, source, routes), http_origin() as (cdn, target, _):
            origin = plain_origin.replace('http:', 'https:', 1)
            j.C.update(base=origin, token='fixture-token')
            routes['/start'] = (302, {'Location': cdn + '/file'}, b'')
            with patch.object(j.urllib.request.HTTPSHandler, 'https_open', https_over_loopback):
                with self.assertRaises(SystemExit):
                    j._request('GET', '/start', raw=True)
            self.assertEqual(len(source), 1)
            self.assertEqual(target, [])

    def test_https_base_rejects_initial_http_attachment_url(self):
        with http_origin() as (origin, seen, _):
            j.C['base'] = origin.replace('http:', 'https:', 1)
            with self.assertRaises(SystemExit):
                j._request('GET', origin + '/file', raw=True)
            self.assertEqual(seen, [])

    def test_caller_headers_cannot_restore_cross_origin_authorization(self):
        with http_origin() as (origin, _, _), http_origin() as (cdn, seen, _):
            j.C['base'] = origin
            j._request('GET', cdn + '/file', raw=True, headers={'authorization': 'Bearer fixture-token'})
            self.assertIsNone(seen[-1]['authorization'])

    def test_origin_comparison_uses_scheme_host_and_exact_effective_port(self):
        self.assertEqual(j._http_origin('https://EXAMPLE.invalid/path'),
                         j._http_origin('https://example.invalid:443/elsewhere'))
        self.assertNotEqual(j._http_origin('https://example.invalid'),
                            j._http_origin('http://example.invalid:443'))
        self.assertNotEqual(j._http_origin('https://example.invalid'),
                            j._http_origin('https://example.invalid:0'))

    def test_attachment_hash_download_uses_unauthenticated_cdn(self):
        with http_origin() as (origin, _, _), http_origin() as (cdn, seen, _):
            j.C['base'] = origin
            attachment = {'filename': 'shot.png', 'size': len(b'attachment'), 'content': cdn + '/file'}
            with patch.object(j, '_issue_fields', return_value={'attachment': [attachment]}):
                self.assertEqual(j._attachment_match('T-1', 'shot.png', b'attachment'), attachment)
            self.assertEqual(len(seen), 1)
            self.assertIsNone(seen[0]['authorization'])

    def test_temporary_opener_is_restored_on_success_and_failure(self):
        previous = j.urllib.request._opener
        with http_origin() as (origin, _, _):
            j.C['base'] = origin
            j._request('GET', '/file', raw=True)
        self.assertIs(j.urllib.request._opener, previous)
        with patch.object(j.urllib.request, 'urlopen', side_effect=OSError('reset')), \
                self.assertRaises(j.WriteUncertain):
            j._request('POST', '/write', body={})
        self.assertIs(j.urllib.request._opener, previous)


class WriteOutcomeTests(Base):
    def test_ambiguous_http_statuses_are_uncertain_without_retries(self):
        import io
        # Include every 5xx, timeout/rate-limit responses, an unknown 4xx and
        # unresolved redirects: none proves that the original write was rejected.
        for code in (*range(500, 600), 408, 425, 429, 499, 301, 302, 307, 308):
            for method in ('POST', 'PUT', 'DELETE'):
                with self.subTest(code=code, method=method):
                    error = j.urllib.error.HTTPError(j.C['base'], code, 'unclear', {}, io.BytesIO(b'{}'))
                    with contextlib.redirect_stderr(io.StringIO()), error, \
                            patch.object(j.urllib.request, 'urlopen', side_effect=error) as send:
                        try:
                            j._request(method, '/rest/api/2/issue/T-1', body={})
                        except SystemExit as exc:
                            self.assertIsInstance(exc, j.WriteUncertain)
                        else:
                            self.fail('ambiguous write was accepted')
                        self.assertEqual(send.call_count, 1)

    def test_real_http_520_and_408_are_not_retried(self):
        with http_origin() as (origin, seen, routes), \
                patch.object(j.urllib.request, 'getproxies', return_value={}):
            j.C['base'] = origin
            for code in (520, 408):
                with self.subTest(code=code):
                    routes['/write'] = (code, {}, b'{}')
                    before = len(seen)
                    try:
                        j._request('POST', '/write', body={'body': 'one write'})
                    except SystemExit as exc:
                        self.assertIsInstance(exc, j.WriteUncertain)
                    else:
                        self.fail('ambiguous write was accepted')
                    self.assertEqual(len(seen), before + 1)

    def test_real_write_redirects_remain_uncertain_without_following(self):
        with http_origin() as (origin, seen, routes), \
                patch.object(j.urllib.request, 'getproxies', return_value={}):
            j.C['base'] = origin
            for code in (301, 302, 303, 307, 308):
                with self.subTest(code=code):
                    routes['/write'] = (code, {'Location': '/done'}, b'')
                    before = len(seen)
                    try:
                        j._request('POST', '/write', body={'body': 'one write'})
                    except SystemExit as exc:
                        self.assertIsInstance(exc, j.WriteUncertain)
                    else:
                        self.fail('redirect does not prove the original write succeeded')
                    self.assertEqual(len(seen), before + 1)

    def test_explicit_rejections_stay_rejected_without_retries(self):
        import io
        for code in (400, 401, 403, 404, 405, 409, 410, 413, 414, 415, 422):
            with self.subTest(code=code):
                error = j.urllib.error.HTTPError(j.C['base'], code, 'rejected', {}, io.BytesIO(b'{}'))
                with contextlib.redirect_stderr(io.StringIO()), error, \
                        patch.object(j.urllib.request, 'urlopen', side_effect=error) as send:
                    with self.assertRaises(SystemExit) as caught:
                        j._request('POST', '/write', body={})
                    self.assertNotIsInstance(caught.exception, j.WriteUncertain)
                    self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
