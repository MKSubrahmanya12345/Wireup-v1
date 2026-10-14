import asyncio
import base64
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import native_esp32
from backend.hardware import BOARD_CONFIG, HardwareService, RuntimeBridge, create_router
from backend.native_esp32 import NativeESP32, RunRequest, SerialRequest, StopRequest


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = SimpleNamespace(write=self.write, drain=AsyncMock())
        self.input = bytearray()
        self.returncode = None
        self.finished = asyncio.Event()
        self.terminated = False
        self.killed = False

    def write(self, data):
        self.input.extend(data)

    def finish(self, code=0):
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.finish(-15)

    def kill(self):
        self.killed = True
        self.finish(-9)


def compiled_project(service, board='esp32-devkit-v1'):
    project = service.create_project(board=board)
    cfg = BOARD_CONFIG[board]
    binary = bytearray(b'\xff' * cfg['flash_size_bytes'])
    segments = []
    for name, filename, offset, size in [
        ('bootloader', 'sketch.ino.bootloader.bin', 0x1000, 32),
        ('partitions', 'sketch.ino.partitions.bin', 0x8000, 32),
        ('boot_app0', 'boot_app0.bin', 0xe000, 32),
        ('application', 'sketch.ino.bin', 0x10000, 32),
    ]:
        binary[offset:offset + size] = bytes(size)
        if name in ('bootloader', 'application'):
            binary[offset] = 0xe9
        if name == 'partitions':
            binary[offset:offset + 2] = b'\xaa\x50'
        segments.append({'name': name, 'filename': filename, 'offset': offset, 'size_bytes': size})
    artifact = {'id': 'compiled', 'board': board, 'fqbn': cfg['fqbn'], 'format': 'bin',
                'source': project['firmware']['source'], 'source_revision': 1, 'project_revision': 1,
                'bin': base64.b64encode(binary).decode('ascii'), 'encoding': 'base64',
                'image_kind': 'merged-flash', 'chip': 'esp32', 'load_address': 0,
                'size_bytes': len(binary), 'flash_size_bytes': len(binary), 'flash_segments': segments}
    with service.lock:
        stored = service._load(project['id'])
        stored['compiler'] = {'status': 'compilation_complete', 'board': board, 'fqbn': cfg['fqbn'],
                              'source_revision': 1, 'project_revision': 1,
                              'source': project['firmware']['source'], 'errors': [], 'artifact': artifact}
        stored['_artifacts'] = {'compiled': copy.deepcopy(artifact)}
        service._save(stored)
    return project, bytes(binary)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    service = HardwareService(tmp_path / 'projects', native=NativeESP32(stop_timeout=0.05),
                              runtime=RuntimeBridge(timeout=0.1))
    project, binary = compiled_project(service)
    state = SimpleNamespace(service=service, project=project, binary=binary, processes=[], paths=[])
    monkeypatch.setattr(native_esp32, 'qemu_executable', lambda: str(tmp_path / 'qemu-system-xtensa.exe'))

    async def spawn(*args, **kwargs):
        assert args[1:] == ('-machine', 'esp32', '-display', 'none', '-serial', 'stdio',
                            '-monitor', 'none', '-nic', 'none', '-drive', 'file=flash.bin,if=mtd,format=raw')
        assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == asyncio.subprocess.PIPE
        path = Path(kwargs['cwd']) / 'flash.bin'
        assert path.read_bytes() == state.binary
        state.paths.append(path)
        process = Process()
        state.processes.append(process)
        return process

    state.spawn = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', state.spawn)
    state.payload = {'runtime_token': project['runtime_token'], 'expected_revision': 1, 'source_revision': 1}
    app = FastAPI()
    app.include_router(create_router(service))
    with TestClient(app) as client:
        state.client = client
        yield state


def post(state, action, **changes):
    return state.client.post(f'/api/hardware/projects/{state.project["id"]}/native/{action}',
                             json={**state.payload, **changes})


def get(state, action='results', **changes):
    return state.client.get(f'/api/hardware/projects/{state.project["id"]}/native/{action}',
                            params={'expected_revision': 1, 'source_revision': 1, **changes},
                            headers={'X-Runtime-Token': state.project['runtime_token']})


