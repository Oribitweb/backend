"""
Covers app/api/.../login.py

Mocks the crud layer (get_user_by_username/email/phone, create_user,
authenticate) and auth_service token helpers, since we're testing route
behavior (status codes, cookie-setting, error branches), not password
hashing / DB uniqueness (that belongs in crud-level unit tests).
"""
import pytest
from unittest.mock import AsyncMock, patch


REGISTER_PAYLOAD = {
    "username": "newuser",
    "email": "new@example.com",
    "phone": "09129998877",
    "password": "StrongPass123!",
    "confirm_password": "StrongPass123!",
}


@pytest.mark.asyncio
async def test_register_success(client):
    with patch("app.api.endpoints.login.get_user_by_username", new=AsyncMock(return_value=None)), \
         patch("app.api.endpoints.login.get_user_by_email", new=AsyncMock(return_value=None)), \
         patch("app.api.endpoints.login.get_user_by_phone", new=AsyncMock(return_value=None)), \
         patch("app.api.endpoints.login.create_user", new=AsyncMock(return_value=None)), \
         patch("app.api.endpoints.login.get_password_hash", return_value="hashed"):
        response = await client.post("/register", json=REGISTER_PAYLOAD)
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_register_duplicate_username(client):
    with patch("app.api.endpoints.login.get_user_by_username", new=AsyncMock(return_value=object())):
        response = await client.post("/register", json=REGISTER_PAYLOAD)
    assert response.status_code == 422
    assert "Username" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    with patch("app.api.endpoints.login.get_user_by_username", new=AsyncMock(return_value=None)), \
         patch("app.api.endpoints.login.get_user_by_email", new=AsyncMock(return_value=object())):
        response = await client.post("/register", json=REGISTER_PAYLOAD)
    assert response.status_code == 422
    assert "Email" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_success_sets_cookies(client):
    fake_user = type("U", (), {"id": 1})()
    with patch("app.api.endpoints.login.authenticate", new=AsyncMock(return_value=fake_user)), \
         patch("app.api.endpoints.login.create_access_token", return_value="access-tok"), \
         patch("app.api.endpoints.login.create_refresh_token", return_value="refresh-tok"):
        response = await client.post("/login", json={"username": "a", "password": "b"})
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "access-tok"
    assert body["refresh_token"] == "refresh-tok"
    assert "access_token" in response.cookies
    assert "refresh_token" in response.cookies


@pytest.mark.asyncio
async def test_login_invalid_credentials(client):
    with patch("app.api.endpoints.login.authenticate", new=AsyncMock(return_value=None)):
        response = await client.post("/login", json={"username": "a", "password": "wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_unexpected_error_returns_401(client):
    with patch("app.api.endpoints.login.authenticate", new=AsyncMock(side_effect=RuntimeError("boom"))):
        response = await client.post("/login", json={"username": "a", "password": "b"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_success(client):
    fake_user = type("U", (), {"id": 1})()
    client.cookies.set("refresh_token", "valid-refresh")
    with patch("app.api.endpoints.login.decode_refresh_token", return_value=1), \
         patch("app.api.endpoints.login.get_user_by_id", new=AsyncMock(return_value=fake_user)), \
         patch("app.api.endpoints.login.create_access_token", return_value="new-access"):
        response = await client.post("/refresh")
    assert response.status_code == 200
    assert "access_token" in response.cookies


@pytest.mark.asyncio
async def test_refresh_user_not_found(client):
    client.cookies.set("refresh_token", "valid-refresh")
    with patch("app.api.endpoints.login.decode_refresh_token", return_value=999), \
         patch("app.api.endpoints.login.get_user_by_id", new=AsyncMock(return_value=None)):
        response = await client.post("/refresh")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_cookies(client):
    response = await client.post("/logout")
    assert response.status_code == 200
    # Response instructs client to delete cookies (Set-Cookie w/ Max-Age=0 or expired)
    set_cookie_headers = response.headers.get_list("set-cookie")
    assert any("access_token" in h for h in set_cookie_headers)
    assert any("refresh_token" in h for h in set_cookie_headers)