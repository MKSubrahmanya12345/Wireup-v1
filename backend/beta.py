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
CURRENT_SOURCE: ContextVar[str] = ContextVar('wireup_current_source', default='server')
SESSION_COOKIE = 'wireup_session'
SESSION_TTL = 60 * 60 * 24 * 14
INVITE_TTL = 60 * 60 * 24 * 7
COOKIE_SAMESITE = os.getenv('WIREUP_COOKIE_SAMESITE', 'lax').lower()
if COOKIE_SAMESITE not in ('lax', 'strict', 'none'):
    raise ValueError('WIREUP_COOKIE_SAMESITE must be lax, strict, or none.')


def _cookie_secure(request: Request) -> bool:
    """Use a Secure cookie on HTTPS or when explicitly configured.

    This keeps local HTTP development working while preserving production safety.
    """
    if os.getenv('WIREUP_COOKIE_SECURE', 'false').lower() == 'true':
        return True
    try:
        return getattr(request.url, 'scheme', '') == 'https'
    except Exception:
        return False


def _cookie_samesite(secure: bool) -> str:
    # Browsers reject SameSite=None cookies without the Secure attribute.
    if COOKIE_SAMESITE == 'none' and not secure:
        return 'lax'
    return COOKIE_SAMESITE


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
            project_id TEXT, session_id TEXT, occurred_at REAL NOT NULL, properties TEXT,
            run_id TEXT, source TEXT
        );
        CREATE TABLE IF NOT EXISTS login_tokens (
            token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
    """)
    _migrate(connection)
    return connection


def _migrate(connection):
    """Add analytics columns to databases created before the control room existed."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info(events)')}
    for column in ('run_id', 'source'):
        if column not in columns:
            connection.execute(f'ALTER TABLE events ADD COLUMN {column} TEXT')
    connection.executescript("""
        CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_user_time ON events(user_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_name_time ON events(name, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_project_time ON events(project_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at DESC);
    """)


# Public alias used by the admin analytics module.
db = _db


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _email(value: str) -> str:
    return value.strip().lower()


def _parse_users() -> dict[str, tuple[str, str]]:
    """Parse a simple username:password (or username:password:role) list from .env."""
    raw = os.getenv('WIREUP_USERS', 'admin:admin123')
    result: dict[str, tuple[str, str]] = {}
    for item in raw.split(','):
        item = item.strip()
        if not item or ':' not in item:
            continue
        parts = [part.strip() for part in item.split(':')]
        username = parts[0]
        password = parts[1]
        role = parts[2] if len(parts) > 2 else ('admin' if username == 'admin' else 'tester')
        if username:
            result[username] = (password, role)
    if 'admin' not in result:
        result['admin'] = ('admin123', 'admin')
    return result


def magic_link(token):
    app_url = os.getenv('WIREUP_PUBLIC_APP_URL', 'http://127.0.0.1:5173').rstrip('/')
    return f'{app_url}/?beta_token={token}'


def _send_magic_link(email, token, ttl='15 minutes'):
    api_key = os.getenv('RESEND_API_KEY', '').strip()
    sender = os.getenv('RESEND_FROM_EMAIL', '').strip()
    if not api_key or not sender:
        return False
    link = magic_link(token)
    payload = json.dumps({
        'from': sender, 'to': [email], 'subject': 'Your WireUp beta access',
        'html': f'<p>Your WireUp beta access is ready.</p><p><a href="{link}">Open your WireUp workspace</a></p><p>This link expires in {ttl}.</p>',
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


MAX_PROPERTY_STRING = 1000
MAX_PROPERTY_ITEMS = 40
MAX_PROPERTIES = 16000


def _redact(value, depth=0):
    """Bound and flatten event properties without ever raising on odd payloads."""
    if depth > 4:
        return '[truncated]'
    if isinstance(value, str):
        return value[:MAX_PROPERTY_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        clean = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_PROPERTY_ITEMS:
                clean['_truncated'] = True
                break
            clean[str(key)[:120]] = _redact(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        clean = [_redact(item, depth + 1) for item in items[:MAX_PROPERTY_ITEMS]]
        if len(items) > MAX_PROPERTY_ITEMS:
            clean.append('[truncated]')
        return clean
    return str(value)[:MAX_PROPERTY_STRING]


def record_event(name: str, *, project_id=None, session_id=None, run_id=None, properties=None,
                 user_id_override=None, workspace_id_override=None, source=None,
                 allow_anonymous=False, forward=True, occurred_at=None):
    """Persist one product event for the current beta actor.

    Anonymous events are reserved for pre-login flows (waitlist/access requests).
    ``forward=False`` keeps high-volume interaction events out of external
    observability while still making them available to the admin control room.
    """
    actor_id = user_id_override if user_id_override is not None else user_id()
    if not enabled() or (not actor_id and not allow_anonymous):
        return
    clean = _redact(properties or {})
    if not isinstance(clean, dict):
        clean = {'value': clean}
    encoded = json.dumps(clean, separators=(',', ':'), default=str)
    if len(encoded) > MAX_PROPERTIES:
        encoded = json.dumps({'_truncated': True}, separators=(',', ':'))
    actor_workspace = workspace_id_override if workspace_id_override is not None else workspace_id()
    actor_source = source or CURRENT_SOURCE.get()
    if forward:
        from backend.monitoring import capture
        capture(name, {**clean, 'project_id': project_id, 'workspace_id': actor_workspace,
                       'source': actor_source, 'run_id': run_id},
                distinct_id=actor_id)
    with _db() as connection:
        connection.execute(
            'INSERT INTO events (id,name,user_id,workspace_id,project_id,session_id,occurred_at,properties,run_id,source) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (secrets.token_urlsafe(16), name, actor_id, actor_workspace, project_id, session_id,
             occurred_at if occurred_at is not None else time.time(), encoded, run_id, actor_source))


class EmailPayload(BaseModel):
    email: str

    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        value = _email(value)
        if len(value) > 320 or '@' not in value or value.startswith('@') or value.endswith('@'):
            raise ValueError('Enter a valid email address.')
        return value


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class EventPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r'^[a-z0-9_.-]+$')
    project_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    properties: dict = Field(default_factory=dict)


router = APIRouter(prefix='/api/beta', tags=['beta'])


@router.get('/session')
async def session(request: Request):
    if not enabled():
        return {'enabled': False, 'user': None}
    dev_login = os.getenv('WIREUP_BETA_DEV_LOGIN', 'false').lower() == 'true'
    if not request.cookies.get(SESSION_COOKIE):
        return {'enabled': True, 'user': None, 'dev_login': dev_login}
    return {'enabled': True, 'user': identify(request), 'dev_login': dev_login}


@router.post('/request-access')
async def request_access(payload: EmailPayload, request: Request):
    email = _email(str(payload.email))
    with _db() as connection:
        invite = connection.execute('SELECT accepted_at FROM invites WHERE email=?', (email,)).fetchone()
        if invite is None and email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')):
            connection.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                               (email, time.time(), 'bootstrap'))
            invite = {'accepted_at': None}
        if invite is None:
            record_event('auth.access_requested', source='auth', allow_anonymous=True,
                         properties={'email': email, 'invited': False, 'delivered': False,
                                     'user_agent': request.headers.get('user-agent', '')[:300]})
            raise HTTPException(403, 'This email is not invited to the beta.')
        token = secrets.token_urlsafe(32)
        connection.execute('INSERT INTO login_tokens VALUES (?,?,?,?)',
                           (_hash(token), email, time.time() + 900, time.time()))
        delivered = _send_magic_link(email, token)
    record_event('auth.access_requested', source='auth', allow_anonymous=True,
                 properties={'email': email, 'invited': True, 'delivered': delivered,
                             'accepted': bool(invite['accepted_at']),
                             'user_agent': request.headers.get('user-agent', '')[:300]})
    return {'accepted': bool(invite['accepted_at']), 'sent': delivered,
            'message': 'Check your email for a sign-in link.' if delivered else 'Email delivery is not configured yet.'}


@router.post('/invite')
async def invite(payload: EmailPayload, request: Request):
    actor = identify(request)
    if actor['role'] != 'admin':
        raise HTTPException(403, 'Admin access is required.')
    email = _email(str(payload.email))
    with _db() as connection:
        connection.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                           (email, time.time(), actor['id']))
        token = secrets.token_urlsafe(32)
        connection.execute('INSERT INTO login_tokens VALUES (?,?,?,?)',
                           (_hash(token), email, time.time() + INVITE_TTL, time.time()))
    sent = _send_magic_link(email, token, '7 days')
    record_event('auth.invite_sent', source='auth',
                 properties={'email': email, 'delivered': sent, 'ttl_days': 7})
    # Email is optional for a zero-budget beta: the admin can copy the link and share it directly.
    return {'email': email, 'invited': True, 'sent': sent, 'link': magic_link(token)}


@router.post('/dev-login')
async def dev_login(payload: EmailPayload, request: Request, response: Response):
    """Issue a session for an invited tester; production should replace this with an email provider."""
    if os.getenv('WIREUP_BETA_DEV_LOGIN', 'false').lower() != 'true':
        raise HTTPException(404, 'Development login is disabled.')
    email = _email(str(payload.email))
    with _db() as connection:
        invite = connection.execute('SELECT email FROM invites WHERE email=?', (email,)).fetchone()
        if invite is None and email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')):
            connection.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                               (email, time.time(), 'bootstrap'))
            invite = {'email': email}
        if invite is None:
            raise HTTPException(403, 'This email is not invited to the beta.')
        row = connection.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if row is None:
            uid = secrets.token_urlsafe(12)
            wid = secrets.token_urlsafe(12)
            role = 'admin' if email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')) else 'tester'
            connection.execute('INSERT INTO users VALUES (?,?,?,?,?,?)', (uid, email, role, wid, time.time(), time.time()))
            user = {'id': uid, 'email': email, 'role': role, 'workspace_id': wid}
        else:
            user = dict(row)
        token = secrets.token_urlsafe(32)
        connection.execute('INSERT INTO sessions VALUES (?,?,?,?)', (_hash(token), user['id'], time.time() + SESSION_TTL, time.time()))
        connection.execute('UPDATE invites SET accepted_at=COALESCE(accepted_at,?) WHERE email=?', (time.time(), email))
    secure = _cookie_secure(request)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure,
                        samesite=_cookie_samesite(secure), max_age=SESSION_TTL)
    record_event('auth.login', source='auth', user_id_override=user['id'],
                 workspace_id_override=user['workspace_id'],
                 properties={'method': 'dev_login', 'user_agent': request.headers.get('user-agent', '')[:300]})
    return {'user': user}