def test_actual_drive_uart_state_stop_and_restart(setup):
    response = post(setup, 'run', artifact_id='compiled')
    assert response.status_code == 200
    state = response.json()
    assert state['running'] is True
    assert state['gpio_available'] is False and state['gpio'] == {}
    assert state['runtime'] == 'qemu-native' and state['artifact_id'] == 'compiled'
    assert response.headers['cache-control'] == 'no-store'
    setup.client.portal.call(setup.processes[0].stdout.feed_data, b'Hello from firmware\r\n')
    setup.client.portal.call(asyncio.sleep, 0)
    result = get(setup).json()
    assert result['serial'] == 'Hello from firmware\r\n'
    assert result['serial_cursor'] == 21
    assert get(setup, 'serial', cursor=21).json()['serial'] == ''
    assert post(setup, 'serial', data='input\n', run_id=state['run_id']).json()['bytes_written'] == 6
    assert setup.processes[0].input == b'input\n'
    assert post(setup, 'run').status_code == 409
    assert post(setup, 'stop', run_id='old-run').status_code == 409
    assert setup.client.delete(f'/api/hardware/projects/{setup.project["id"]}').status_code == 409
    stopped = post(setup, 'stop', run_id=state['run_id']).json()
    assert stopped['running'] is False and stopped['status'] == 'stopped'
    assert setup.processes[0].terminated
    assert not setup.paths[0].exists()
    assert post(setup, 'serial', data='input').status_code == 409
    assert post(setup, 'stop').status_code == 200
    restarted = post(setup, 'run').json()
    assert restarted['run_id'] != state['run_id']
    assert restarted['serial'] == ''
    assert post(setup, 'stop').status_code == 200


@pytest.mark.parametrize('changes,status', [
    ({'runtime_token': 'wrong'}, 403), ({'expected_revision': 2}, 409),
    ({'source_revision': 2}, 409), ({'expected_revision': True}, 422),
    ({'source_revision': '1'}, 422), ({'artifact_id': 'old'}, 409),
    ({'executable': 'evil.exe'}, 422), ({'file': 'evil.bin'}, 422),
    ({'options': ['-net', 'user']}, 422),
])
def test_rejects_token_revisions_and_client_launch_options(setup, changes, status):
    assert post(setup, 'run', **changes).status_code == status
    setup.spawn.assert_not_awaited()


@pytest.mark.parametrize('record,field,value', [
    ('compiler', 'status', 'error'), ('compiler', 'stale', True), ('compiler', 'errors', ['failed']),
    ('compiler', 'source', 'different'), ('compiler', 'project_revision', 2),
    ('artifact', 'board', 'esp32-c3'), ('artifact', 'chip', 'esp32s3'),
    ('artifact', 'source_revision', 2), ('artifact', 'fqbn', 'wrong'),
    ('artifact', 'source', 'different'), ('artifact', 'image_kind', 'application'),
    ('artifact', 'bin', 'invalid'), ('artifact', 'load_address', 0x10000),
    ('artifact', 'size_bytes', 32), ('artifact', 'flash_segments', []),
])
def test_requires_successful_fresh_complete_classic_artifact(setup, record, field, value):
    stored = setup.service._load(setup.project['id'])
    target = stored['compiler'] if record == 'compiler' else stored['compiler']['artifact']
    target[field] = value
    setup.service._save(stored)
    assert post(setup, 'run').status_code == 409
    setup.spawn.assert_not_awaited()


def test_actual_exit_and_bounded_output(setup):
    setup.service.native.output_limit = 16
    assert post(setup, 'run').status_code == 200
    process = setup.processes[0]
    setup.client.portal.call(process.stdout.feed_data, b'a' * 100 + b'final UART')
    setup.client.portal.call(process.stderr.feed_data, b'x' * 100 + b'failure')
    setup.client.portal.call(process.finish, 3)
    setup.client.portal.call(asyncio.sleep, 0.01)
    result = get(setup, cursor=0).json()
    assert result['status'] == 'error' and result['returncode'] == 3
    assert not result['running']
    assert len(result['serial']) == len(result['stderr']) == 16
    assert result['serial'].endswith('final UART')
    assert result['serial_truncated'] and result['stderr_truncated']
    assert not setup.paths[0].exists()


