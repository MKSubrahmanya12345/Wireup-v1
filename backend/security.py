"""Production ingress guards; mount outside routing and exception middleware."""

import asyncio
import hmac
import ipaddress
import json
import os
import re
import threading
from urllib.parse import urlsplit

MAX_BODY_BYTES = 600000
DEFAULT_MAX_CONCURRENT_REQUESTS = 4
DOC_PATHS = frozenset(('/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect'))


def _positive_int(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a positive integer.') from None
    if result < 1:
        raise ValueError(f'{name} must be a positive integer.')
    return result


def _origin(value):
    if not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        port = parsed.port
        host = parsed.hostname.lower()
        if ':' in host:
            host = f'[{host}]'
        default_port = 443 if parsed.scheme == 'https' else 80
        return f'{parsed.scheme}://{host}' + (f':{port}' if port and port != default_port else '')
    except ValueError:
        return None


def _loopback_origin(value):
    parsed = urlsplit(value)
    if parsed.hostname == 'localhost':
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class SecurityMiddleware:
    """Pure ASGI guards with a per-process, fail-fast expensive-request budget."""

    def __init__(self, app, *, production=None, access_token=None, allowed_origins=None,
                 max_body_bytes=None, max_concurrent_requests=None, public_health=None,
                 body_timeout_seconds=None):
        self.app = app
        self.production = (os.getenv('WIREUP_DEPLOYMENT', '').lower() == 'production'
                           if production is None else bool(production))
        token = os.getenv('WIREUP_ACCESS_TOKEN', '') if access_token is None else access_token
        if token and (not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token) is None):
            raise ValueError('WIREUP_ACCESS_TOKEN must be a valid bearer token.')
        if self.production and not token:
            raise ValueError('WIREUP_ACCESS_TOKEN is required in production.')
        self._token = token.encode('ascii') if token else None
        origins = os.getenv('WIREUP_ALLOWED_ORIGINS', '').split(',') if allowed_origins is None else allowed_origins
        self.allowed_origins = frozenset()
        normalized = []
        for value in origins:
            value = value.strip()
            if not value:
                continue
            origin = _origin(value)
            if origin is None:
                raise ValueError('WIREUP_ALLOWED_ORIGINS must contain exact HTTP(S) origins, not wildcards or paths.')
            normalized.append(origin)
        self.allowed_origins = frozenset(normalized)
        self.max_body_bytes = _positive_int(
            os.getenv('WIREUP_MAX_BODY_BYTES', str(MAX_BODY_BYTES)) if max_body_bytes is None else max_body_bytes,
            'WIREUP_MAX_BODY_BYTES')
        if self.max_body_bytes > MAX_BODY_BYTES:
            raise ValueError('WIREUP_MAX_BODY_BYTES cannot exceed 600000 bytes.')
        self.max_concurrent_requests = _positive_int(
            os.getenv('WIREUP_MAX_CONCURRENT_REQUESTS', str(DEFAULT_MAX_CONCURRENT_REQUESTS))
            if max_concurrent_requests is None else max_concurrent_requests,
            'WIREUP_MAX_CONCURRENT_REQUESTS')
        health = os.getenv('WIREUP_PUBLIC_HEALTH', 'true') if public_health is None else public_health
        if isinstance(health, str):
            if health.lower() not in ('true', 'false'):
                raise ValueError('WIREUP_PUBLIC_HEALTH must be true or false.')
            health = health.lower() == 'true'
        self.public_health = bool(health)
        self.body_timeout_seconds = _positive_int(
            os.getenv('WIREUP_BODY_TIMEOUT_SECONDS', '30') if body_timeout_seconds is None else body_timeout_seconds,
            'WIREUP_BODY_TIMEOUT_SECONDS')
        self._active = 0
        self._lock = threading.Lock()

    def public_status(self):
        return {'production': self.production, 'authentication_required': self._token is not None}

    def _expensive(self, path):
        return (path == '/api/chat' or path.startswith('/api/chat/')
                or path in ('/api/compile', '/api/compiler')
                or path.startswith('/api/compiler/')
                or re.fullmatch(r'/api/hardware/project/[^/]+/command/?', path) is not None
                or (path.startswith('/api/') and path.rstrip('/').endswith('/compile')))

    def _headers(self, headers, no_store):
        replacements = {
            b'x-content-type-options': b'nosniff',
            b'x-frame-options': b'DENY',
            b'referrer-policy': b'no-referrer',
        }
        if no_store:
            replacements[b'cache-control'] = b'no-store'
            replacements[b'pragma'] = b'no-cache'
            replacements[b'expires'] = b'0'
        return [(key, value) for key, value in headers if key.lower() not in replacements] + list(replacements.items())

    async def _response(self, scope, send, status, detail, extra=()):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 4401 if status == 401 else 4403})
            return
        body = json.dumps(detail if isinstance(detail, dict) else {'detail': detail}).encode('utf-8')
        headers = self._headers([(b'content-type', b'application/json'),
                                 (b'content-length', str(len(body)).encode()), *extra], True)
        await send({'type': 'http.response.start', 'status': status, 'headers': headers})
        await send({'type': 'http.response.body', 'body': b'' if scope.get('method') == 'HEAD' else body})

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        api = path == '/api' or path.startswith('/api/')
        protected = api or path in DOC_PATHS or path.startswith('/docs/') or path.startswith('/redoc/')
        headers = {}
        for key, value in scope.get('headers', []):
            headers.setdefault(key.lower(), []).append(value)
        origin_values = headers.get(b'origin', [])
        trusted_origin = False
        if protected and origin_values:
            origin = _origin(origin_values[0].decode('latin-1')) if len(origin_values) == 1 else None
            trusted_origin = origin is not None and (origin in self.allowed_origins or _loopback_origin(origin))
            if not trusted_origin:
                await self._response(scope, send, 403, 'Origin is not allowed.')
                return
        safe_health = (scope['type'] == 'http' and path == '/api/health'
                       and scope.get('method') in ('GET', 'HEAD') and self.production and self.public_health)
        authenticated = False
        if protected and self._token is not None and not safe_health:
            authorization = headers.get(b'authorization', [])
            if len(authorization) == 1:
                parts = authorization[0].split(b' ')
                authenticated = (len(parts) == 2 and parts[0].lower() == b'bearer'
                                 and hmac.compare_digest(parts[1], self._token))
            if not authenticated:
                await self._response(scope, send, 401, 'Bearer authentication required.',
                                     ((b'www-authenticate', b'Bearer'),))
                return
        if safe_health:
            await self._response(scope, send, 200, {'status': 'ok', 'security': self.public_status()})
            return
        child_scope = dict(scope)
        # Only this trusted ingress writes the marker; headers cannot supply it.
        child_scope['wireup.security'] = {
            **self.public_status(), 'authenticated': authenticated,
            'trusted_origin': trusted_origin, 'origin_present': bool(origin_values),
        }

        async def secure_send(message):
            if message['type'] in ('http.response.start', 'websocket.accept'):
                message = {**message, 'headers': self._headers(message.get('headers', []), protected)}
                if trusted_origin and scope['type'] == 'http':
                    origin = origin_values[0]
                    message['headers'] = [
                        *message['headers'],
                        (b'access-control-allow-origin', origin),
                        (b'access-control-allow-credentials', b'true'),
                        (b'access-control-allow-headers', b'content-type, authorization'),
                        (b'access-control-allow-methods', b'GET, POST, PUT, PATCH, DELETE, OPTIONS'),
                        (b'vary', b'Origin'),
                    ]
            await send(message)

        if scope['type'] == 'websocket':
            await self.app(child_scope, receive, secure_send)
            return
        expensive = self._expensive(path)
        if expensive:
            with self._lock:
                admitted = self._active < self.max_concurrent_requests
                if admitted:
                    self._active += 1
            if not admitted:
                await self._response(scope, send, 429, 'Request concurrency limit reached.', ((b'retry-after', b'1'),))
                return
        try:
            lengths = headers.get(b'content-length', [])
            if lengths:
                if (len(lengths) != 1 or not re.fullmatch(rb'[0-9]+', lengths[0])
                        or b'transfer-encoding' in headers or len(lengths[0]) > 20):
                    await self._response(scope, send, 400, 'Invalid request framing.')
                    return
                if int(lengths[0]) > self.max_body_bytes:
                    await self._response(scope, send, 413, 'Request body exceeds the size limit.')
                    return
            body = bytearray()
            try:
                async with asyncio.timeout(self.body_timeout_seconds):
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return
                        if message['type'] != 'http.request':
                            continue
                        chunk = message.get('body', b'')
                        if len(body) + len(chunk) > self.max_body_bytes:
                            await self._response(scope, send, 413, 'Request body exceeds the size limit.')
                            return
                        body.extend(chunk)
                        if not message.get('more_body', False):
                            break
            except TimeoutError:
                await self._response(scope, send, 408, 'Request body timed out.')
                return
            if lengths and int(lengths[0]) != len(body):
                await self._response(scope, send, 400, 'Invalid request framing.')
                return
            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()

            await self.app(child_scope, replay_receive, secure_send)
        finally:
            if expensive:
                with self._lock:
                    self._active -= 1
