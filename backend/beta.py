"""Invite-only beta identity, tenant context, and product event capture."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from urllib.request import Request as UrlRequest, urlopen
import json
from contextvars import ContextVar
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv('WIREUP_BETA_DB', ROOT / 'backend/data/beta.sqlite3'))
CURRENT_USER: ContextVar[str | None] = ContextVar('wireup_current_user', default=None)
CURRENT_WORKSPACE: ContextVar[str | None] = ContextVar('wireup_current_workspace', default=None)
SESSION_COOKIE = 'wireup_session'
SESSION_TTL = 60 * 60 * 24 * 14
COOKIE_SAMESITE = os.getenv('WIREUP_COOKIE_SAMESITE', 'lax').lower()
if COOKIE_SAMESITE not in ('lax', 'strict', 'none'):
    raise ValueError('WIREUP_COOKIE_SAMESITE must be lax, strict, or none.')


def enabled() -> bool:
    return os.getenv('WIREUP_BETA_AUTH_ENABLED', 'false').lower() == 'true'


def user_id() -> str | None:
    return CURRENT_USER.get()


def workspace_id() -> str | None:
    return CURRENT_WORKSPACE.get()


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
            workspace_id TEXT NOT NULL, created_at REAL NOT NULL, last_seen_at REAL
        );
        CREATE TABLE IF NOT EXISTS invites (
            email TEXT PRIMARY KEY, created_at REAL NOT NULL, invited_by TEXT NOT NULL,
            accepted_at REAL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, user_id TEXT, workspace_id TEXT,
            project_id TEXT, session_id TEXT, occurred_at REAL NOT NULL, properties TEXT
        );
        CREATE TABLE IF NOT EXISTS login_tokens (
            token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
    """)
    return connection


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _email(value: str) -> str:
    return value.strip().lower()


def _send_magic_link(email, token):
    api_key = os.getenv('RESEND_API_KEY', '').strip()
    sender = os.getenv('RESEND_FROM_EMAIL', '').strip()
    app_url = os.getenv('WIREUP_PUBLIC_APP_URL', 'http://127.0.0.1:5173').rstrip('/')
    if not api_key or not sender:
        return False
    link = f'{app_url}/?beta_token={token}'
    payload = json.dumps({
        'from': sender, 'to': [email], 'subject': 'Your WireUp beta access',
        'html': f'<p>Your WireUp beta access is ready.</p><p><a href="{link}">Open your WireUp workspace</a></p><p>This link expires in 15 minutes.</p>',
    }).encode()
    try:
        request = UrlRequest('https://api.resend.com/emails', data=payload,
                             headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                             method='POST')
        with urlopen(request, timeout=8):
            return True
    except Exception:
        return False


def identify(request: Request):
    if not enabled():
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, 'Sign in to access the beta.')
    with _db() as db:
        row = db.execute(
            'SELECT u.id, u.email, u.role, u.workspace_id FROM sessions s JOIN users u ON u.id=s.user_id '
            'WHERE s.token_hash=? AND s.expires_at>? ', (_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(401, 'Your beta session has expired. Sign in again.')
        db.execute('UPDATE users SET last_seen_at=? WHERE id=?', (time.time(), row['id']))
    CURRENT_USER.set(row['id'])
    CURRENT_WORKSPACE.set(row['workspace_id'])
    request.state.user = dict(row)
    return request.state.user


def record_event(name: str, *, project_id=None, session_id=None, properties=None):
    if not enabled() or not user_id():
        return
    import json
    from backend.monitoring import capture
    capture(name, {**(properties or {}), 'project_id': project_id, 'workspace_id': workspace_id()},
            distinct_id=user_id())
    with _db() as db:
        db.execute(
            'INSERT INTO events (id,name,user_id,workspace_id,project_id,session_id,occurred_at,properties) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (secrets.token_urlsafe(16), name, user_id(), workspace_id(), project_id, session_id,
             time.time(), json.dumps(properties or {}, separators=(',', ':'))))


class EmailPayload(BaseModel):
    email: str

    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        value = _email(value)
        if len(value) > 320 or '@' not in value or value.startswith('@') or value.endswith('@'):
            raise ValueError('Enter a valid email address.')
        return value


class EventPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r'^[a-z0-9_.-]+$')
    project_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    properties: dict = Field(default_factory=dict)


router = APIRouter(prefix='/api/beta', tags=['beta'])


@router.get('/session')
async def session(request: Request):
    if not enabled():
        return {'enabled': False, 'user': None}
    if not request.cookies.get(SESSION_COOKIE):
        return {'enabled': True, 'user': None}
    return {'enabled': True, 'user': identify(request)}