def test_revision_mutation_marks_run_stale_but_allows_stop(setup):
    assert post(setup, 'run').status_code == 200
    setup.service._mutate(setup.project['id'], 'modify_component', {'id': 'board', 'x': 200})
    assert get(setup).status_code == 409
    assert get(setup, expected_revision=2).json()['stale']
    assert post(setup, 'serial', expected_revision=2, data='input').status_code == 409
    assert post(setup, 'stop', expected_revision=2).status_code == 200


def test_rechecks_revision_after_subprocess_launch(setup):
    original = setup.spawn.side_effect

    async def changed(*args, **kwargs):
        process = await original(*args, **kwargs)
        setup.service._mutate(setup.project['id'], 'modify_component', {'id': 'board', 'x': 200})
        return process

    setup.spawn.side_effect = changed
    assert post(setup, 'run').status_code == 409
    assert setup.processes[0].terminated
    assert not setup.paths[0].exists()


def test_local_only_token_on_reads_and_missing_session(setup):
    url = f'/api/hardware/projects/{setup.project["id"]}/native/results'
    assert setup.client.get(url, params={'expected_revision': 1, 'source_revision': 1}).status_code == 403
    assert get(setup).status_code == 404
    assert setup.client.post(url.replace('results', 'run'), json=setup.payload,
                             headers={'host': 'remote.example'}).status_code == 403
    assert setup.client.post(url.replace('results', 'run'), json=setup.payload,
                             headers={'origin': 'https://evil.example'}).status_code == 403
    assert get(setup, cursor=-1).status_code == 400
    other = setup.service.create_project(board='arduino-uno')
    setup.project = other
    setup.payload['runtime_token'] = other['runtime_token']
    assert post(setup, 'run').status_code == 400
    setup.spawn.assert_not_awaited()


def test_missing_executable_and_spawn_failure(setup, monkeypatch):
    def missing():
        raise HTTPException(503, 'QEMU unavailable')

    monkeypatch.setattr(native_esp32, 'qemu_executable', missing)
    assert post(setup, 'run').status_code == 503
    monkeypatch.setattr(native_esp32, 'qemu_executable', lambda: 'qemu-system-xtensa.exe')
    setup.spawn.side_effect = OSError('not installed')
    assert post(setup, 'run').status_code == 503
    assert not setup.service.native.active(setup.project['id'])


def test_timeout_kill_and_shutdown_cleanup(setup):
    setup.service.native.lifetime = 0.02
    original = setup.spawn.side_effect

    async def stubborn(*args, **kwargs):
        process = await original(*args, **kwargs)
        process.terminate = lambda: setattr(process, 'terminated', True)
        return process

    setup.spawn.side_effect = stubborn
    assert post(setup, 'run').status_code == 200
    process = setup.processes[0]
    async def finished():
        await asyncio.wait_for(setup.service.native.sessions[setup.project['id']].task, 2)

    setup.client.portal.call(finished)
    result = get(setup).json()
    assert result['status'] == 'timeout' and not result['running']
    assert process.terminated and process.killed
    assert not setup.paths[0].exists()
    setup.service.native.lifetime = 3600
    setup.spawn.side_effect = original
    assert post(setup, 'run').status_code == 200
    setup.client.portal.call(setup.service.native.close)
    assert setup.processes[1].terminated and not setup.paths[1].exists()


def test_launch_cancellation_reaps_late_subprocess(setup):
    async def exercise():
        release = asyncio.Event()
        original = setup.spawn.side_effect

        async def delayed(*args, **kwargs):
            await release.wait()
            return await original(*args, **kwargs)

        setup.spawn.side_effect = delayed
        task = asyncio.create_task(setup.service.native.run(setup.service, setup.project['id'], RunRequest(**setup.payload)))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert setup.service.native.active(setup.project['id'])
        release.set()
        await setup.service.native.close()
        assert setup.processes[0].terminated
        assert not setup.paths[0].exists()

    setup.client.portal.call(exercise)


