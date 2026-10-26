import asyncio
from contextlib import aclosing, asynccontextmanager
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from threading import Lock
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator, field_validator
from backend.agent import stream_agent
from backend.models import model_options, build_model
from backend.bedrock_model import bedrock_configured
from backend.plain_chat import stream_plain_chat
from backend.hardware import router as hardware_router, local_connection
from backend.sample_projects import router as sample_router
from backend.security import SecurityMiddleware
from backend import admin
from backend import beta
from backend import monitoring

ENV_PATH = Path(__file__).resolve().parents[1] / '.env'
load_dotenv(ENV_PATH)


def reload_settings():
    # Local configuration edits must replace credentials cached at process startup.
    load_dotenv(ENV_PATH, override=True)

SYSTEM_PROMPT = 'You are WireUp, a helpful AI assistant. Give accurate, clear answers. Use Markdown when helpful. Be honest about uncertainty and capabilities.'


class ChatMessage(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=16000)


class FeasibilityApproval(BaseModel):
    assessment_id: str = Field(min_length=32, max_length=32, pattern=r'^[0-9a-f]+$')
    choice: Literal['hardware_only', 'cancel', 'revise']


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=128)
    runtime_token: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    chat_id: str | None = Field(default=None, max_length=128)
    project_answers: dict[str, str] | None = None
    approval: FeasibilityApproval | None = None
    provider: Literal['groq', 'nvidia', 'bedrock', 'azure', 'sarvam', 'openrouter'] = 'groq'
    model: str | None = Field(default=None, min_length=1, max_length=256, pattern=r'^[^\s\x00-\x1f]+$')

    @field_validator('project_answers')
    @classmethod
    def validate_answers(cls, answers):
        if answers is not None and (not 1 <= len(answers) <= 20 or any(not key or len(key) > 80 or not value or len(value) > 1000 for key, value in answers.items())):
            raise ValueError('Invalid project questionnaire answers.')
        return answers

    @model_validator(mode='after')
    def validate_history(self):
        if self.messages[-1].role != 'user' or self.messages[0].role != 'user':
            raise ValueError('Conversation must begin and end with a user message.')
        if any(not message.content.strip() for message in self.messages):
            raise ValueError('Messages cannot be blank.')
        if sum(len(message.content) for message in self.messages) > 500000:
            raise ValueError('Conversation is too large.')
        return self


def safe_error(error: Exception) -> str:
    name = type(error).__name__.lower()
    detail = str(error).lower()
    status = getattr(error, 'status_code', None)
    if 'authentication' in name or status == 401 or 'invalid_api_key' in detail:
        return 'Groq authentication failed. Check GROQ_API_KEY in .env, then retry.'
    if 'permission' in name or status == 403:
        return 'Groq has denied access. Check your project permissions and model access in the Groq console.'
    if 'ratelimit' in name or status == 429 or 'rate_limit_exceeded' in detail:
        return 'Groq rate limit reached. Wait a moment or check your limits at https://console.groq.com/settings/limits, then retry.'
    if 'notfound' in name or status == 404 or any(code in detail for code in ('model_not_found', 'model_decommissioned')):
        return 'The configured Groq model is unavailable. Set GROQ_MODEL to an active model in .env, then retry.'
    if isinstance(error, TimeoutError) or 'timeout' in name:
        return 'Groq took too long to respond. Please try again.'
    if 'connection' in name:
        return 'Could not connect to Groq. Check your network connection and retry.'
    return 'Groq could not complete this response. Check the model and connection, then retry.'


def event(kind: str, **values) -> str:
    return json.dumps({'type': kind, **values}) + '\n'


