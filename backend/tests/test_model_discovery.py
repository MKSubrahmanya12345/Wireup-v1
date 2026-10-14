import asyncio
from backend import model_discovery


def test_lists_all_models_and_cache(monkeypatch):
    model_discovery._cache.clear()
    monkeypatch.setattr(model_discovery, 'model_options', lambda: [{'id': 'groq', 'label': 'Groq', 'model': 'default', 'configured': True}])
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'data': [{'id': 'one'}, {'id': 'two'}, {'id': 'one'}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs): calls.append(args); return Response()
    monkeypatch.setattr(model_discovery.httpx, 'AsyncClient', Client)
    result = asyncio.run(model_discovery.discover_models())
    assert [item['model'] for item in result['models']] == ['one', 'two', 'default']
    assert result['errors'] == {}
    assert asyncio.run(model_discovery.discover_models()) == result
    assert len(calls) == 1


def test_failure_preserves_default_without_exposing_credentials(monkeypatch):
    model_discovery._cache.clear()
    monkeypatch.setattr(model_discovery, 'model_options', lambda: [{'id': 'azure', 'label': 'Azure', 'model': 'deployment', 'configured': True}])
    class Client:
        def __init__(self, **kwargs): raise RuntimeError('secret-key')
    monkeypatch.setattr(model_discovery.httpx, 'AsyncClient', Client)
    result = asyncio.run(model_discovery.discover_models())
    assert result['models'][0]['model'] == 'deployment'
    assert 'azure' in result['errors']
    assert 'secret-key' not in str(result)