def test_concurrent_run_and_capacity(setup):
    async def exercise():
        results = await asyncio.gather(*[
            setup.service.native.run(setup.service, setup.project['id'], RunRequest(**setup.payload))
            for _ in range(2)
        ], return_exceptions=True)
        assert sum(isinstance(item, dict) for item in results) == 1
        assert [item.status_code for item in results if isinstance(item, HTTPException)] == [409]
        setup.service.native.max_running = 1
        other, _ = compiled_project(setup.service)
        payload = RunRequest(runtime_token=other['runtime_token'], expected_revision=1, source_revision=1)
        with pytest.raises(HTTPException) as error:
            await setup.service.native.run(setup.service, other['id'], payload)
        assert error.value.status_code == 429
        await setup.service.native.close()

    setup.client.portal.call(exercise)


def test_explicit_native_hardware_command_routing(setup):
    async def exercise():
        args = {'runtime': 'qemu-native', 'expected_revision': 1, 'source_revision': 1}
        for name in ('run_simulation', 'read_simulation_results', 'stop_simulation'):
            result = await setup.service.command(setup.project['id'], name, args, setup.project['runtime_token'])
            assert result['status'] == 'acknowledged' and result['result']['runtime'] == 'qemu-native'
        assert BOARD_CONFIG[setup.project['board']]['simulation'] == 'native'

    setup.client.portal.call(exercise)


def test_classic_esp32_never_dispatches_the_browser_bridge(setup):
    setup.service.runtime.sessions[setup.project['id']] = object()
    setup.service.runtime.command = AsyncMock(return_value={'status': 'acknowledged', 'result': {'running': True}})
    result = setup.client.portal.call(setup.service.command, setup.project['id'], 'run_simulation', {}, setup.project['runtime_token'])
    assert result['result']['runtime'] == 'qemu-native'
    setup.service.runtime.command.assert_not_awaited()
    assert setup.spawn.await_count == 1


def test_qemu_discovery_is_vendor_only_and_env_is_operator_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(native_esp32, 'ROOT', tmp_path)
    monkeypatch.delenv('WIREUP_QEMU_XTENSA', raising=False)
    with pytest.raises(HTTPException) as error:
        native_esp32.qemu_executable()
    assert error.value.status_code == 503
    path = tmp_path / 'vendor/qemu/esp-qemu-v9.2.2/bin/qemu-system-xtensa.exe'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'operator-installed-binary')
    assert native_esp32.qemu_executable() == str(path.resolve())
    monkeypatch.setenv('WIREUP_QEMU_XTENSA', str(tmp_path / 'missing.exe'))
    with pytest.raises(HTTPException):
        native_esp32.qemu_executable()
    monkeypatch.setenv('WIREUP_QEMU_XTENSA', str(path))
    assert native_esp32.qemu_executable() == str(path.resolve())


def test_startup_timeout_reaps_process_and_uart_bytes_are_bounded(setup):
    async def exercise():
        release = asyncio.Event()
        original = setup.spawn.side_effect
        setup.service.native.startup_timeout = 0.02

        async def delayed(*args, **kwargs):
            await release.wait()
            return await original(*args, **kwargs)

        setup.spawn.side_effect = delayed
        with pytest.raises(HTTPException) as error:
            await setup.service.native.run(setup.service, setup.project['id'], RunRequest(**setup.payload))
        assert error.value.status_code == 504
        release.set()
        await setup.service.native.sessions[setup.project['id']].task
        assert setup.processes[0].terminated and not setup.paths[0].exists()
        setup.spawn.side_effect = original
        setup.service.native.startup_timeout = 10
        await setup.service.native.run(setup.service, setup.project['id'], RunRequest(**setup.payload))
        with pytest.raises(HTTPException) as error:
            await setup.service.native.serial(setup.service, setup.project['id'], SerialRequest(**setup.payload, data='é' * 4096))
        assert error.value.status_code == 400
        assert not setup.processes[1].input
        await setup.service.native.close()

    setup.client.portal.call(exercise)