@router.post('/login')
async def login(payload: LoginPayload, request: Request, response: Response):
    users = _parse_users()
    credentials = users.get(payload.username)
    if credentials is None or credentials[0] != payload.password:
        raise HTTPException(401, 'Invalid username or password.')
    role = credentials[1]
    with _db() as connection:
        row = connection.execute('SELECT * FROM users WHERE id=?', (payload.username,)).fetchone()
        if row is None:
            workspace_id = 'admin' if payload.username == 'admin' else secrets.token_urlsafe(12)
            connection.execute('INSERT INTO users VALUES (?,?,?,?,?,?)',
                               (payload.username, payload.username, role, workspace_id, time.time(), time.time()))
            user = {'id': payload.username, 'email': payload.username, 'role': role, 'workspace_id': workspace_id}
        else:
            user = dict(row)
            connection.execute('UPDATE users SET last_seen_at=? WHERE id=?', (time.time(), user['id']))
    token = secrets.token_urlsafe(32)
    with _db() as connection:
        connection.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (_hash(token), user['id'], time.time() + SESSION_TTL, time.time()))
    secure = _cookie_secure(request)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure,
                        samesite=_cookie_samesite(secure), max_age=SESSION_TTL)
    record_event('auth.login', source='auth', user_id_override=user['id'],
                 workspace_id_override=user['workspace_id'],
                 properties={'method': 'password', 'user_agent': request.headers.get('user-agent', '')[:300]})
    return {'user': user}