def create_app(model_factory=None, api_key=None):
    model_cache = {}
    model_lock = Lock()
    model_loop = None

    def cached_model(provider, key, model_name, loop):
        nonlocal model_loop

        def signature():
            # SDKs also read implicit proxy, endpoint, and credential settings.
            settings = json.dumps((key, model_name, sorted(os.environ.items())))
            return build_model, hashlib.sha256(settings.encode()).digest()

        with model_lock:
            # Async provider transports must not cross event loops.
            if model_loop is not loop:
                model_cache.clear()
                model_loop = loop
            current = signature()
            cached = model_cache.get(provider)
            if cached is not None and cached[0] == current:
                return cached[1]
            model_cache.pop(provider, None)
            model = build_model(provider, key, model_name)
            if signature() == current:
                model_cache[provider] = (current, model)
            return model

    @asynccontextmanager
    async def lifespan(application):
        nonlocal model_loop
        try:
            yield
        finally:
            with model_lock:
                model_cache.clear()
                model_loop = None
            from backend.remote_projects import remote_projects
            await remote_projects.shutdown()

    application = FastAPI(title='WireUp LangChain API', lifespan=lifespan)
    application.add_middleware(SecurityMiddleware)
    application.state.model_factory = model_factory
    application.include_router(hardware_router)
    application.include_router(sample_router)
    application.include_router(beta.router)
    application.include_router(admin.router)
    application.include_router(monitoring.router)

    @application.middleware('http')
    async def beta_auth(request: Request, call_next):
        if request.method == 'OPTIONS':
            response = Response(status_code=204)
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') in {
                allowed.strip().rstrip('/')
                for allowed in os.getenv('WIREUP_ALLOWED_ORIGINS', '').split(',')
                if allowed.strip()
            }:
                response.headers.update({
                    'Access-Control-Allow-Origin': origin,
                    'Access-Control-Allow-Credentials': 'true',
                    'Access-Control-Allow-Headers': 'content-type, authorization',
                    'Access-Control-Allow-Methods': 'GET, POST, PUT, PATCH, DELETE, OPTIONS',
                    'Vary': 'Origin',
                })
            return response
        if beta.enabled() and request.url.path.startswith('/api/') and not (
            request.url.path in ('/api/health', '/api/beta/session', '/api/beta/request-access',
                                 '/api/beta/dev-login', '/api/beta/verify', '/api/beta/login', '/api/beta/logout')
            or request.url.path.startswith('/api/beta/invite')
        ):
            try:
                beta.identify(request)
            except HTTPException as error:
                return JSONResponse({'detail': error.detail}, status_code=error.status_code,
                                    headers={'Cache-Control': 'no-store'})
        try:
            response = await call_next(request)
            return response
        except Exception as error:
            monitoring.capture_error(error, path=request.url.path, user_id=beta.user_id())
            raise

    @application.get('/api/health')
    async def health():
        reload_settings()
        key = api_key if api_key is not None else os.getenv('GROQ_API_KEY', '')
        return {'configured': bool(key.strip()), 'model': os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b'), 'providers': model_options()}

    @application.get('/api/models')
    async def models():
        reload_settings()
        from backend.model_discovery import discover_models
        return await discover_models()

    @application.post('/api/chat')
    async def chat(payload: ChatRequest, request: Request):
        if not local_connection(request):
            raise HTTPException(403, 'The local hardware agent is restricted to loopback clients.')
        reload_settings()
        provider = payload.provider
        run_id = payload.run_id or secrets.token_urlsafe(12)
        actor_id, actor_workspace = beta.user_id(), beta.workspace_id()
        prompt = payload.messages[-1].content
        key_name = {'azure': 'AZURE_API_KEY', 'nvidia': 'NVIDIA_API_KEY', 'bedrock': 'BEDROCK_API_KEY', 'groq': 'GROQ_API_KEY', 'sarvam': 'SARVAM_API_KEY', 'openrouter': 'OPENROUTER_API_KEY'}[provider]
        key = api_key if api_key is not None else os.getenv(key_name, '')
        model_name = (
            os.getenv('AZURE_MODEL_ID', 'gpt-6.1-sol') if provider == 'azure'
            else os.getenv('BEDROCK_MODEL_ID', 'moonshotai.kimi-k2.5') if provider == 'bedrock'
            else os.getenv('NVIDIA_MODEL', 'z-ai/glm-5.3') if provider == 'nvidia'
            else os.getenv('SARVAM_MODEL', 'sarvam-105b') if provider == 'sarvam'
            else os.getenv('OPENROUTER_MODEL', 'openai/gpt-oss-120b:free') if provider == 'openrouter'
            else os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')
        )
        model_name = payload.model or model_name
        if not key.strip() and not (provider == 'bedrock' and bedrock_configured()):
            beta.record_event('ai.run_failed', project_id=payload.project_id, session_id=payload.chat_id,
                              run_id=run_id, source='server', forward=False,
                              user_id_override=actor_id, workspace_id_override=actor_workspace,
                              properties={'provider': provider, 'model': model_name,
                                          'status': 'error', 'reason': 'missing_key',
                                          'prompt_preview': prompt[:500], 'prompt_length': len(prompt)})
            raise HTTPException(503, f'Add {key_name} to .env, then retry to enable {provider.upper()} replies.')
        beta.record_event('ai.run_started', project_id=payload.project_id, session_id=payload.chat_id,
                          run_id=run_id, source='server', forward=False,
                          user_id_override=actor_id, workspace_id_override=actor_workspace,
                          properties={'provider': provider, 'model': model_name, 'chat_id': payload.chat_id,
                                      'prompt_preview': prompt[:500], 'prompt_length': len(prompt)})
        selected = []
        count = 0
        for message in reversed(payload.messages):
            if count + len(message.content) > 48000:
                break
            selected.append(message)
            count += len(message.content)
        selected.reverse()
        while selected and selected[0].role != 'user':
            selected.pop(0)
        # Merge adjacent roles after an interrupted assistant turn was omitted.
        history = [SystemMessage(content=SYSTEM_PROMPT)]
        for message in selected:
            cls = HumanMessage if message.role == 'user' else AIMessage
            if len(history) > 1 and isinstance(history[-1], cls):
                history[-1].content += '\n\n' + message.content
            else:
                history.append(cls(content=message.content))

        async def generate():
            started = time.monotonic()
            recorded = False

            def finish(status, reason=None, final=None):
                nonlocal recorded
                if recorded:
                    return
                recorded = True
                properties = {
                    'provider': provider, 'model': model_name, 'status': status, 'reason': reason,
                    'duration_ms': int((time.monotonic() - started) * 1000),
                    'prompt_preview': prompt[:500], 'prompt_length': len(prompt),
                }
                if final:
                    properties['duration_ms'] = int(final.get('elapsedMs') or properties['duration_ms'])
                    properties['tool_calls'] = int(final.get('toolCalls') or 0)
                    properties['model_calls'] = int(final.get('modelCalls') or 0)
                    properties['changes'] = final.get('changes') or {}
                    if final.get('usage'):
                        properties['usage'] = final['usage']
                beta.record_event('ai.run_failed' if status == 'error' else 'ai.run_completed',
                                  project_id=payload.project_id, session_id=payload.chat_id,
                                  run_id=run_id, source='server', forward=False,
                                  user_id_override=actor_id, workspace_id_override=actor_workspace,
                                  properties=properties)

            yield event('heartbeat')
            try:
                try:
                    async with asyncio.timeout(20):
                        model = await asyncio.to_thread(application.state.model_factory) if application.state.model_factory else await asyncio.to_thread(cached_model, provider, key, model_name, asyncio.get_running_loop())
                except Exception:
                    finish('error', 'model_initialization_failed')
                    monitoring.capture('ai_run_failed', {'provider': provider, 'model': model_name,
                                                         'duration_ms': int((time.monotonic() - started) * 1000)},
                                       distinct_id=actor_id)
                    yield event('text', channel='narration', text=f'{provider.upper()} model initialization failed or timed out. Check its API settings and retry.')
                    yield event('done', status='error')
                    return
                if len(selected) < len(payload.messages):
                    yield event('text', channel='narration', text='Older messages were omitted from model context.\n')
                plain = not payload.project_id and not any(word in payload.messages[-1].content.lower() for word in ('calculate', 'calculator', 'compute')) and application.state.model_factory is None
                stream = stream_plain_chat(model, history, timeout_seconds=300 if provider == 'bedrock' else 30) if plain else stream_agent(model, history, payload.project_id, payload.runtime_token, requirements=payload.project_answers, approval=payload.approval.model_dump() if payload.approval else None, run_id=run_id, chat_id=payload.chat_id)
                async with aclosing(stream) as agent:
                    pending = asyncio.create_task(anext(agent, None))
                    try:
                        while True:
                            done, _ = await asyncio.wait({pending}, timeout=10)
                            if not done:
                                if await request.is_disconnected():
                                    finish('cancelled', 'client_disconnected')
                                    return
                                yield event('heartbeat')
                                continue
                            item = pending.result()
                            if item is None:
                                finish('cancelled', 'client_disconnected')
                                return
                            if await request.is_disconnected():
                                finish('cancelled', 'client_disconnected')
                                return
                            if provider in ('nvidia', 'azure') and item.get('type') == 'text' and item.get('channel') == 'narration':
                                text = item.get('text', '').replace('Groq', 'NVIDIA' if provider == 'nvidia' else 'Azure')
                                if provider == 'nvidia' and 'model could not complete' in text.lower():
                                    text = 'NVIDIA could not complete this request. Check your key and model access in the NVIDIA API catalog.'
                                item = {**item, 'text': text}
                            yield json.dumps(item, ensure_ascii=False) + '\n'
                            if item.get('type') == 'done':
                                finish(item.get('status') or 'success', item.get('reason'), item)
                                monitoring.capture('ai_run_completed', {
                                    'provider': provider, 'model': model_name,
                                    'duration_ms': int((time.monotonic() - started) * 1000),
                                    'status': item.get('status', 'success'),
                                }, distinct_id=actor_id)
                                return
                            pending = asyncio.create_task(anext(agent, None))
                    finally:
                        if not pending.done():
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
            except asyncio.CancelledError:
                finish('cancelled', 'client_disconnected')
                raise
            except Exception:
                finish('error', 'stream_error')
                raise
            finally:
                finish('cancelled', 'client_disconnected')

        return StreamingResponse(generate(), media_type='application/x-ndjson', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', 'X-Content-Type-Options': 'nosniff'})

    mount_static_app(application)
    return application


def mount_static_app(application):
    """Serve the built SPA from the same origin as the API when dist/ exists."""
    dist = Path(__file__).resolve().parents[1] / 'dist'
    index = (dist / 'index.html').resolve()
    if not index.is_file():
        return
    root = dist.resolve()

    @application.get('/{path:path}', include_in_schema=False)
    async def spa(path: str):
        if path.startswith('api/') or path == 'openapi.json' or path.startswith(('docs', 'redoc')):
            raise HTTPException(404, 'Not found.')
        target = (root / path).resolve() if path else index
        if path and target.is_file() and target.is_relative_to(root):
            cache = 'public, max-age=31536000, immutable' if path.startswith('assets/') else 'public, max-age=3600'
            return FileResponse(target, headers={'Cache-Control': cache})
        return FileResponse(index, headers={'Cache-Control': 'no-store'})


app = create_app()
