"""Local UI handoff only. Pairing never authorizes remote desktop actions.

Discovery may be blocked by browser local-network permission or policy; callers
must offer a manual local-UI link rather than infer that nothing is installed.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import os
import platform
import time

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import Headers, QueryParams
from starlette.responses import JSONResponse

DISCOVERY_PATH = "/companion/discovery"
VERSION = "ready1"


def valid_origin(value):
    """Accept serialized origins, not arbitrary URLs; preserve the exact key."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if any(ord(c) <= 32 or ord(c) >= 127 for c in value) or any(c in value for c in "\\%@?#"):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not value.startswith(parsed.scheme + "://"):
            return False
        if not parsed.hostname or parsed.path or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.netloc.endswith(":") or (parsed.port is not None and not 1 <= parsed.port <= 65535):
            return False
        host = parsed.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")):
            return False
        if parsed.scheme == "http":
            from .desktop import loopback
            return loopback(host)
        return True
    except (ValueError, TypeError):
        return False


def discovery_scope(scope):
    """The sole cross-origin exemption: exact harmless GET/OPTIONS + real peer."""
    if scope.get("type") != "http" or scope.get("method") not in {"GET", "OPTIONS"} or scope.get("path") != DISCOVERY_PATH:
        return False
    if scope.get("raw_path", DISCOVERY_PATH.encode()) != DISCOVERY_PATH.encode():
        return False
    try:
        if not ipaddress.ip_address(scope["client"][0]).is_loopback:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    headers = Headers(scope=scope)
    hosts = headers.getlist("host")
    if len(hosts) != 1 or not valid_origin("http://" + hosts[0]):
        return False
    origins = headers.getlist("origin")
    if len(origins) > 1 or (origins and not valid_origin(origins[0])):
        return False
    try:
        values = QueryParams(scope.get("query_string", b"").decode("ascii")).getlist("origin")
    except UnicodeDecodeError:
        return False
    if values and (len(values) != 1 or not origins or values[0] != origins[0]):
        return False
    # Retain Host, actual peer and every proxy claim. Ignore only browser origin
    # checks for this inert endpoint, not for login, pairing, API or WebSocket.
    from .desktop import local_scope
    transport = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k.lower() not in {b"origin", b"sec-fetch-site"}]}
    return local_scope(transport)


class OriginGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(min_length=1, max_length=2048, strict=True)

    @field_validator("origin")
    @classmethod
    def serialized_origin(cls, value):
        if not valid_origin(value):
            raise ValueError("Expected an HTTPS or loopback HTTP origin")
        return value


def register_pairing_routes(app, store, authenticated):
    # Device-level grants: authenticated local sessions share one configured
    # owner. Audit IDs are already hashed by Store, never raw login cookies.
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS pairings (origin TEXT PRIMARY KEY, granted_by TEXT NOT NULL, granted_at REAL NOT NULL)")
    device_name = os.getenv("JARVIS_DEVICE_NAME", platform.node() or "This Windows device")[:128]

    def status():
        with store.connect() as db:
            origins = [row[0] for row in db.execute("SELECT origin FROM pairings ORDER BY origin")]
        return {"origins": origins, "device_name": device_name}

    def local_session(request: Request, session=Depends(authenticated)):
        from .desktop import local_scope
        if not local_scope(request.scope):
            raise HTTPException(403, "Pairing requires the local UI")
        if request.method == "POST" and request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Pairing requires an explicit same-origin request")
        return session

    @app.get("/api/pairing")
    def pairing_status(session=Depends(local_session)):
        return status()

    @app.post("/api/pairing/approve")
    def approve(body: OriginGrant, request: Request, session=Depends(local_session)):
        with store.connect() as db:
            # Serialize check+insert across processes as well as request threads.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM pairings WHERE origin=?", (body.origin,)).fetchone() and db.execute("SELECT count(*) FROM pairings").fetchone()[0] >= 16:
                raise HTTPException(409, "Revoke an origin before pairing another (limit 16)")
            db.execute("INSERT OR IGNORE INTO pairings VALUES (?,?,?)", (body.origin, session["id"], time.time()))
        return {**status(), "target_url": str(request.base_url)}

    @app.post("/api/pairing/revoke")
    def revoke(body: OriginGrant, session=Depends(local_session)):
        with store.connect() as db:
            db.execute("DELETE FROM pairings WHERE origin=?", (body.origin,))
        return status()

    @app.options(DISCOVERY_PATH)
    @app.get(DISCOVERY_PATH)
    def discovery(request: Request):
        if not discovery_scope(request.scope):
            return JSONResponse({"detail": "Invalid local discovery request"}, status_code=403)
        origin = request.headers.get("origin")
        headers = {"Cache-Control": "no-store", "Vary": "Origin"}
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
        if request.method == "OPTIONS":
            if not origin or request.headers.get("access-control-request-method") != "GET" or request.headers.get("access-control-request-headers"):
                return JSONResponse({"detail": "Discovery allows only credentialless GET"}, status_code=403)
            headers["Access-Control-Allow-Methods"] = "GET"
            if request.headers.get("access-control-request-private-network") == "true":
                headers["Access-Control-Allow-Private-Network"] = "true"
            from starlette.responses import Response
            return Response(status_code=204, headers=headers)
        with store.connect() as db:
            paired = bool(origin and db.execute("SELECT 1 FROM pairings WHERE origin=?", (origin,)).fetchone())
        return JSONResponse({"installed": True, "version": VERSION, "paired": paired,
                             "device_name": device_name if paired else "This Windows device", "target": str(request.base_url)}, headers=headers)
