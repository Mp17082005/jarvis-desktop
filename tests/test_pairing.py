"""Real ASGI/SQLite handoff tests; never start desktop hardware workers."""
import pytest
from fastapi.testclient import TestClient

TOKEN = "pairing-test-token-not-a-real-secret"
LOCAL = "http://127.0.0.1:8767"
CLOUD = "https://jarvis.example"
DISCOVERY = "/companion/discovery"


def make_app(monkeypatch, tmp_path, *, enabled=True, local_only=True):
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("JARVIS_DESKTOP_ENABLED", str(enabled).lower())
    monkeypatch.setenv("JARVIS_DESKTOP_LOCAL_ONLY", str(local_only).lower())
    monkeypatch.setenv("JARVIS_DEVICE_NAME", "Pairing test PC")
    from jarvis.main import create_app
    return create_app()


def local_client(app, *, peer="127.0.0.1", base=LOCAL):
    # Not entering TestClient deliberately avoids lifespan/hardware startup.
    return TestClient(app, base_url=base, client=(peer, 49000))


def test_discovery_is_minimal_credentialless_cross_origin_get(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path))
    response = client.get(DISCOVERY, params={"origin": CLOUD}, headers={"Origin": CLOUD, "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200
    assert response.json() == {"installed": True, "version": "ready1", "paired": False,
                               "device_name": "This Windows device", "target": LOCAL + "/"}
    assert response.headers["access-control-allow-origin"] == CLOUD
    assert response.headers["vary"] == "Origin"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-credentials" not in response.headers
    assert "set-cookie" not in response.headers


def test_local_authenticated_approval_persists_exact_origin_and_revokes(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path))
    assert client.get("/api/pairing").status_code == 401
    assert client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL}).status_code == 401
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    assert client.get("/api/pairing").json() == {"origins": [], "device_name": "Pairing test PC"}
    response = client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL})
    assert response.status_code == 200
    assert response.json() == {"origins": [CLOUD], "device_name": "Pairing test PC", "target_url": LOCAL + "/"}
    assert "set-cookie" not in response.headers
    again = local_client(make_app(monkeypatch, tmp_path))
    discovery = again.get(DISCOVERY, params={"origin": CLOUD}, headers={"Origin": CLOUD}).json()
    assert discovery["paired"] is True
    assert discovery["device_name"] == "Pairing test PC"
    assert again.get(DISCOVERY).json()["paired"] is False
    other = again.get(DISCOVERY, headers={"Origin": "https://other.example"}).json()
    assert other["paired"] is False and other["device_name"] == "This Windows device"
    assert client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL}).json()["origins"] == [CLOUD]
    assert client.post("/api/pairing/revoke", json={"origin": CLOUD}, headers={"Origin": LOCAL}).json()["origins"] == []
    assert again.get(DISCOVERY, headers={"Origin": CLOUD}).json()["paired"] is False


def test_grants_are_bounded_and_duplicates_remain_idempotent(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path))
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    for i in range(16):
        assert client.post("/api/pairing/approve", json={"origin": f"https://site{i}.example"}, headers={"Origin": LOCAL}).status_code == 200
    assert client.post("/api/pairing/approve", json={"origin": "https://site0.example"}, headers={"Origin": LOCAL}).status_code == 200
    assert client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL}).status_code == 409
    assert len(client.get("/api/pairing").json()["origins"]) == 16
    assert client.post("/api/pairing/revoke", json={"origin": "https://site0.example"}, headers={"Origin": LOCAL}).status_code == 200
    assert client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL}).status_code == 200