@router.post('/verify')
async def verify(payload: dict, request: Request, response: Response):
    token = payload.get('token')
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        raise HTTPException(400, 'Invalid beta sign-in link.')
    with _db() as connection:
        row = connection.execute('SELECT email FROM login_tokens WHERE token_hash=? AND expires_at>?',
                                 (_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(400, 'This beta sign-in link is expired or has already been used.')
        connection.execute('DELETE FROM login_tokens WHERE token_hash=?', (_hash(token),))
        email = row['email']
        user_row = connection.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if user_row is None:
            user_id_value = secrets.token_urlsafe(12)
            workspace = secrets.token_urlsafe(12)
            role = 'admin' if email == _email(os.getenv('WIREUP_ADMIN_EMAIL', '')) else 'tester'
            connection.execute('INSERT INTO users VALUES (?,?,?,?,?,?)',
                               (user_id_value, email, role, workspace, time.time(), time.time()))
            user = {'id': user_id_value, 'email': email, 'role': role, 'workspace_id': workspace}
        else:
            user = dict(user_row)
        session_token = secrets.token_urlsafe(32)
        connection.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (_hash(session_token), user['id'], time.time() + SESSION_TTL, time.time()))
    secure = _cookie_secure(request)
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True,
                        secure=secure, samesite=_cookie_samesite(secure), max_age=SESSION_TTL)
    record_event('auth.login', source='auth', user_id_override=user['id'],
                 workspace_id_override=user['workspace_id'],
                 properties={'method': 'magic_link', 'user_agent': request.headers.get('user-agent', '')[:300]})
    return {'user': user}


