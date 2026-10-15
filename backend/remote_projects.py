import asyncio

from fastapi import HTTPException
from backend.remote_simulation import RemoteSimulationClient, RemoteSimulationError, BOARD_IDS


class RemoteProjectRuntimes:
    def __init__(self, client_factory=RemoteSimulationClient.from_env):
        self.client_factory = client_factory
        self.sessions = {}
        self.lock = asyncio.Lock()

    async def shutdown(self):
        sessions = list(self.sessions.items())
        try:
            async with self.client_factory() as client:
                for project_id, session in sessions:
                    try:
                        await client.stop(session)
                        self.sessions.pop(project_id, None)
                    except RemoteSimulationError:
                        continue
        except RemoteSimulationError:
            pass

    async def command(self, project, name, artifact=None):
        if project['board'] not in BOARD_IDS:
            raise HTTPException(503, 'This board has no implemented remote emulator adapter.')
        try:
            async with self.lock:
                async with self.client_factory() as client:
                    if name == 'run_simulation':
                        if project['id'] in self.sessions:
                            raise HTTPException(409, 'Remote simulation is already running. Stop it first.')
                        capabilities = await client.capabilities()
                        supported = capabilities.get('boards', [])
                        if project['board'] not in supported and not any(isinstance(item, dict) and item.get('id') == project['board'] and item.get('available') for item in supported):
                            raise HTTPException(503, 'The remote emulator does not advertise this board as available.')
                        result = await client.start(project['board'], artifact.get('program') or artifact.get('bin', ''))
                        session = result.get('id') or result.get('session_id')
                        if not session:
                            raise RemoteSimulationError('Remote emulator did not return a session ID.')
                        if result.get('status') != 'running':
                            await client.stop(session)
                            raise RemoteSimulationError('Remote emulator did not acknowledge a running session.')
                        self.sessions[project['id']] = session
                        return result
                    session = self.sessions.get(project['id'])
                    if not session:
                        raise HTTPException(409, 'No remote simulation is running for this project.')
                    if name == 'stop_simulation':
                        result = await client.stop(session)
                        self.sessions.pop(project['id'], None)
                        return result
                    return await client.results(session)
        except RemoteSimulationError as error:
            raise HTTPException(503, str(error)) from None


remote_projects = RemoteProjectRuntimes()