@pytest.mark.parametrize("origin", [
    "null", "*", "http://cloud.example", "https://user@cloud.example", "https://user:pass@cloud.example",
    "https://cloud.example/", "https://cloud.example/path", "https://cloud.example?", "https://cloud.example#",
    "https://cloud.example?x=1", "https://cloud.example#x", "https://cloud.example\\evil", "https://cloud.example%2f.evil",
    "https://cloud.example:0", "https://cloud.example:65536", "https://cloud.example:",
    "https://cloud..example", "https://-cloud.example", "https://cloud.example,https://evil.example",
    "https://cloud.example https://evil.example", " https://cloud.example", "https://cloud.example\t", "ftp://cloud.example",
    "http://localhost.evil.example", "http://127.0.0.1.evil.example", "http://127.1", "http://2130706433",
    "https://[::1]evil", "https://[::1].evil", "https://[::1]:+443",
])
def test_invalid_origins_never_receive_cors_or_grants(monkeypatch, tmp_path, origin):
    client = local_client(make_app(monkeypatch, tmp_path))
    response = client.get(DISCOVERY, headers={"Origin": origin})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    assert client.post("/api/pairing/approve", json={"origin": origin}, headers={"Origin": LOCAL}).status_code == 422
    assert client.get("/api/pairing").json()["origins"] == []


@pytest.mark.parametrize("origin", [CLOUD, "https://Cloud.Example:443", "http://localhost:8765", "http://127.0.0.1:8765", "http://[::1]:8765"])
def test_valid_origin_is_preserved_exactly(monkeypatch, tmp_path, origin):
    client = local_client(make_app(monkeypatch, tmp_path))
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    assert client.post("/api/pairing/approve", json={"origin": origin}, headers={"Origin": LOCAL}).json()["origins"] == [origin]
    response = client.get(DISCOVERY, params={"origin": origin}, headers={"Origin": origin})
    assert response.json()["paired"] is True
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("peer,headers", [
    ("203.0.113.1", {}), ("localhost", {}),
    ("127.0.0.1", {"Host": "localhost.evil.example:8767"}),
    ("127.0.0.1", {"Host": "evil.example"}),
    ("127.0.0.1", {"Host": "127.0.0.1:8767?"}),
    ("127.0.0.1", {"Host": "127.0.0.1:8767#"}),
    ("127.0.0.1", {"Host": "127.0.0.1:8767:"}),
    ("127.0.0.1", {"Forwarded": "for=127.0.0.1"}),
    ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
    ("127.0.0.1", {"X-Forwarded-Host": "127.0.0.1:8767"}),
    ("127.0.0.1", {"X-Forwarded-Proto": "http"}),
])
def test_discovery_rejects_bad_transport_and_authority(monkeypatch, tmp_path, peer, headers):
    client = local_client(make_app(monkeypatch, tmp_path), peer=peer)
    response = client.get(DISCOVERY, headers={"Origin": CLOUD, **headers})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_query_duplicates_and_mismatches_are_not_reflected(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path))
    cases = [({"origin": "https://other.example"}, {"Origin": CLOUD}),
             ({"origin": CLOUD}, {}),
             ([("origin", CLOUD), ("origin", CLOUD)], {"Origin": CLOUD}),
             ({}, [("Origin", CLOUD), ("Origin", CLOUD)]),
             ({}, [("Origin", CLOUD), ("Host", "127.0.0.1:8767"), ("Host", "127.0.0.1:8767")])]
    for params, headers in cases:
        response = client.get(DISCOVERY, params=params, headers=headers)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers


def test_pairing_is_not_a_remote_control_grant(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path))
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    for path in ("/api/pairing/approve", "/api/pairing/revoke"):
        for headers in ({}, {"Origin": CLOUD}, {"Origin": LOCAL, "Sec-Fetch-Site": "cross-site"}, {"Origin": "http://localhost:8767"}):
            assert client.post(path, json={"origin": CLOUD}, headers=headers).status_code == 403
    assert client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL}).status_code == 200
    for method, path in [("GET", "/api/pairing"), ("GET", "/api/desktop/status"), ("POST", "/api/chat"),
                         ("POST", "/api/login"), ("POST", "/api/pairing/approve"), ("OPTIONS", "/api/desktop/status"),
                         ("POST", DISCOVERY), ("HEAD", DISCOVERY), ("GET", DISCOVERY + "/"), ("GET", "/companion/%64iscovery")]:
        response = client.request(method, path, headers={"Origin": CLOUD}, follow_redirects=False)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": CLOUD}):
            pass
    assert client.get("/api/pairing").json()["origins"] == [CLOUD]


