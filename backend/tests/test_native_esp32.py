import asyncio
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import native_esp32
from backend.hardware import BOARD_CONFIG, HardwareService, RuntimeBridge, create_router
from backend.native_esp32 import GPIORequest, NativeESP32, RunRequest, SerialRequest, StopRequest


class Input:
    def __init__(self, process):
        self.process = process
        self.buffer = bytearray()
        self.commands = []

    def write(self, data):
        self.buffer.extend(data)
        while b'\n' in self.buffer:
            line, _, remainder = self.buffer.partition(b'\n')
            self.buffer = bytearray(remainder)
            command = json.loads(line)
            self.commands.append(command)
            if command['type'] == 'stop':
                self.process.finish(0)

    async def drain(self):
        return None


class Process:
    def __init__(self, ready=True):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = Input(self)
        self.returncode = None
        self.finished = asyncio.Event()
        self.terminated = False
        self.killed = False
        if ready:
            self.event({'type': 'ready', 'gpio_available': True})

    def event(self, event):
        self.stdout.feed_data((json.dumps(event) + '\n').encode())

    def finish(self, code=0):
        if self.returncode is not None:
            return
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
                'bin': base64.b64encode(binary).decode(), 'encoding': 'base64',
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
    dll = tmp_path / 'libqemu-xtensa.dll'
    rom = tmp_path / 'share/qemu'
    state = SimpleNamespace(service=service, project=project, binary=binary, processes=[], paths=[])
    monkeypatch.setattr(native_esp32, 'qemu_runtime', lambda: (str(dll), str(rom)))

    async def spawn(*args, **kwargs):
        assert args[:3] == (native_esp32.sys.executable, '-m', 'backend.native_esp32_worker')
        assert args[3:5] == ('--dll', str(dll))
        assert args[5:7] == ('--rom', str(rom))
        assert args[7] == '--flash'
        path = Path(args[8])
        assert path.read_bytes() == state.binary
        assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == asyncio.subprocess.PIPE
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


def test_worker_uart_gpio_input_stop_and_restart(setup):
    started = post(setup, 'run', artifact_id='compiled').json()
    assert started['running'] and started['gpio_available'] and started['runtime'] == 'qemu-native'
    process = setup.processes[0]
    process.event({'type': 'uart', 'port': 0, 'data': base64.b64encode(b'firmware\r\n').decode()})
    process.event({'type': 'gpio_direction', 'pin': 32, 'output': True})
    process.event({'type': 'gpio', 'pin': 32, 'level': True})
    setup.client.portal.call(asyncio.sleep, 0)
    result = get(setup).json()
    assert result['serial'] == 'firmware\r\n'
    assert result['gpio']['GPIO32'] == {'level': True, 'drive': 'high'}
    assert post(setup, 'serial', data='input\n', run_id=started['run_id']).json()['bytes_written'] == 6
    assert process.stdin.commands[-1] == {'type': 'uart_input', 'port': 0,
                                          'data': base64.b64encode(b'input\n').decode()}
    gpio = post(setup, 'gpio', pin=4, level=False, run_id=started['run_id'])
    assert gpio.status_code == 200
    assert process.stdin.commands[-1] == {'type': 'gpio_input', 'pin': 4, 'level': False}
    assert post(setup, 'gpio', pin=40, level=True).status_code == 422
    assert setup.client.delete(f'/api/hardware/projects/{setup.project["id"]}').status_code == 409
    stopped = post(setup, 'stop', run_id=started['run_id']).json()
    assert not stopped['running'] and stopped['status'] == 'stopped'
    assert not setup.paths[0].exists()
    restarted = post(setup, 'run').json()
    assert restarted['run_id'] != started['run_id']
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
    ('compiler', 'status', 'error'), ('compiler', 'stale', True),
    ('artifact', 'board', 'esp32-c3'), ('artifact', 'chip', 'esp32s3'),
    ('artifact', 'source_revision', 2), ('artifact', 'image_kind', 'application'),
    ('artifact', 'bin', 'invalid'), ('artifact', 'flash_segments', []),
])
def test_requires_fresh_complete_classic_artifact(setup, record, field, value):
    stored = setup.service._load(setup.project['id'])
    target = stored['compiler'] if record == 'compiler' else stored['compiler']['artifact']
    target[field] = value
    setup.service._save(stored)
    assert post(setup, 'run').status_code == 409
    setup.spawn.assert_not_awaited()


