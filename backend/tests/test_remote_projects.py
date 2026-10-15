import asyncio
import pytest
from fastapi import HTTPException
from backend.remote_projects import RemoteProjectRuntimes


class Client:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def capabilities(self): return {'boards': ['esp32-devkit-v1']}
    async def start(self, board, firmware): return {'session_id': 'a' * 32, 'status': 'running'}
    async def results(self, session): return {'session_id': session, 'status': 'running', 'serial': 'hello'}
    async def stop(self, session): return {'session_id': session, 'status': 'stopped'}


def test_remote_runtime_uses_actual_service_acknowledgements():
    async def run():
        runtime = RemoteProjectRuntimes(lambda: Client())
        project = {'id': 'project', 'board': 'esp32-devkit-v1'}
        assert (await runtime.command(project, 'run_simulation', {'program': 'base64'}))['status'] == 'running'
        assert (await runtime.command(project, 'read_simulation_results'))['serial'] == 'hello'
        assert (await runtime.command(project, 'stop_simulation'))['status'] == 'stopped'
        assert not runtime.sessions
    asyncio.run(run())


def test_remote_missing_configuration_is_truthful(monkeypatch):
    monkeypatch.delenv('REMOTE_SIMULATION_URL', raising=False)
    monkeypatch.delenv('REMOTE_SIMULATION_TOKEN', raising=False)
    async def run():
        with pytest.raises(HTTPException) as error:
            await RemoteProjectRuntimes().command({'id': 'p', 'board': 'esp32-devkit-v1'}, 'run_simulation', {'program': 'x'})
        assert error.value.status_code == 503
        assert 'not configured' in error.value.detail
    asyncio.run(run())
