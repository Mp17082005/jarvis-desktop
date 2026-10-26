"""Read-only HTTP smoke-check contracts; all unit-test networking is mocked."""
import unittest
from unittest.mock import MagicMock, patch

try:
    from scripts import check_readiness as readiness
except ImportError:
    readiness = None


class ReadinessTests(unittest.TestCase):
    def test_check_http_sends_only_unauthenticated_get_without_reading_body(self):
        self.assertIsNotNone(readiness, "The readiness checker has not been implemented")
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        with patch("urllib.request.OpenerDirector.open", return_value=response) as opened:
            result = readiness.check_http("https://example.com/", expected_status=200, timeout=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(opened.call_args.kwargs["timeout"], 3)
        response.read.assert_not_called()
        response.__exit__.assert_called_once()

    def test_http_401_is_success_only_when_expected_and_error_is_closed(self):
        from urllib.error import HTTPError
        error = HTTPError("https://example.com/api/status", 401, "private", {}, MagicMock())
        with patch("urllib.request.OpenerDirector.open", side_effect=error):
            result = readiness.check_http("https://example.com/api/status", 401)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 401)
        error.fp.close.assert_called_once()
        with patch("urllib.request.OpenerDirector.open", side_effect=HTTPError(
                "https://example.com/", 500, "private", {}, None)):
            self.assertFalse(readiness.check_http("https://example.com/", 200)["ok"])

    def test_network_errors_fail_closed_without_printing_exception_secrets(self):
        from urllib.error import URLError
        for error in (URLError("private-token"), TimeoutError("private-token"), OSError("private-token")):
            with self.subTest(error=type(error).__name__):
                with patch("urllib.request.OpenerDirector.open", side_effect=error):
                    result = readiness.check_http("https://example.com/", 200)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["status"])
                self.assertNotIn("private-token", str(result))

    def test_transport_refuses_redirects_and_uses_verified_tls_without_proxy_credentials(self):
        import ssl
        import urllib.request
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        original = urllib.request.build_opener
        with patch("urllib.request.build_opener", wraps=original) as built:
            with patch("urllib.request.OpenerDirector.open", return_value=response):
                readiness.check_http("https://example.com/", 200)
        handlers = built.call_args.args
        redirect = next((h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)), None)
        self.assertIsNotNone(redirect, "An explicit fail-closed redirect handler is required")
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "http://other.test/"))
        https = next(h for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        proxy = next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})

    def test_cli_probes_local_and_public_origins_without_claiming_production_ready(self):
        import io
        import json
        self.assertTrue(callable(getattr(readiness, "main", None)), "CLI entry point missing")
        output = io.StringIO()
        with patch.dict("os.environ", {"JARVIS_PUBLIC_URL": "https://example.com/"}):
            with patch.object(readiness, "check_http", return_value={"ok": True, "status": 200}) as check:
                with patch("sys.stdout", output):
                    exit_code = readiness.main([])
        self.assertEqual(exit_code, 0)
        self.assertEqual([(c.args[0], c.kwargs["expected_status"]) for c in check.call_args_list], [
            ("http://127.0.0.1:8765/", 200), ("http://127.0.0.1:8765/api/status", 401),
            ("https://example.com/", 200), ("https://example.com/api/status", 401)])
        report = json.loads(output.getvalue())
        self.assertTrue(report["smoke_ok"])
        self.assertFalse(report["production_ready"])
        self.assertEqual(len(report["checks"]), 4)

    def test_cli_rejects_unsafe_origins_before_any_network_request(self):
        import io
        invalid = [[], ["--public-url", "http://example.com"],
                   ["--public-url", "https://user:private-token@example.com"],
                   ["--public-url", "https://example.com/?token=private-token"],
                   ["--public-url", "https://example.com/#private-token"],
                   ["--public-url", "https://example.com/path"],
                   ["--public-url", "https://example.com:wrong"],
                   ["--public-url", "https://example.com\n"],
                   ["--public-url", "https://example.com", "--timeout", "nan"],
                   ["--public-url", "https://example.com", "--timeout", "0"],
                   ["--public-url", "https://example.com", "--local-url", "http://remote.example"]]
        for argv in invalid:
            with self.subTest(argv=argv), patch.dict("os.environ", {"JARVIS_PUBLIC_URL": ""}):
                output = io.StringIO()
                with patch.object(readiness, "check_http", return_value={"ok": True}) as check, patch("sys.stdout", output):
                    code = readiness.main(argv)
                self.assertEqual(code, 2)
                check.assert_not_called()
                self.assertNotIn("private-token", output.getvalue())

    def test_cli_returns_one_and_checks_every_target_when_one_probe_fails(self):
        import io
        import json
        output = io.StringIO()
        with patch.object(readiness, "check_http", side_effect=[
                {"ok": False, "status": 503}, {"ok": True, "status": 401},
                {"ok": True, "status": 200}, {"ok": True, "status": 401}]) as check:
            with patch("sys.stdout", output):
                code = readiness.main(["--public-url", "https://example.com"])
        self.assertEqual(code, 1)
        self.assertEqual(check.call_count, 4)
        self.assertFalse(json.loads(output.getvalue())["smoke_ok"])

    def test_redirect_status_fails_instead_of_becoming_a_login_page_pass(self):
        from urllib.error import HTTPError
        with patch("urllib.request.OpenerDirector.open", side_effect=HTTPError(
                "https://example.com/", 302, "redirect", {}, None)):
            result = readiness.check_http("https://example.com/", 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 302)

    def test_malformed_http_reply_fails_without_traceback_or_private_details(self):
        import http.client
        with patch("urllib.request.OpenerDirector.open", side_effect=http.client.BadStatusLine("private-token")):
            result = readiness.check_http("https://example.com/", 200)
        self.assertFalse(result["ok"])
        self.assertNotIn("private-token", str(result))


if __name__ == "__main__":
    unittest.main()