@router.post('/request-access')
async def request_access(payload: EmailPayload):
    email = _email(str(payload.email))
    with _db() as db:
        invite = db.execute('SELECT accepted_at FROM invites WHERE email=?', (email,)).fetchone()
        if invite is None and email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')):
            db.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                       (email, time.time(), 'bootstrap'))
            invite = {'accepted_at': None}
        if invite is None:
            raise HTTPException(403, 'This email is not invited to the beta.')
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO login_tokens VALUES (?,?,?,?)',
                   (_hash(token), email, time.time() + 900, time.time()))
        delivered = _send_magic_link(email, token)
        return {'accepted': bool(invite['accepted_at']), 'sent': delivered,
                'message': 'Check your email for a sign-in link.' if delivered else 'Email delivery is not configured yet.'}


@router.post('/invite')
async def invite(payload: EmailPayload, request: Request):
    actor = identify(request)
    if actor['role'] != 'admin':
        raise HTTPException(403, 'Admin access is required.')
    email = _email(str(payload.email))
    with _db() as db:
        db.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                   (email, time.time(), actor['id']))
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO login_tokens VALUES (?,?,?,?)',
                   (_hash(token), email, time.time() + 900, time.time()))
    return {'email': email, 'invited': True, 'sent': _send_magic_link(email, token)}


@router.post('/dev-login')
async def dev_login(payload: EmailPayload, response: Response):
    """Issue a session for an invited tester; production should replace this with an email provider."""
    if os.getenv('WIREUP_BETA_DEV_LOGIN', 'false').lower() != 'true':
        raise HTTPException(404, 'Development login is disabled.')
    email = _email(str(payload.email))
    with _db() as db:
        invite = db.execute('SELECT email FROM invites WHERE email=?', (email,)).fetchone()
        if invite is None and email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')):
            db.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                       (email, time.time(), 'bootstrap'))
            invite = {'email': email}
        if invite is None:
            raise HTTPException(403, 'This email is not invited to the beta.')
        row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if row is None:
            uid = secrets.token_urlsafe(12)
            wid = secrets.token_urlsafe(12)
            role = 'admin' if email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')) else 'tester'
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)', (uid, email, role, wid, time.time(), time.time()))
            user = {'id': uid, 'email': email, 'role': role, 'workspace_id': wid}
        else:
            user = dict(row)
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (_hash(token), user['id'], time.time() + SESSION_TTL, time.time()))
        db.execute('UPDATE invites SET accepted_at=COALESCE(accepted_at,?) WHERE email=?', (time.time(), email))
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=os.getenv('WIREUP_COOKIE_SECURE', 'false').lower() == 'true',
                        samesite=COOKIE_SAMESITE, max_age=SESSION_TTL)
    return {'user': user}


@router.post('/verify')
async def verify(payload: dict, response: Response):
    token = payload.get('token')
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        raise HTTPException(400, 'Invalid beta sign-in link.')
    with _db() as db:
        row = db.execute('SELECT email FROM login_tokens WHERE token_hash=? AND expires_at>?',
                         (_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(400, 'This beta sign-in link is expired or has already been used.')
        db.execute('DELETE FROM login_tokens WHERE token_hash=?', (_hash(token),))
        email = row['email']
        user_row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if user_row is None:
            user_id_value = secrets.token_urlsafe(12)
            workspace = secrets.token_urlsafe(12)
            role = 'admin' if email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')) else 'tester'
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)',
                       (user_id_value, email, role, workspace, time.time(), time.time()))
            user = {'id': user_id_value, 'email': email, 'role': role, 'workspace_id': workspace}
        else:
            user = dict(user_row)
        session_token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                   (_hash(session_token), user['id'], time.time() + SESSION_TTL, time.time()))
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True,
                        secure=os.getenv('WIREUP_COOKIE_SECURE', 'false').lower() == 'true',
                        samesite=COOKIE_SAMESITE, max_age=SESSION_TTL)
    return {'user': user}


@router.post('/logout')
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (_hash(token),))
    response.delete_cookie(SESSION_COOKIE)
    return {'ok': True}


@router.post('/events')
async def events(payload: EventPayload, request: Request):
    identify(request)
    record_event(payload.name, project_id=payload.project_id, session_id=payload.session_id,
                 properties=payload.properties)
    return {'accepted': True}


@router.get('/admin/summary')
async def summary(request: Request):
    actor = identify(request)
    if actor['role'] != 'admin':
        raise HTTPException(403, 'Admin access is required.')
    with _db() as db:
        users = db.execute('SELECT id,email,role,last_seen_at FROM users ORDER BY last_seen_at DESC').fetchall()
        counts = db.execute('SELECT name,COUNT(*) AS count FROM events GROUP BY name ORDER BY count DESC LIMIT 50').fetchall()
    return {'users': [dict(row) for row in users], 'events': [dict(row) for row in counts]}