@router.post('/logout')
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    actor = None
    if token:
        with _db() as connection:
            row = connection.execute('SELECT user_id FROM sessions WHERE token_hash=?', (_hash(token),)).fetchone()
            if row:
                user_row = connection.execute('SELECT id,workspace_id FROM users WHERE id=?', (row['user_id'],)).fetchone()
                actor = dict(user_row) if user_row else None
            connection.execute('DELETE FROM sessions WHERE token_hash=?', (_hash(token),))
    if actor:
        record_event('auth.logout', source='auth', user_id_override=actor['id'],
                     workspace_id_override=actor['workspace_id'])
    response.delete_cookie(SESSION_COOKIE)
    return {'ok': True}


@router.post('/events')
async def events(payload: EventPayload, request: Request):
    identify(request)
    record_event(payload.name, project_id=payload.project_id, session_id=payload.session_id,
                 run_id=payload.run_id, properties=payload.properties, source='web', forward=False)
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


def issue_login_link(email, ttl_seconds=INVITE_TTL):
    """Mint a one-time sign-in link locally, for operators without an email provider."""
    email = _email(email)
    if '@' not in email or email.startswith('@') or email.endswith('@'):
        raise ValueError('Enter a valid email address.')
    token = secrets.token_urlsafe(32)
    with _db() as db:
        db.execute('INSERT OR IGNORE INTO invites (email,created_at,invited_by) VALUES (?,?,?)',
                   (email, time.time(), 'cli'))
        db.execute('INSERT INTO login_tokens VALUES (?,?,?,?)',
                   (_hash(token), email, time.time() + ttl_seconds, time.time()))
    return magic_link(token)


if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    load_dotenv(ROOT / '.env')
    if len(sys.argv) != 3 or sys.argv[1] not in ('link', 'invite'):
        print('usage: python -m backend.beta link <email>')
        raise SystemExit(2)
    try:
        print(issue_login_link(sys.argv[2]))
    except ValueError as error:
        print(error)
        raise SystemExit(2)
