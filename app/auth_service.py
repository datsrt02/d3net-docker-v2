from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

DATA_DIR = Path('/app/data')
USERS_PATH = DATA_DIR / 'users.json'
SESSION_TTL_SECONDS = 24 * 60 * 60

_sessions: dict[str, str] = {}


def _hash_password(password: str, salt: str | None = None) -> dict[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 200_000)
    return {'salt': salt, 'hash': digest.hex(), 'algorithm': 'pbkdf2_sha256', 'iterations': '200000'}


def _ensure_users() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if USERS_PATH.exists():
        try:
            return json.loads(USERS_PATH.read_text())
        except Exception:
            pass
    data = {'users': {'admin': _hash_password('admin')}}
    USERS_PATH.write_text(json.dumps(data, indent=2))
    return data


def _save_users(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_PATH.write_text(json.dumps(data, indent=2))


def verify_password(username: str, password: str) -> bool:
    data = _ensure_users()
    record = (data.get('users') or {}).get(username)
    if not record:
        return False
    computed = _hash_password(password, record.get('salt'))
    return hmac.compare_digest(computed['hash'], record.get('hash', ''))


def change_password(username: str, current_password: str, new_password: str) -> tuple[bool, str]:
    if username != 'admin':
        return False, 'Only admin user is supported'
    if len(new_password or '') < 4:
        return False, 'New password must be at least 4 characters'
    if not verify_password(username, current_password):
        return False, 'Current password is incorrect'
    data = _ensure_users()
    data.setdefault('users', {})[username] = _hash_password(new_password)
    _save_users(data)
    return True, 'Password updated'


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = username
    return token


def get_session_user(token: str | None) -> str | None:
    if not token:
        return None
    return _sessions.get(token)


def delete_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)
