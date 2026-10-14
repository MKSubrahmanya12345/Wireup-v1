import asyncio
from contextlib import aclosing, asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
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
    project_answers: dict[str, str] | None = None
    approval: FeasibilityApproval | None = None
    provider: Literal['groq', 'nvidia', 'bedrock', 'azure'] = 'groq'
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
        key_name = {'azure': 'AZURE_API_KEY', 'nvidia': 'NVIDIA_API_KEY', 'bedrock': 'BEDROCK_API_KEY', 'groq': 'GROQ_API_KEY'}[provider]
        key = api_key if api_key is not None else os.getenv(key_name, '')
        if not key.strip() and not (provider == 'bedrock' and bedrock_configured()):
            raise HTTPException(503, f'Add {key_name} to .env, then retry to enable {provider.upper()} replies.')
        model_name = os.getenv('AZURE_MODEL_ID', 'gpt-6.1-sol') if provider == 'azure' else os.getenv('BEDROCK_MODEL_ID', 'moonshotai.kimi-k2.5') if provider == 'bedrock' else os.getenv('NVIDIA_MODEL', 'z-ai/glm-5.3') if provider == 'nvidia' else os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')
        model_name = payload.model or model_name
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
            yield event('heartbeat')
            try:
                async with asyncio.timeout(20):
                    model = await asyncio.to_thread(application.state.model_factory) if application.state.model_factory else await asyncio.to_thread(cached_model, provider, key, model_name, asyncio.get_running_loop())
            except Exception:
                yield event('text', channel='narration', text=f'{provider.upper()} model initialization failed or timed out. Check its API settings and retry.')
                yield event('done', status='error')
                return
            if len(selected) < len(payload.messages):
                yield event('text', channel='narration', text='Older messages were omitted from model context.\n')
            plain = not payload.project_id and not any(word in payload.messages[-1].content.lower() for word in ('calculate', 'calculator', 'compute')) and application.state.model_factory is None
            stream = stream_plain_chat(model, history, timeout_seconds=300 if provider == 'bedrock' else 30) if plain else stream_agent(model, history, payload.project_id, payload.runtime_token, requirements=payload.project_answers, approval=payload.approval.model_dump() if payload.approval else None)
            async with aclosing(stream) as agent:
                pending = asyncio.create_task(anext(agent, None))
                try:
                    while True:
                        finished, _ = await asyncio.wait({pending}, timeout=10)
                        if not finished:
                            if await request.is_disconnected():
                                return
                            yield event('heartbeat')
                            continue
                        item = pending.result()
                        if item is None:
                            return
                        if await request.is_disconnected():
                            return
                        if provider in ('nvidia', 'azure') and item.get('type') == 'text' and item.get('channel') == 'narration':
                            text = item.get('text', '').replace('Groq', 'NVIDIA' if provider == 'nvidia' else 'Azure')
                            if provider == 'nvidia' and 'model could not complete' in text.lower():
                                text = 'NVIDIA could not complete this request. Check your key and model access in the NVIDIA API catalog.'
                            item = {**item, 'text': text}
                        yield json.dumps(item, ensure_ascii=False) + '\n'
                        if item.get('type') == 'done':
                            return
                        pending = asyncio.create_task(anext(agent, None))
                finally:
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

        return StreamingResponse(generate(), media_type='application/x-ndjson', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', 'X-Content-Type-Options': 'nosniff'})

    return application


app = create_app()
