import json

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

import backend.app as module


class Model:
    async def astream(self, history):
        yield AIMessageChunk(content='Ready')


def test_provider_client_reused_and_settings_changes_invalidate(monkeypatch):
    calls = []
    monkeypatch.setattr(module, 'reload_settings', lambda: None)
    monkeypatch.setattr(module, 'build_model', lambda *args: calls.append(args) or Model())
    monkeypatch.setenv('GROQ_MODEL', 'test-model')
    app = module.create_app(api_key='test-key')
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]})
            assert response.status_code == 200
            assert json.loads(response.text.splitlines()[0]) == {'type': 'heartbeat'}
        assert len(calls) == 1
        monkeypatch.setenv('GROQ_MODEL', 'changed-model')
        client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]})
        assert len(calls) == 2
        monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid')
        client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]})
        assert len(calls) == 3


def test_custom_factory_is_not_cached():
    calls = []
    with TestClient(module.create_app(lambda: calls.append(True) or Model(), api_key='test-key')) as client:
        for _ in range(2):
            client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]})
    assert len(calls) == 2
