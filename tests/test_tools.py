"""Isolated tool contract tests; never send real external messages."""
import importlib.util
from pathlib import Path
import unittest
import os
import tempfile
from unittest.mock import patch, MagicMock

MODULE = Path(__file__).resolve().parents[1] / 'jarvis_tools.py'


class ToolTests(unittest.TestCase):
    def tools(self):
        self.assertTrue(MODULE.exists(), 'jarvis_tools.py must exist')
        import jarvis_tools
        return jarvis_tools

    def test_unknown_tools_are_refused_and_risky(self):
        tools = self.tools()
        self.assertEqual(tools.execute_tool('shell', {}), {'success': False, 'error': 'Unknown tool.'})
        self.assertTrue(tools.tool_risk('shell'))

    def test_current_time_schema_and_validation(self):
        tools = self.tools()
        result = tools.execute_tool('current_time', {})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['timezone'], 'Asia/Kolkata')
        self.assertTrue(result['datetime'].endswith('+05:30'))
        self.assertFalse(tools.tool_risk('current_time'))
        for args in [None, [], {'timezone': 12}, {'extra': 1}, {'timezone': 'Not/AZone'}]:
            self.assertFalse(tools.execute_tool('current_time', args)['success'])
        schema = next(t['function'] for t in tools.TOOLS if t['function']['name'] == 'current_time')
        self.assertFalse(schema['parameters']['additionalProperties'])
        self.assertEqual(schema['parameters']['properties']['timezone']['default'], 'Asia/Kolkata')

    def test_workspace_write_read_and_list_are_confined(self):
        tools = self.tools()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp) / 'workspace'
            with patch.dict(os.environ, {'JARVIS_WORKSPACE_DIR': str(root)}):
                result = tools.execute_tool('write_file', {'path': 'notes/a.txt', 'content': 'hello \u2603'})
                self.assertTrue(result['success'], result)
                self.assertEqual((root / 'notes/a.txt').read_text('utf-8'), 'hello \u2603')
                self.assertEqual(tools.execute_tool('read_file', {'path': 'notes/a.txt'})['content'], 'hello \u2603')
                self.assertEqual(tools.execute_tool('list_files', {'path': 'notes'})['files'][0]['path'], 'notes/a.txt')
                for path in ['../outside', '/etc/passwd', 'C:\\Windows\\win.ini', '.env', '.ssh/key', 'notes/../.hermes/token', 'file:stream', 'NUL', 'notes/.env.local']:
                    for name in ['read_file', 'list_files', 'write_file']:
                        args = {'path': path, **({'content': 'x'} if name == 'write_file' else {})}
                        self.assertFalse(tools.execute_tool(name, args)['success'], (name, path))
                self.assertFalse(tools.execute_tool('write_file', {'path': 'large.txt', 'content': 'x' * 100001})['success'])
                self.assertFalse(tools.execute_tool('write_file', {'path': 'notes/a.txt', 'content': 'overwrite'})['success'])
                (root / 'huge.txt').write_bytes(b'x' * 100001)
                self.assertFalse(tools.execute_tool('read_file', {'path': 'huge.txt'})['success'])
                outside = Path(tmp) / 'outside.txt'
                outside.write_text('private')
                try:
                    (root / 'link.txt').symlink_to(outside)
                except OSError:
                    # Windows directory junction needs no symlink privilege.
                    import subprocess
                    external = Path(tmp) / 'external'
                    external.mkdir()
                    (external / 'secret.txt').write_text('private')
                    junction = root / 'junction'
                    p = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(external)], capture_output=True)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertFalse(tools.execute_tool('read_file', {'path': 'junction/secret.txt'})['success'])
                    junction.rmdir()
                else:
                    self.assertFalse(tools.execute_tool('read_file', {'path': 'link.txt'})['success'])
        self.assertTrue(tools.tool_risk('write_file'))
        self.assertFalse(tools.tool_risk('read_file'))
        self.assertFalse(tools.tool_risk('list_files'))

    def test_diagnostic_command_is_real_allowlisted_and_bounded(self):
        tools = self.tools()
        result = tools.execute_tool('run_command', {'command': 'python --version'})
        self.assertTrue(result['success'], result)
        self.assertIn('Python ', result['stdout'])
        self.assertIn('server', result['location'])
        self.assertTrue(tools.tool_risk('run_command'))
        for command in ['python -c print(1)', 'whoami && dir', 'hostname; whoami', 'dir']:
            self.assertFalse(tools.execute_tool('run_command', {'command': command})['success'])

    def test_open_app_uses_windows_allowlist(self):
        tools = self.tools()
        # Never open an actual GUI during tests.
        with patch('subprocess.Popen') as launch, patch('sys.platform', 'win32'):
            launch.return_value.pid = 123
            result = tools.execute_tool('open_app', {'app': 'notepad'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['location'], 'server VM, not your browser device')
            self.assertTrue(str(launch.call_args.args[0][0]).lower().endswith('notepad.exe'))
            self.assertFalse(launch.call_args.kwargs['shell'])
            launch.reset_mock()
            for app in ['cmd', 'powershell', 'notepad & calc', 'C:/evil.exe']:
                self.assertFalse(tools.execute_tool('open_app', {'app': app})['success'])
            launch.assert_not_called()
        self.assertTrue(tools.tool_risk('open_app'))

    def test_web_search_uses_configured_searxng_json(self):
        tools = self.tools()
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"results":[{"title":"Python documentation","url":"https://docs.python.org/3/","content":"<b>Actual &amp; evidence</b><script>evil()</script>","publishedDate":"2026-10-21T09:00:00Z"}]}'
        response.__enter__.return_value = response
        with patch.dict(os.environ, {'JARVIS_SEARCH_URL': 'http://127.0.0.1:8888', 'SEARXNG_URL': 'http://unused.invalid'}), patch('urllib.request.urlopen', return_value=response) as request:
            result = tools.execute_tool('web_search', {'query': 'hello & world'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['source'], 'SearXNG')
            self.assertEqual(result['results'], [{'title': 'Python documentation', 'url': 'https://docs.python.org/3/', 'snippet': 'Actual & evidence', 'publishedDate': '2026-10-21T09:00:00Z'}])
            self.assertEqual(request.call_args.args[0].full_url, 'http://127.0.0.1:8888/search?q=hello+%26+world&format=json')
            self.assertLessEqual(request.call_args.kwargs['timeout'], 20)
            response.read.assert_called_once_with(1000001)

    def test_web_search_filters_deduplicates_and_bounds_searxng_results(self):
        import json
        tools = self.tools()
        rows = [None, {'title': 'Bad', 'url': 'javascript:alert(1)'},
                {'title': 123, 'url': 'https://example.com/bad'},
                {'title': 'Missing URL'}, {'title': 'Blank', 'url': 'https://'},
                {'title': 'Oversize', 'url': 'https://example.com/' + 'x' * 2000},
                {'title': 'Credentials', 'url': 'https://user:password@example.com/'},
                {'title': '  ', 'url': 'https://example.com/empty'}]
        rows += [{'title': 'T' * 600, 'url': 'https://example.com/' + str(i)} for i in range(8)]
        rows.insert(10, rows[8])
        with patch.dict(os.environ, {'JARVIS_SEARCH_URL': '', 'SEARXNG_URL': 'http://127.0.0.1:8888/search/'}), patch.object(tools, '_http', return_value=json.dumps({'results': rows})) as request:
            result = tools.execute_tool('web_search', {'query': 'test'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['results'], [{'title': 'T' * 500, 'url': 'https://example.com/' + str(i)} for i in range(5)])
            self.assertEqual(request.call_args.args[0], 'http://127.0.0.1:8888/search?q=test&format=json')

    def test_web_search_rejects_invalid_service_responses_without_fallback(self):
        tools = self.tools()
        with patch.dict(os.environ, {'JARVIS_SEARCH_URL': 'http://127.0.0.1:8888'}), patch.object(tools, '_http') as request:
            for body in ['{"results":"captcha"}', '{"results":{}}', '{"results":[],"error":"secret"}', '{"results":[],"unresponsive_engines":[["google","timeout"]]}', '<html>captcha</html>', '{"results":[{"title":"Bad","url":"javascript:bad"}]}']:
                with self.subTest(body=body):
                    request.reset_mock()
                    request.return_value = body
                    result = tools.execute_tool('web_search', {'query': 'test'})
                    self.assertFalse(result['success'], result)
                    self.assertNotIn('secret', str(result))
                    request.assert_called_once()
            request.return_value = '{"results":[]}'
            self.assertEqual(tools.execute_tool('web_search', {'query': 'test'}), {'success': True, 'results': [], 'source': 'SearXNG'})
            request.side_effect = TimeoutError('secret endpoint details')
            result = tools.execute_tool('web_search', {'query': 'test'})
            self.assertFalse(result['success'])
            self.assertNotIn('secret', str(result))

    def test_web_search_endpoint_is_admin_only_http_configuration(self):
        tools = self.tools()
        with patch.object(tools, '_http') as request:
            for endpoint in ['file:///private', 'ftp://example.com', 'http://', 'http://user:secret@example.com', 'http://example.com/?other=1', 'http://example.com/#fragment']:
                with patch.dict(os.environ, {'JARVIS_SEARCH_URL': endpoint}):
                    self.assertFalse(tools.execute_tool('web_search', {'query': 'test'})['success'])
            self.assertFalse(tools.execute_tool('web_search', {'query': 'test', 'url': 'http://example.com'})['success'])
            request.assert_not_called()

    def test_web_search_parses_real_html_format_and_handles_errors(self):
        tools = self.tools()
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx">Example &amp; title</a><a class="result__a" href="javascript:bad">Bad</a>'
        response.__enter__.return_value = response
        with patch.dict(os.environ, {'JARVIS_SEARCH_URL': '', 'SEARXNG_URL': ''}), patch('urllib.request.urlopen', return_value=response) as request:
            result = tools.execute_tool('web_search', {'query': 'hello & world'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['results'], [{'title': 'Example & title', 'url': 'https://example.com/x'}])
            self.assertIn('q=hello+%26+world', request.call_args.args[0].full_url)
            self.assertLessEqual(request.call_args.kwargs['timeout'], 20)
            response.status = 429
            self.assertFalse(tools.execute_tool('web_search', {'query': 'test'})['success'])
            response.status = 202
            self.assertFalse(tools.execute_tool('web_search', {'query': 'test'})['success'])
            response.status = 200
            response.read.return_value = b'<html>captcha challenge</html>'
            self.assertFalse(tools.execute_tool('web_search', {'query': 'test'})['success'])
        self.assertTrue(tools.tool_risk('web_search'))

    def test_web_search_falls_back_to_duckduckgo_lite_when_html_endpoint_is_challenged(self):
        # html.duckduckgo.com answers bots with HTTP 202 + an anomaly page; installed copies have
        # no SearXNG, so research must still work through the lite endpoint.
        tools = self.tools()
        challenged, lite = MagicMock(), MagicMock()
        challenged.status = 202
        challenged.read.return_value = b'<html>anomaly</html>'
        lite.status = 200
        lite.read.return_value = (b"<a rel=\"nofollow\" href=\"//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.goodreturns.in%2Fgold-rates%2F&amp;rut=x\" "
                                  b"class='result-link'>Gold Rate Today</a><td class='result-snippet'>Gold <b>rate</b> today</td>")
        for r in (challenged, lite):
            r.__enter__.return_value = r
        with patch.dict(os.environ, {'JARVIS_SEARCH_URL': '', 'SEARXNG_URL': ''}), \
             patch('urllib.request.urlopen', side_effect=[challenged, lite]) as request:
            result = tools.execute_tool('web_search', {'query': 'gold price'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['results'][0]['url'], 'https://www.goodreturns.in/gold-rates/')
        self.assertEqual(result['results'][0]['title'], 'Gold Rate Today')
        self.assertIn('lite.duckduckgo.com', request.call_args.args[0].full_url)

    def test_public_page_fetches_evidence_over_pinned_https(self):
        import io
        import socket
        from datetime import datetime, timezone
        from email.message import Message
        tools = self.tools()
        body = b'<html><head><style>bad css</style></head><body><nav>Menu</nav><h1>Gold &amp; rates</h1><p>22K INR 100 per gram</p><script>execute_me()</script><p>Source date 2026-10-21</p></body></html>'
        response = MagicMock()
        response.status = 200
        response.headers = Message()
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.read1.side_effect = io.BytesIO(body).read
        address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))
        with patch('socket.getaddrinfo', return_value=[address]) as dns, patch('socket.socket') as factory, patch('ssl.create_default_context') as tls, patch('http.client.HTTPSConnection.getresponse', return_value=response):
            sock = factory.return_value
            tls.return_value.wrap_socket.return_value = sock
            before = datetime.now(timezone.utc)
            result = tools.execute_tool('read_public_page', {'url': 'https://example.com/rates?q=gold#rates'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['url'], 'https://example.com/rates?q=gold')
            self.assertEqual(result['text'], 'Gold & rates\n22K INR 100 per gram\nSource date 2026-10-21')
            self.assertFalse(result['truncated'])
            self.assertGreaterEqual(datetime.fromisoformat(result['fetched_at']), before)
            self.assertTrue(result['untrusted'])
            dns.assert_called_once()
            sock.connect.assert_called_once_with(('93.184.216.34', 443))
            tls.return_value.wrap_socket.assert_called_once_with(sock, server_hostname='example.com')
            self.assertIn(b'Host: example.com', sock.sendall.call_args.args[0])
            self.assertNotIn(b'Authorization:', sock.sendall.call_args.args[0])
            self.assertNotIn(b'Cookie:', sock.sendall.call_args.args[0])
            sock.close.assert_called()
        for name in ['read_public_page', 'web_search', 'weather']:
            self.assertTrue(tools.tool_risk(name))

    def public_response(self, body=b'evidence', status=200, content_type='text/plain', location=None):
        import io
        from email.message import Message
        response = MagicMock()
        response.status = status
        response.headers = Message()
        response.headers['Content-Type'] = content_type
        if location is not None:
            response.headers['Location'] = location
        response.read1.side_effect = io.BytesIO(body).read
        return response

    def public_address(self, ip='93.184.216.34'):
        import socket
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443, 0, 0) if ':' in ip else (ip, 443))

    def test_public_page_redirects_revalidate_and_report_final_source(self):
        tools = self.tools()
        responses = [self.public_response(status=302, location='/actual'), self.public_response(b'actual evidence')]
        with patch('socket.getaddrinfo', return_value=[self.public_address()]) as dns, patch.object(tools, '_PinnedHTTPSConnection') as factory:
            factory.return_value.getresponse.side_effect = responses
            result = tools.execute_tool('read_public_page', {'url': 'https://example.com/start'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['url'], 'https://example.com/actual')
            self.assertEqual(result['text'], 'actual evidence')
            self.assertEqual(dns.call_count, 2)
            self.assertEqual(factory.return_value.close.call_count, 2)

    def test_public_page_enforces_byte_mime_and_text_limits(self):
        tools = self.tools()
        with patch('socket.getaddrinfo', return_value=[self.public_address()]), patch.object(tools, '_PinnedHTTPSConnection') as factory:
            for body, mime in [(b'x' * 1000001, 'text/plain'), (b'PDF', 'application/pdf'), (b'<p>x</p>', 'application/octet-stream')]:
                factory.return_value.getresponse.return_value = self.public_response(body, content_type=mime)
                self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'], mime)
            for body, mime in [(b'x' * 13000, 'text/plain'), (b'{"rate":123}', 'application/json')]:
                factory.return_value.getresponse.return_value = self.public_response(body, content_type=mime)
                result = tools.execute_tool('read_public_page', {'url': 'https://example.com/'})
                self.assertTrue(result['success'], result)
                self.assertEqual(result['text'], body.decode()[:12000])
                self.assertEqual(result['truncated'], len(body) > 12000)
            oversized = self.public_response()
            oversized.headers['Content-Length'] = '1000001'
            factory.return_value.getresponse.return_value = oversized
            self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])
            oversized.read1.assert_not_called()
            compressed = self.public_response()
            compressed.headers['Content-Encoding'] = 'gzip'
            factory.return_value.getresponse.return_value = compressed
            self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])

    def test_public_page_refuses_bot_challenges_explicitly(self):
        tools = self.tools()
        with patch('socket.getaddrinfo', return_value=[self.public_address()]), patch.object(tools, '_PinnedHTTPSConnection') as factory:
            for response in [self.public_response(b'<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/test"></script>', content_type='text/html'), self.public_response(b'Please verify you are human'), self.public_response(status=403), self.public_response(status=429)]:
                factory.return_value.getresponse.return_value = response
                result = tools.execute_tool('read_public_page', {'url': 'https://example.com/'})
                self.assertFalse(result['success'], result)
                self.assertIn('blocked', result['error'].lower())
                self.assertNotIn('text', result)

    def test_public_page_rejects_ssrf_before_any_connection(self):
        tools = self.tools()
        urls = ['http://example.com/', 'https://127.0.0.1/', 'https://169.254.169.254/latest/meta-data',
                'https://[::1]/', 'https://[fe80::1%25eth0]/', 'https://[2606:4700:4700::1111]/',
                'https://8.8.8.8/', 'https://localhost/', 'https://printer.local/', 'https://a.internal/',
                'https://example.com:8443/', 'https://user:secret@example.com/', 'https://@example.com/',
                'file:///etc/passwd', 'https://example.com/\r\nX-Header: bad', 'https://example.com\\@localhost/']
        with patch('socket.getaddrinfo') as dns, patch.object(tools, '_PinnedHTTPSConnection') as connection:
            for url in urls:
                with self.subTest(url=url):
                    self.assertFalse(tools.execute_tool('read_public_page', {'url': url})['success'])
            dns.assert_not_called()
            connection.assert_not_called()
        denied = ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.0.1', '169.254.169.254', '0.0.0.0',
                  '100.64.0.1', '224.0.0.1', '240.0.0.1', '192.0.2.1', '::1', '::', 'fe80::1', 'fc00::1',
                  'ff02::1', '2001:db8::1', '::ffff:127.0.0.1', '2002:7f00:1::']
        with patch('socket.getaddrinfo') as dns, patch.object(tools, '_PinnedHTTPSConnection') as connection:
            for ip in denied:
                dns.return_value = [self.public_address(), self.public_address(ip)]
                self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://mixed.example.com/'})['success'], ip)
            connection.assert_not_called()

    def test_public_page_redirect_ssrf_rebinding_and_limit(self):
        tools = self.tools()
        with patch('socket.getaddrinfo', return_value=[self.public_address()]) as dns, patch.object(tools, '_PinnedHTTPSConnection') as factory:
            for location in ['https://127.0.0.1/', 'https://[::1]/', 'http://example.com/', 'https://user:secret@example.com/']:
                factory.reset_mock()
                factory.return_value.getresponse.return_value = self.public_response(status=302, location=location)
                self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])
                factory.assert_called_once()
            factory.reset_mock()
            factory.return_value.getresponse.return_value = self.public_response(status=302, location='/loop')
            self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])
            self.assertEqual(factory.call_count, 4)
            factory.reset_mock()
            dns.side_effect = [[self.public_address()], [self.public_address('127.0.0.1')]]
            self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])
            factory.assert_called_once()
        # A resolver that rebinds on its second call must never be consulted
        # by the actual TLS connection: only the previously validated IP is used.
        with patch('socket.getaddrinfo', side_effect=[[self.public_address()], [self.public_address('127.0.0.1')]]) as dns, patch('socket.socket') as factory, patch('ssl.create_default_context') as tls, patch('http.client.HTTPSConnection.getresponse', return_value=self.public_response()):
            tls.return_value.wrap_socket.return_value = factory.return_value
            result = tools.execute_tool('read_public_page', {'url': 'https://example.com/'})
            self.assertTrue(result['success'], result)
            dns.assert_called_once()
            factory.return_value.connect.assert_called_once_with(('93.184.216.34', 443))

    def test_public_page_retries_transient_transport_once_and_times_out(self):
        tools = self.tools()
        with patch('socket.getaddrinfo', return_value=[self.public_address()]) as dns, patch.object(tools, '_PinnedHTTPSConnection') as factory:
            factory.return_value.getresponse.side_effect = [TimeoutError('private'), self.public_response()]
            result = tools.execute_tool('read_public_page', {'url': 'https://example.com/'})
            self.assertTrue(result['success'], result)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(dns.call_count, 1)  # Retry only already-validated IPs.
            factory.reset_mock()
            factory.return_value.getresponse.side_effect = TimeoutError('private')
            result = tools.execute_tool('read_public_page', {'url': 'https://example.com/'})
            self.assertFalse(result['success'])
            self.assertNotIn('private', str(result))
            self.assertEqual(factory.call_count, 2)
        with patch('socket.getaddrinfo', return_value=[self.public_address()]), patch.object(tools, '_PinnedHTTPSConnection') as factory, patch.object(tools.time, 'monotonic', side_effect=[0, 0, 31, 31, 31]):
            factory.return_value.getresponse.return_value = self.public_response(b'x' * 100)
            self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])

    def test_public_page_closes_response_even_after_connection_releases_socket(self):
        tools = self.tools()
        response = self.public_response()
        with patch('socket.getaddrinfo', return_value=[self.public_address()]), patch.object(tools, '_PinnedHTTPSConnection') as factory:
            factory.return_value.sock = None
            factory.return_value.getresponse.return_value = response
            self.assertTrue(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])
            response.close.assert_called_once()

    def test_search_snippets_are_bounded_and_strip_control_characters(self):
        import json
        tools = self.tools()
        rows = [{'title': 'Evidence', 'url': 'https://example.com/' + str(i),
                 'content': '<nav>menu</nav>Text\x01\u202e ' + 'z' * 3000, 'publishedDate': 'D' * 200} for i in range(7)]
        with patch.dict(os.environ, {'JARVIS_SEARCH_URL': 'http://127.0.0.1:8888'}), patch.object(tools, '_http', return_value=json.dumps({'results': rows})):
            result = tools.execute_tool('web_search', {'query': 'test'})
            self.assertEqual(len(result['results']), 5)
            for row in result['results']:
                self.assertLessEqual(len(row['snippet']), 1500)
                self.assertLessEqual(len(row['publishedDate']), 100)
                self.assertNotIn('\x01', row['snippet'])
                self.assertNotIn('\u202e', row['snippet'])
                self.assertNotIn('menu', row['snippet'])

    def test_public_page_never_reports_empty_or_incomplete_evidence(self):
        tools = self.tools()
        with patch('socket.getaddrinfo', return_value=[self.public_address()]), patch.object(tools, '_PinnedHTTPSConnection') as factory:
            for body in [b'', b'<script>only_javascript()</script>', b'   ']:
                factory.return_value.getresponse.return_value = self.public_response(body, content_type='text/html')
                self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])
            response = self.public_response(b'cut short')
            response.headers['Content-Length'] = '100'
            factory.return_value.getresponse.return_value = response
            self.assertFalse(tools.execute_tool('read_public_page', {'url': 'https://example.com/'})['success'])

    def test_weather_uses_geocoding_and_current_forecast(self):
        tools = self.tools()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.side_effect = [b'{"results":[{"name":"Delhi","country":"India","latitude":28.6,"longitude":77.2}]}', b'{"current":{"temperature_2m":30,"time":"2026-01-01T12:00"},"current_units":{"temperature_2m":"C"},"timezone":"Asia/Kolkata"}']
        with patch('urllib.request.urlopen', return_value=response) as request:
            result = tools.execute_tool('weather', {'city': 'Delhi'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['current']['temperature_2m'], 30)
            self.assertIn('latitude=28.6', request.call_args.args[0].full_url)
        self.assertTrue(tools.tool_risk('weather'))

    def test_music_returns_encoded_browser_link_not_autoplay(self):
        tools = self.tools()
        result = tools.execute_tool('music_search', {'query': 'jazz & blues'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['url'], 'https://www.youtube.com/results?search_query=jazz+%26+blues')
        self.assertFalse(result['autoplay'])
        self.assertFalse(tools.tool_risk('music_search'))

    def test_google_wrapper_search_uses_safe_argv_and_clear_config_errors(self):
        tools = self.tools()
        with patch.dict(os.environ, {}, clear=True):
            result = tools.execute_tool('gmail_search', {'query': 'is:unread'})
            self.assertFalse(result['success'])
            self.assertIn('configured', result['error'])
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            script = Path(tmp) / 'wrapper.py'
            script.write_text('# configured wrapper')
            with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': str(script), 'JARVIS_GOOGLE_PYTHON': 'python-custom'}), patch('subprocess.run') as run:
                run.return_value = MagicMock(returncode=0, stdout='[{"id":"abc","subject":"Hi"}]')
                result = tools.execute_tool('gmail_search', {'query': 'is:unread; echo nope', 'max_results': 3})
                self.assertTrue(result['success'], result)
                self.assertEqual(result['messages'][0]['id'], 'abc')
                self.assertEqual(run.call_args.args[0], ['python-custom', str(script), 'gmail', 'search', 'is:unread; echo nope', '--max', '3'])
                self.assertFalse(run.call_args.kwargs['shell'])
                self.assertLessEqual(run.call_args.kwargs['timeout'], 30)
                run.return_value = MagicMock(returncode=1, stdout='PRIVATE_AUTH', stderr='PRIVATE_AUTH')
                result = tools.execute_tool('gmail_search', {'query': 'is:unread'})
                self.assertFalse(result['success'])
                self.assertNotIn('PRIVATE_AUTH', str(result))
                run.reset_mock()
                self.assertFalse(tools.execute_tool('gmail_search', {'query': 'x', 'max_results': True})['success'])
                run.assert_not_called()
        self.assertFalse(tools.tool_risk('gmail_search'))

    def test_gmail_imap_search_quotes_raw_query_and_uses_readonly_uids(self):
        tools = self.tools()
        with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': '', 'JARVIS_GMAIL_ADDRESS': 'owner@example.com', 'JARVIS_GMAIL_APP_PASSWORD': 'test-only'}), patch('imaplib.IMAP4_SSL') as factory:
            client = factory.return_value.__enter__.return_value
            client.select.return_value = ('OK', [b'2'])
            client.uid.side_effect = [('OK', [b'101 102'])]
            query = 'subject:"hello" \\ test'
            result = tools.execute_tool('gmail_search', {'query': query, 'max_results': 1})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['messages'], [{'id': '102', 'id_type': 'imap_uid'}])
            self.assertEqual(client.uid.call_args.args, ('SEARCH', None, 'X-GM-RAW', '"' + query.replace('\\', '\\\\').replace('"', '\\"') + '"'))
            client.select.assert_called_once_with('INBOX', readonly=True)
            self.assertEqual(factory.call_args.args[0], 'imap.gmail.com')
            self.assertLessEqual(factory.call_args.kwargs['timeout'], 20)
            client.uid.reset_mock()
            self.assertFalse(tools.execute_tool('gmail_search', {'query': 'x\r\nLOGOUT'})['success'])
            client.uid.assert_not_called()
            client.login.side_effect = RuntimeError('test-only PRIVATE_AUTH')
            result = tools.execute_tool('gmail_search', {'query': 'x'})
            self.assertFalse(result['success'])
            self.assertNotIn('test-only', str(result))

    def test_gmail_read_fetches_bounded_peek_and_supports_wrapper(self):
        tools = self.tools()
        with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': '', 'JARVIS_GMAIL_ADDRESS': 'owner@example.com', 'JARVIS_GMAIL_APP_PASSWORD': 'test-only'}), patch('imaplib.IMAP4_SSL') as factory:
            client = factory.return_value.__enter__.return_value
            client.select.return_value = ('OK', [b'1'])
            client.uid.return_value = ('OK', [(b'1 (UID 123 BODY[]<0> {65}', b'Subject: Hello\r\nFrom: a@example.com\r\nContent-Type: text/plain\r\n\r\nBody text'), b')'])
            result = tools.execute_tool('gmail_read', {'message_id': '123'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['message']['subject'], 'Hello')
            self.assertEqual(result['message']['body'], 'Body text')
            self.assertEqual(client.uid.call_args.args, ('FETCH', '123', '(BODY.PEEK[]<0.100000>)'))
            client.uid.reset_mock()
            self.assertFalse(tools.execute_tool('gmail_read', {'message_id': '1:*'})['success'])
            client.uid.assert_not_called()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            script = Path(tmp) / 'wrapper.py'
            script.write_text('# configured wrapper')
            with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': str(script)}), patch('subprocess.run') as run:
                run.return_value = MagicMock(returncode=0, stdout='{"id":"abc123","body":"Hi"}')
                self.assertTrue(tools.execute_tool('gmail_read', {'message_id': 'abc123'})['success'])
                self.assertEqual(run.call_args.args[0][-3:], ['gmail', 'get', 'abc123'])
        self.assertFalse(tools.tool_risk('gmail_read'))

    def test_gmail_send_is_single_recipient_validated_and_no_retry(self):
        tools = self.tools()
        args = {'to': 'friend@example.com', 'subject': 'Hello', 'body': 'Direct requested message'}
        with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': '', 'JARVIS_GMAIL_ADDRESS': 'owner@example.com', 'JARVIS_GMAIL_APP_PASSWORD': 'test-only'}), patch('smtplib.SMTP_SSL') as factory:
            client = factory.return_value.__enter__.return_value
            client.send_message.return_value = {}
            result = tools.execute_tool('gmail_send', args)
            self.assertTrue(result['success'], result)
            self.assertEqual(result['status'], 'provider_accepted')
            self.assertFalse(result['delivery_confirmed'])
            self.assertTrue(result['message_id'].startswith('<'))
            self.assertEqual(client.send_message.call_args.args[0]['To'], args['to'])
            self.assertEqual(factory.call_args.args[:2], ('smtp.gmail.com', 465))
            client.send_message.reset_mock()
            for invalid in [{'to': 'a@example.com,b@example.com'}, {'to': 'a@example.com\r\nBcc: b@example.com'}, {'to': 'bad'}, {'subject': 'x\nBcc:y'}, {'body': 'x' * 20001}]:
                self.assertFalse(tools.execute_tool('gmail_send', {**args, **invalid})['success'])
            client.send_message.assert_not_called()
            client.send_message.side_effect = TimeoutError('test-only')
            result = tools.execute_tool('gmail_send', args)
            self.assertFalse(result['success'])
            self.assertNotIn('test-only', str(result))
            client.send_message.assert_called_once()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            script = Path(tmp) / 'wrapper.py'
            script.write_text('# configured wrapper')
            with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': str(script)}), patch('subprocess.run') as run:
                run.return_value = MagicMock(returncode=0, stdout='{"status":"sent","id":"xyz"}')
                self.assertTrue(tools.execute_tool('gmail_send', args)['success'])
                self.assertEqual(run.call_args.args[0][-5:], ['gmail', 'send', '--to=friend@example.com', '--subject=Hello', '--body=Direct requested message'])
        self.assertTrue(tools.tool_risk('gmail_send'))

    def test_calendar_list_validates_timezone_and_wrapper_output(self):
        tools = self.tools()
        with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': ''}):
            self.assertIn('configured', tools.execute_tool('calendar_list', {})['error'])
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            script = Path(tmp) / 'wrapper.py'
            script.write_text('# configured wrapper')
            with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': str(script)}), patch('subprocess.run') as run:
                run.return_value = MagicMock(returncode=0, stdout='[{"id":"event1"}]')
                args = {'start': '2026-01-01T12:00:00+05:30', 'end': '2026-01-02T12:00:00+05:30'}
                result = tools.execute_tool('calendar_list', args)
                self.assertTrue(result['success'], result)
                self.assertEqual(result['events'][0]['id'], 'event1')
                self.assertEqual(run.call_args.args[0][-4:], ['calendar', 'list', '--start=' + args['start'], '--end=' + args['end']])
                run.reset_mock()
                for bad in [{'start': '2026-01-01T12:00:00', 'end': args['end']}, {'start': args['end'], 'end': args['start']}, {'start': args['start']}]:
                    self.assertFalse(tools.execute_tool('calendar_list', bad)['success'])
                run.assert_not_called()
        self.assertFalse(tools.tool_risk('calendar_list'))

    def test_calendar_create_validates_range_and_has_mutation_risk(self):
        tools = self.tools()
        args = {'summary': 'Meeting', 'start': '2026-01-01T12:00:00Z', 'end': '2026-01-01T13:00:00Z', 'location': 'Office'}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            script = Path(tmp) / 'wrapper.py'
            script.write_text('# configured wrapper')
            with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': str(script)}), patch('subprocess.run') as run:
                run.return_value = MagicMock(returncode=0, stdout='{"status":"created","id":"event1"}')
                result = tools.execute_tool('calendar_create', args)
                self.assertTrue(result['success'], result)
                self.assertEqual(result['event']['id'], 'event1')
                self.assertIn('--summary=Meeting', run.call_args.args[0])
                run.reset_mock()
                self.assertFalse(tools.execute_tool('calendar_create', {**args, 'end': args['start']})['success'])
                run.assert_not_called()
        self.assertTrue(tools.tool_risk('calendar_create'))

    def test_telegram_send_uses_fixed_config_chat_and_checks_api_status(self):
        import json
        tools = self.tools()
        with patch.dict(os.environ, {'JARVIS_TELEGRAM_BOT_TOKEN': '', 'JARVIS_TELEGRAM_CHAT_ID': ''}):
            self.assertIn('configured', tools.execute_tool('telegram_send', {'text': 'Hello'})['error'])
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"ok":true,"result":{"message_id":42,"chat":{"id":123},"text":"Hello"}}'
        with patch.dict(os.environ, {'JARVIS_TELEGRAM_BOT_TOKEN': '123:test-only', 'JARVIS_TELEGRAM_CHAT_ID': '123'}), patch('urllib.request.urlopen', return_value=response) as request:
            result = tools.execute_tool('telegram_send', {'text': 'Hello'})
            self.assertTrue(result['success'], result)
            self.assertEqual(result['message_id'], 42)
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload['chat_id'], '123')
            self.assertEqual(payload['text'], 'Hello')
            self.assertEqual(request.call_args.args[0].get_method(), 'POST')
            self.assertNotIn('parse_mode', payload)
            response.read.return_value = b'{"ok":false,"description":"123:test-only"}'
            result = tools.execute_tool('telegram_send', {'text': 'Hello'})
            self.assertFalse(result['success'])
            self.assertNotIn('test-only', str(result))
            request.reset_mock()
            self.assertFalse(tools.execute_tool('telegram_send', {'text': 'Hello', 'chat_id': '999'})['success'])
            self.assertFalse(tools.execute_tool('telegram_send', {'text': 'x' * 4097})['success'])
            request.assert_not_called()
        self.assertTrue(tools.tool_risk('telegram_send'))

    def test_workspace_rejects_links_to_hidden_files_inside_root(self):
        tools = self.tools()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp, patch.dict(os.environ, {'JARVIS_WORKSPACE_DIR': tmp}):
            root = Path(tmp)
            hidden = root / '.private'
            hidden.mkdir()
            (hidden / 'data.txt').write_text('private')
            alias = root / 'alias'
            try:
                alias.symlink_to(hidden, target_is_directory=True)
            except OSError:
                import subprocess
                p = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(alias), str(hidden)], capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
            try:
                self.assertFalse(tools.execute_tool('read_file', {'path': 'alias/data.txt'})['success'])
                self.assertFalse(tools.execute_tool('write_file', {'path': 'alias/new.txt', 'content': 'x'})['success'])
            finally:
                if alias.is_symlink():
                    alias.unlink()
                else:
                    alias.rmdir()

    def test_validation_rejects_whitespace_bad_mail_and_bad_offsets(self):
        tools = self.tools()
        with patch('subprocess.run') as run, patch('urllib.request.urlopen') as network, patch('smtplib.SMTP_SSL') as smtp:
            for name, args in [
                ('web_search', {'query': '   '}),
                ('gmail_send', {'to': '.user@example.com', 'subject': 'Hi', 'body': 'Hi'}),
                ('gmail_send', {'to': 'user@a..example.com', 'subject': 'Hi', 'body': 'Hi'}),
                ('calendar_create', {'summary': 'Event', 'start': '2026-01-01T10:00:00+05:99', 'end': '2026-01-01T12:00:00+05:99'}),
            ]:
                self.assertFalse(tools.execute_tool(name, args)['success'])
            run.assert_not_called()
            network.assert_not_called()
            smtp.assert_not_called()

    def test_gmail_rejects_invalid_localpart_before_connecting(self):
        tools = self.tools()
        with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': '', 'JARVIS_GMAIL_ADDRESS': 'owner@example.com', 'JARVIS_GMAIL_APP_PASSWORD': 'test-only'}), patch('smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            for recipient in ['.user@example.com', 'user.@example.com', 'user@-bad.example.com', 'user@bad-.example.com', 'x' * 65 + '@example.com']:
                result = tools.execute_tool('gmail_send', {'to': recipient, 'subject': 'Hi', 'body': 'Hi'})
                self.assertFalse(result['success'], recipient)
            smtp.assert_not_called()

    def test_calendar_rejects_invalid_offsets_before_wrapper(self):
        tools = self.tools()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            script = Path(tmp) / 'wrapper.py'
            script.write_text('# configured wrapper')
            with patch.dict(os.environ, {'JARVIS_GOOGLE_SCRIPT': str(script)}), patch('subprocess.run') as run:
                run.return_value = MagicMock(returncode=0, stdout='{"status":"created","id":"event1"}')
                result = tools.execute_tool('calendar_create', {'summary': 'Event', 'start': '2026-01-01T10:00:00+05:99', 'end': '2026-01-01T12:00:00+05:99'})
                self.assertFalse(result['success'])
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
