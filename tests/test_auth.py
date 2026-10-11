import pytest
from fastapi.testclient import TestClient

from scrapeflow import auth, config
from scrapeflow.web import app

ADMIN = {"name": "Admin", "email": "Admin@Example.com", "password": "correct horse", "confirm": "correct horse"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SCRAPEFLOW_AUTH_DISABLED", "false")
    config.get_settings.cache_clear()
    auth._failures.clear()
    with TestClient(app) as c:
        yield c


def test_first_start_creates_admin_then_requires_sign_in(client):
    # No account yet: every page leads to the setup page, the API answers 401.
    r = client.get("/sites/1?tab=files", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert client.get("/api/sites").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert "Set up your admin account" in client.get("/setup").text

    r = client.post("/setup", data={**ADMIN, "confirm": "different"})
    assert r.status_code == 422 and "match" in r.text
    r = client.post("/setup", data=ADMIN, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    home = client.get("/")
    assert home.status_code == 200 and "admin@example.com" in home.text and "v0.3.0" in home.text
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"  # only once

    # Signed out: pages redirect to the sign-in page and remember where you were going.
    client.post("/logout")
    r = client.get("/settings/storage", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/settings/storage"
    r = client.post("/login", data={"email": "admin@example.com", "password": "nope"})
    assert r.status_code == 401 and "Wrong e-mail or password" in r.text
    r = client.post("/login", data={"email": "ADMIN@example.com", "password": "correct horse",
                                    "next": "/settings/storage"}, follow_redirects=False)
    assert r.headers["location"] == "/settings/storage"
    r = client.post("/login", data={"email": "admin@example.com", "password": "correct horse",
                                    "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"  # no open redirect


def test_api_key_and_password_change(client):
    client.post("/setup", data=ADMIN)
    page = client.post("/settings/account", data={"action": "api_key"}).text
    key = page.split('id="api-key" value="')[1].split('"')[0]
    assert key.startswith("sf-")

    api = TestClient(app)
    assert api.get("/api/sites").status_code == 401
    assert api.get("/api/sites", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get("/api/sites", headers={"Authorization": f"Bearer {key}"}).status_code == 200

    r = client.post("/settings/account", data={"action": "password", "current": "bad", "password": "new password",
                                               "confirm": "new password"})
    assert r.status_code == 422 and "current password is wrong" in r.text
    other = TestClient(app)
    other.post("/login", data={"email": ADMIN["email"], "password": "correct horse"})
    assert other.get("/", follow_redirects=False).status_code == 200
    r = client.post("/settings/account", data={"action": "password", "current": "correct horse",
                                               "password": "new password", "confirm": "new password"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200  # this browser stays signed in
    assert other.get("/", follow_redirects=False).status_code == 303  # others are signed out

    client.post("/settings/account", data={"action": "revoke_api_key"})
    assert api.get("/api/sites", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_too_many_wrong_passwords_lock_the_client(client):
    client.post("/setup", data=ADMIN)
    client.post("/logout")
    for _ in range(auth.MAX_FAILURES):
        client.post("/login", data={"email": ADMIN["email"], "password": "wrong"})
    r = client.post("/login", data={"email": ADMIN["email"], "password": "correct horse"})
    assert r.status_code == 401 and "Too many attempts" in r.text


def test_password_hashing():
    stored = auth.hash_password("secret pw")
    assert stored.startswith("scrypt$") and "secret" not in stored
    assert auth.verify_password("secret pw", stored) and not auth.verify_password("other", stored)
    assert not auth.verify_password("x", "garbage")
