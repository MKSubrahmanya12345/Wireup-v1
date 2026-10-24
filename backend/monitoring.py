"""Optional error, product, and model monitoring integrations.

All integrations are disabled unless their endpoint/key is configured. Monitoring
must never become a dependency of the product request path.
"""

import json
import os
import threading
import time
import traceback
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix='/api/monitoring', tags=['monitoring'])

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None

if sentry_sdk and os.getenv('SENTRY_DSN', '').strip():
    sentry_sdk.init(dsn=os.getenv('SENTRY_DSN'), environment=os.getenv('WIREUP_ENV', 'beta'),
                    traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0.1')))


def _post(url, payload, headers=None):
    try:
        body = json.dumps(payload, separators=(',', ':')).encode()
        request = UrlRequest(url, data=body, headers={'Content-Type': 'application/json', **(headers or {})}, method='POST')
        with urlopen(request, timeout=3):
            return True
    except Exception:
        return False


def _async_post(url, payload, headers=None):
    if not url:
        return
    threading.Thread(target=_post, args=(url, payload, headers), daemon=True).start()


def capture(name, properties=None, *, distinct_id=None):
    """Send a redacted structured event to configured PostHog and Langfuse."""
    props = {key: value for key, value in (properties or {}).items()
             if key not in {'prompt', 'content', 'source', 'api_key', 'runtime_token'}}
    posthog_key = os.getenv('POSTHOG_API_KEY', '').strip()
    posthog_host = os.getenv('POSTHOG_HOST', 'https://app.posthog.com').rstrip('/')
    if posthog_key:
        _async_post(f'{posthog_host}/capture/', {
            'api_key': posthog_key, 'event': name, 'distinct_id': distinct_id or 'anonymous',
            'properties': {'source': 'wireup', **props},
        })
    langfuse_key = os.getenv('LANGFUSE_PUBLIC_KEY', '').strip()
    langfuse_secret = os.getenv('LANGFUSE_SECRET_KEY', '').strip()
    langfuse_host = os.getenv('LANGFUSE_HOST', 'https://cloud.langfuse.com').rstrip('/')
    if langfuse_key and langfuse_secret:
        import base64
        auth = base64.b64encode(f'{langfuse_key}:{langfuse_secret}'.encode()).decode()
        _async_post(f'{langfuse_host}/api/public/ingestion', {
            'batch': [{'id': f'wireup-{time.time_ns()}', 'type': 'event-create',
                       'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                       'body': {'name': name, 'metadata': props,
                                'userId': distinct_id or 'anonymous'}}],
        }, {'Authorization': f'Basic {auth}'})


def capture_error(error, *, path=None, user_id=None):
    details = ''.join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:]
    capture('wireup.error', {'path': path or '', 'error_type': type(error).__name__, 'traceback': details},
            distinct_id=user_id)
    if sentry_sdk:
        sentry_sdk.set_user({'id': user_id} if user_id else None)
        sentry_sdk.capture_exception(error)


class MonitorEvent(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r'^[a-z0-9_.-]+$')
    properties: dict = Field(default_factory=dict)
    project_id: str | None = Field(default=None, max_length=128)


@router.post('/event')
async def event(payload: MonitorEvent, request: Request):
    user = getattr(request.state, 'user', None)
    capture(payload.name, {**payload.properties, 'project_id': payload.project_id},
            distinct_id=user.get('id') if user else None)
    return {'accepted': True}


@router.get('/status')
async def status(request: Request):
    user = getattr(request.state, 'user', None)
    if not user or user.get('role') != 'admin':
        return {'enabled': False}
    return {
        'posthog': bool(os.getenv('POSTHOG_API_KEY', '').strip()),
        'langfuse': bool(os.getenv('LANGFUSE_PUBLIC_KEY', '').strip() and os.getenv('LANGFUSE_SECRET_KEY', '').strip()),
        'sentry': bool(os.getenv('SENTRY_DSN', '').strip()),
    }
