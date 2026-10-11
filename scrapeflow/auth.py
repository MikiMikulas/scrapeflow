"""Local accounts: password hashing, browser sessions and API keys.

The first visit creates the admin account. Browsers then use a session
cookie; scripts send ``Authorization: Bearer <API key>``.
"""

import base64
import hashlib
import hmac
import secrets
import time
from datetime import timedelta

from fastapi import Request
from sqlmodel import Session, func, select

from .models import AuthSession, User, utcnow

COOKIE = "scrapeflow_session"
SESSION_DAYS = 30
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


def password_problem(password: str, confirm: str | None = None) -> str | None:
    if len(password) < 8:
        return "The password must have at least 8 characters"
    if confirm is not None and password != confirm:
        return "The passwords don't match"
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- users & sessions


def has_users(session: Session) -> bool:
    return session.exec(select(func.count(User.id))).one() > 0


def create_user(session: Session, *, email: str, name: str, password: str) -> User:
    user = User(email=email.strip().lower(), name=name.strip() or email.split("@")[0],
                password_hash=hash_password(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.exec(select(User).where(User.email == email.strip().lower())).first()
    if user is None:
        hash_password(password)  # same work either way, so timing doesn't reveal accounts
        return None
    return user if verify_password(password, user.password_hash) else None


def start_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(token_hash=_token_hash(token), user_id=user.id,
                            expires_at=utcnow() + timedelta(days=SESSION_DAYS)))
    user.last_login_at = utcnow()
    session.add(user)
    session.commit()
    return token


def end_session(session: Session, token: str | None) -> None:
    if token and (row := session.get(AuthSession, _token_hash(token))):
        session.delete(row)
        session.commit()


def end_all_sessions(session: Session, user: User, keep: str | None = None) -> None:
    keep_hash = _token_hash(keep) if keep else None
    for row in session.exec(select(AuthSession).where(AuthSession.user_id == user.id)):
        if row.token_hash != keep_hash:
            session.delete(row)
    session.commit()


def new_api_key(session: Session, user: User) -> str:
    key = "sf-" + secrets.token_urlsafe(32)
    user.api_key_hash, user.api_key_hint = _token_hash(key), key[-4:]
    session.add(user)
    session.commit()
    return key


def current_user(request: Request, session: Session) -> User | None:
    """The user behind the request's session cookie or ``Authorization: Bearer`` API key."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        key = header[7:].strip()
        return session.exec(select(User).where(User.api_key_hash == _token_hash(key))).first() if key else None
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = session.get(AuthSession, _token_hash(token))
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)
    if expires < utcnow():
        session.delete(row)
        session.commit()
        return None
    return session.get(User, row.user_id)


# --------------------------------------------------------------------------- brute-force protection

_failures: dict[str, tuple[int, float]] = {}
MAX_FAILURES, LOCK_SECONDS = 5, 60


def locked_for(ip: str) -> int:
    """Seconds this client still has to wait after too many wrong passwords."""
    count, since = _failures.get(ip, (0, 0.0))
    if count < MAX_FAILURES:
        return 0
    left = int(since + LOCK_SECONDS - time.monotonic())
    if left <= 0:
        _failures.pop(ip, None)
        return 0
    return left


def record_failure(ip: str) -> None:
    count, _ = _failures.get(ip, (0, 0.0))
    _failures[ip] = (count + 1, time.monotonic())


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
