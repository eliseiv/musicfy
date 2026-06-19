from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_guest_sign_in_and_me(client):
    resp = await client.post("/v1/auth/guest", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["isGuest"] is True
    assert data["token"]
    token = data["token"]

    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["userId"] == data["userId"]
    assert me.json()["isGuest"] is True


@pytest.mark.asyncio
async def test_me_requires_token(client):
    resp = await client.get("/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_invalid_token_rejected(client):
    resp = await client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_SESSION"


@pytest.mark.asyncio
async def test_logout_revokes_session(client):
    token = (await client.post("/v1/auth/guest", json={})).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/v1/auth/me", headers=headers)).status_code == 200

    out = await client.post("/v1/auth/logout", headers=headers)
    assert out.status_code == 204
    assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_apple_sign_in_promotes_guest_and_merges(client, app):
    """Apple sign-in поверх guest-сессии: новый Apple-аккаунт промоутит guest,
    повторный вход существующего Apple мержит второго guest в тот же аккаунт."""
    from app.auth.sessions import AuthService

    class FakeAppleVerifier:
        async def verify(self, identity_token, *, nonce=None):
            return {"sub": identity_token, "email": "user@example.com"}

        async def aclose(self):
            pass

    # Подменяем verifier в работающем AuthService.
    app.state.auth_service = AuthService(
        app.state.sessionmaker,
        apple_verifier=FakeAppleVerifier(),
        session_ttl_seconds=3600,
    )

    # 1. guest
    guest = (await client.post("/v1/auth/guest", json={})).json()
    guest_id = guest["userId"]

    # 2. Apple sign-in поверх guest -> промоушн того же пользователя
    apple = (
        await client.post(
            "/v1/auth/apple",
            json={"identityToken": "apple-sub-123"},
            headers={"Authorization": f"Bearer {guest['token']}"},
        )
    ).json()
    assert apple["isGuest"] is False
    assert apple["userId"] == guest_id  # тот же user — guest промоутнут

    # 3. Второй guest + вход тем же Apple -> merge во существующий аккаунт
    guest2 = (await client.post("/v1/auth/guest", json={})).json()
    apple2 = (
        await client.post(
            "/v1/auth/apple",
            json={"identityToken": "apple-sub-123"},
            headers={"Authorization": f"Bearer {guest2['token']}"},
        )
    ).json()
    assert apple2["userId"] == guest_id  # вернулись в исходный Apple-аккаунт