def test_worker_exit_and_bounded_output(setup):
    setup.service.native.output_limit = 16
    assert post(setup, 'run').status_code == 200
    process = setup.processes[0]
    process.event({'type': 'uart', 'port': 0, 'data': base64.b64encode(b'a' * 100 + b'final UART').decode()})
    process.stderr.feed_data(b'x' * 100 + b'failure')
    process.finish(3)
    setup.client.portal.call(asyncio.sleep, 0.01)
    result = get(setup, cursor=0).json()
    assert result['status'] == 'error' and result['returncode'] == 3
    assert len(result['serial']) == len(result['stderr']) == 16
    assert result['serial_truncated'] and result['stderr_truncated']


def test_revision_mutation_marks_run_stale_but_allows_stop(setup):
    assert post(setup, 'run').status_code == 200
    setup.service._mutate(setup.project['id'], 'modify_component', {'id': 'board', 'x': 200})
    assert get(setup).status_code == 409
    assert get(setup, expected_revision=2).json()['stale']
    assert post(setup, 'serial', expected_revision=2, data='input').status_code == 409
    assert post(setup, 'gpio', expected_revision=2, pin=2, level=True).status_code == 409
    assert post(setup, 'stop', expected_revision=2).status_code == 200


def test_local_only_token_missing_session_and_classic_routing(setup):
    assert get(setup).status_code == 404
    url = f'/api/hardware/projects/{setup.project["id"]}/native/results'
    assert setup.client.get(url, params={'expected_revision': 1, 'source_revision': 1}).status_code == 403
    assert get(setup, cursor=-1).status_code == 400
    result = setup.client.portal.call(setup.service.command, setup.project['id'], 'run_simulation', {}, setup.project['runtime_token'])
    assert result['result']['runtime'] == 'qemu-native'
    assert setup.spawn.await_count == 1
    setup.client.portal.call(setup.service.native.close)


def test_missing_runtime_and_spawn_failure(setup, monkeypatch):
    def missing():
        raise HTTPException(503, 'QEMU unavailable')
    monkeypatch.setattr(native_esp32, 'qemu_runtime', missing)
    assert post(setup, 'run').status_code == 503
    monkeypatch.setattr(native_esp32, 'qemu_runtime', lambda: ('qemu.dll', 'rom'))
    setup.spawn.side_effect = OSError('not installed')
    assert post(setup, 'run').status_code == 503
    assert not setup.service.native.active(setup.project['id'])


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


def test_runtime_discovery_is_vendor_only_and_env_is_operator_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(native_esp32, 'ROOT', tmp_path)
    monkeypatch.delenv('WIREUP_QEMU_XTENSA_DLL', raising=False)
    with pytest.raises(HTTPException) as error:
        native_esp32.qemu_runtime()
    assert error.value.status_code == 503
    dll = tmp_path / 'vendor/qemu-picsimlab/bin/libqemu-xtensa.dll'
    rom = tmp_path / 'vendor/qemu-picsimlab/share/qemu'
    dll.parent.mkdir(parents=True)
    rom.mkdir(parents=True)
    dll.write_bytes(b'dll')
    for name in ('esp32-v3-rom.bin', 'esp32-v3-rom-app.bin'):
        (rom / name).write_bytes(b'rom')
    assert native_esp32.qemu_runtime() == (str(dll.resolve()), str(rom.resolve()))
    monkeypatch.setenv('WIREUP_QEMU_XTENSA_DLL', str(tmp_path / 'missing.dll'))
    with pytest.raises(HTTPException):
        native_esp32.qemu_runtime()
