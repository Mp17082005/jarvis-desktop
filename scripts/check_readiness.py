"""Read-only HTTP smoke checks. Uses only the Python standard library."""
import http.client
import ssl
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not let a login redirect or HTTPS downgrade produce a false pass."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_http(url, expected_status, timeout=10):
    """GET response headers only, without authentication or a cookie jar."""
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (OSError, http.client.HTTPException) as exc:
        return {"ok": False, "status": None, "expected_status": expected_status,
                "error": type(exc).__name__}
    return {"ok": status == expected_status, "status": status,
            "expected_status": expected_status}


def main(argv=None):
    """Emit a smoke-check report, not a production certification."""
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-url", default="http://127.0.0.1:8765")
    parser.add_argument("--public-url", default=os.environ.get("JARVIS_PUBLIC_URL", ""))
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        import math
        from urllib.parse import urlsplit

        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
            raise ValueError()
        for public, raw in ((False, args.local_url), (True, args.public_url)):
            if not raw or any(ord(char) <= 32 for char in raw) or "\\" in raw:
                raise ValueError()
            parsed = urlsplit(raw)
            if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                    or parsed.port == 0 or "?" in raw or "#" in raw):
                raise ValueError()
            if public and parsed.scheme != "https":
                raise ValueError()
            if not public and (parsed.scheme not in ("http", "https")
                               or parsed.hostname not in ("127.0.0.1", "::1", "localhost")):
                raise ValueError()
    except ValueError:
        print(json.dumps({"smoke_ok": False, "production_ready": False, "checks": [],
                          "error": "Require a loopback local origin, a credential-free HTTPS public origin, and timeout in (0, 60]. No paths, queries, or fragments."}))
        return 2
    checks = []
    for scope, base in (("local", args.local_url), ("public", args.public_url)):
        for path, expected in (("/", 200), ("/api/status", 401)):
            result = check_http(base.rstrip("/") + path, expected_status=expected, timeout=args.timeout)
            checks.append({"scope": scope, "path": path, **result})
    ok = all(check["ok"] for check in checks)
    print(json.dumps({"smoke_ok": ok, "production_ready": False,
                      "note": "HTTP/TLS smoke only; release gates require separate evidence.",
                      "checks": checks}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