@pytest.mark.parametrize("enabled,local_only", [(False, False), (False, True), (True, False)])
def test_pairing_and_discovery_exist_only_in_explicit_local_mode(monkeypatch, tmp_path, enabled, local_only):
    client = local_client(make_app(monkeypatch, tmp_path, enabled=enabled, local_only=local_only))
    assert client.get(DISCOVERY).status_code == 404
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    assert client.get("/api/pairing").status_code == 404
    assert client.post("/api/pairing/approve", json={"origin": CLOUD}, headers={"Origin": LOCAL}).status_code in (404, 405)
    response = client.get(DISCOVERY, headers={"Origin": CLOUD})
    assert "access-control-allow-origin" not in response.headers


def test_connect_page_alone_can_probe_fixed_companion_origin(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path, enabled=False))
    connect = client.get("/connect.html")
    assert connect.status_code == 200
    assert "connect-src 'self' http://127.0.0.1:8767;" in connect.headers["content-security-policy"]
    for path in ("/", "/pair-device.html", "/static/connect.html"):
        response = client.get(path)
        assert "connect-src 'self';" in response.headers["content-security-policy"]
        assert "http://127.0.0.1:8767" not in response.headers["content-security-policy"]


def test_fixed_package_download_is_public_and_ignores_request_paths(monkeypatch, tmp_path):
    import zipfile
    package = tmp_path / "companion.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("README.txt", "Test installer fixture, no credentials")
    monkeypatch.setenv("JARVIS_COMPANION_PACKAGE_PATH", str(package))
    client = local_client(make_app(monkeypatch, tmp_path, enabled=False))
    response = client.get("/downloads/Jarvis-Windows-Companion.zip", params={"path": "../.env", "file": "../jarvis.sqlite3"})
    assert response.status_code == 200
    assert response.content == package.read_bytes()
    assert response.headers["content-type"] == "application/zip"
    assert "Jarvis-Windows-Companion.zip" in response.headers["content-disposition"]
    assert "set-cookie" not in response.headers
    for path in ("/downloads/.env", "/downloads/other.zip", "/downloads/%2e%2e/.env"):
        assert client.get(path).status_code == 404
    package.unlink()
    assert client.get("/downloads/Jarvis-Windows-Companion.zip").status_code == 404


def test_discovery_malformed_raw_query_fails_closed():
    from jarvis.pairing import discovery_scope
    scope = {"type": "http", "method": "GET", "path": DISCOVERY, "raw_path": DISCOVERY.encode(),
             "scheme": "http", "client": ("127.0.0.1", 49000), "query_string": b"origin=\xff",
             "headers": [(b"host", b"127.0.0.1:8767"), (b"origin", CLOUD.encode())]}
    assert discovery_scope(scope) is False


def test_discovery_private_network_preflight_is_get_only(monkeypatch, tmp_path):
    client = local_client(make_app(monkeypatch, tmp_path))
    headers = {"Origin": CLOUD, "Sec-Fetch-Site": "cross-site",
               "Access-Control-Request-Method": "GET", "Access-Control-Request-Private-Network": "true"}
    response = client.options(DISCOVERY, params={"origin": CLOUD}, headers=headers)
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["access-control-allow-origin"] == CLOUD
    assert response.headers["access-control-allow-methods"] == "GET"
    assert response.headers["access-control-allow-private-network"] == "true"
    assert "access-control-allow-credentials" not in response.headers
    for extra in ({"Access-Control-Request-Method": "POST"}, {"Access-Control-Request-Headers": "authorization"}):
        denied = client.options(DISCOVERY, headers={**headers, **extra})
        assert denied.status_code == 403
        assert "access-control-allow-origin" not in denied.headers
