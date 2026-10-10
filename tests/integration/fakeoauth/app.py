"""A fake OAuth 2.0 provider (Google / Microsoft stand-in) for the kuvert test suite.

Implements the authorization-code grant with PKCE (S256) and refresh tokens the way the real
providers do, strictly: the code is bound to the client, redirect URI and challenge, used once,
and a refresh token can be revoked (then ``invalid_grant``). The browser leg is replaced by
``/_admin/approve``, which a test calls with the parameters of the authorization URL.

* ``POST /token``            grant_type=authorization_code | refresh_token
* ``POST /_admin/clients``   register ``{client_id, client_secret}``
* ``POST /_admin/approve``   ``{client_id, redirect_uri, code_challenge, scope, email, name, access_token}`` → ``{code}``
* ``POST /_admin/revoke``    ``{email}``: every refresh token of that address stops working
* ``POST /_admin/config``    ``{expires_in, rotate_refresh}``
* ``POST /_admin/hold`` / ``/_admin/release``  park token requests until released
* ``GET  /_admin/held``      how many requests are parked right now
* ``GET  /_admin/log``       every token request (grant type, client)
* ``GET  /_admin/health``
"""

import asyncio
import base64
import hashlib
import json
import secrets

from aiohttp import web

STATE: dict = {"clients": {}, "codes": {}, "refresh": {}, "revoked": set(), "config": {"expires_in": 3600, "rotate_refresh": False}, "log": [], "hold": asyncio.Event(), "held": 0}
STATE["hold"].set()


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _id_token(email: str, name: str) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64({'iss': 'fakeoauth', 'email': email, 'name': name, 'email_verified': True})}.sig"


def _error(kind: str, description: str, status: int = 400) -> web.Response:
    return web.json_response({"error": kind, "error_description": description}, status=status)


def _grant(email: str, name: str, access_token: str, scope: str, with_refresh: bool = True) -> dict:
    body = {"access_token": access_token, "token_type": "Bearer", "expires_in": STATE["config"]["expires_in"], "scope": scope, "id_token": _id_token(email, name)}
    if with_refresh:
        refresh = secrets.token_urlsafe(24)
        STATE["refresh"][refresh] = {"email": email, "name": name, "access_token": access_token, "scope": scope}
        body["refresh_token"] = refresh
    return body


async def token(request: web.Request) -> web.Response:
    form = await request.post()
    client_id, secret = form.get("client_id"), form.get("client_secret")
    STATE["log"].append({"grant_type": form.get("grant_type"), "client_id": client_id})
    if not STATE["hold"].is_set():
        STATE["held"] += 1
        try:
            await STATE["hold"].wait()
        finally:
            STATE["held"] -= 1
    client = STATE["clients"].get(client_id)
    if client is None or (client.get("client_secret") and client["client_secret"] != secret):
        return _error("invalid_client", "Unknown client or wrong secret.", 401)
    if form.get("grant_type") == "authorization_code":
        code = STATE["codes"].pop(form.get("code", ""), None)
        if code is None:
            return _error("invalid_grant", "Unknown or used code.")
        if code["client_id"] != client_id or code["redirect_uri"] != form.get("redirect_uri"):
            return _error("invalid_grant", "The code was issued to another client or redirect URI.")
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != code["code_challenge"]:
            return _error("invalid_grant", "PKCE verification failed.")
        return web.json_response(_grant(code["email"], code["name"], code["access_token"], code["scope"]))
    if form.get("grant_type") == "refresh_token":
        refresh = form.get("refresh_token", "")
        grant = STATE["refresh"].get(refresh)
        if grant is None or grant["email"] in STATE["revoked"]:
            return _error("invalid_grant", "Token has been expired or revoked.")
        rotate = STATE["config"]["rotate_refresh"]
        if rotate:
            STATE["refresh"].pop(refresh)
        body = _grant(grant["email"], grant["name"], grant["access_token"], grant["scope"], with_refresh=rotate)
        return web.json_response(body)
    return _error("unsupported_grant_type", "Only authorization_code and refresh_token.")


async def clients(request: web.Request) -> web.Response:
    body = await request.json()
    STATE["clients"][body["client_id"]] = body
    return web.json_response({"ok": True})


async def approve(request: web.Request) -> web.Response:
    body = await request.json()
    code = secrets.token_urlsafe(16)
    STATE["codes"][code] = {
        "client_id": body["client_id"],
        "redirect_uri": body["redirect_uri"],
        "code_challenge": body["code_challenge"],
        "scope": body.get("scope", ""),
        "email": body["email"],
        "name": body.get("name", ""),
        "access_token": body.get("access_token") or secrets.token_urlsafe(24),
    }
    STATE["revoked"].discard(body["email"])
    return web.json_response({"code": code})


async def revoke(request: web.Request) -> web.Response:
    STATE["revoked"].add((await request.json())["email"])
    return web.json_response({"ok": True})


async def config(request: web.Request) -> web.Response:
    STATE["config"].update(await request.json())
    return web.json_response(STATE["config"])


async def hold(request: web.Request) -> web.Response:
    STATE["hold"].clear()
    return web.json_response({"ok": True})


async def release(request: web.Request) -> web.Response:
    STATE["hold"].set()
    return web.json_response({"ok": True})


async def held(request: web.Request) -> web.Response:
    return web.json_response({"held": STATE["held"]})


async def log(request: web.Request) -> web.Response:
    return web.json_response({"log": STATE["log"]})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


app = web.Application()
app.add_routes(
    [
        web.post("/token", token),
        web.post("/_admin/clients", clients),
        web.post("/_admin/approve", approve),
        web.post("/_admin/revoke", revoke),
        web.post("/_admin/config", config),
        web.post("/_admin/hold", hold),
        web.post("/_admin/release", release),
        web.get("/_admin/held", held),
        web.get("/_admin/log", log),
        web.get("/_admin/health", health),
    ]
)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
