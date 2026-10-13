import asyncio
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.hardware import (
    ArduinoCompiler, BOARD_CONFIG, ComponentCatalog, DEFAULT_BOARD_SOURCES, DEFAULT_SOURCE, HardwareService, PIN_LAYOUTS,
    RuntimeBridge, TOOLS, calculator, create_router,
)


class FakeCompiler:
    async def compile(self, project):
        result = {'status': 'simulation_ready', 'stdout': 'compiled', 'stderr': '', 'errors': [],
                'source_revision': project['firmware']['revision'], 'project_revision': project['revision'],
                'source': project['firmware']['source'],
                'artifact': {'id': 'test-artifact', 'format': 'hex', 'board': project['board'],
                             'source_revision': project['firmware']['revision'],
                             'project_revision': project['revision'], 'hex': ':00000001FF\n'}}
        if project['board'] == 'pi-pico':
            result['artifact'].pop('hex')
            result['artifact'].update({'format': 'bin', 'bin': base64.b64encode(bytes(512)).decode('ascii'),
                                       'encoding': 'base64', 'load_address': 0x10000000, 'size_bytes': 512})
        return result


@pytest.fixture
def service(tmp_path):
    return HardwareService(tmp_path / 'projects', compiler=FakeCompiler(), runtime=RuntimeBridge(timeout=0.2))


@pytest.fixture
def client(service):
    app = FastAPI()
    app.include_router(create_router(service))
    with TestClient(app) as test_client:
        yield test_client


def command(client, project, name, args=None, token=None):
    return client.post(f'/api/hardware/project/{project["id"]}/command',
                       json={'name': name, 'args': args or {}, 'runtime_token': token})


@pytest.mark.parametrize('payload', [{}, {'board': 'unselected'}, {'board': 'arduino-uno'}])
def test_project_creation_and_persistence(client, service, payload):
    response = client.post('/api/hardware/projects', json=payload)
    assert response.status_code == 200
    project = response.json()
    assert project['board'] == payload.get('board', 'unselected')
    assert project['wires'] == []
    assert project['history'] == []
    assert project['compiler'] is None
    if project['board'] == 'unselected':
        assert project['components'] == []
        assert project['firmware']['source'] == ''
    else:
        assert project['components'][0]['id'] == 'board'
        assert project['firmware']['source'] == DEFAULT_SOURCE
    assert project['schema_version'] == project['revision'] == 1
    assert len(project['runtime_token']) > 32
    assert '_undo' not in project
    reloaded = HardwareService(service.data_dir).get_project(project['id'])
    assert reloaded == project
    assert client.get('/api/hardware/projects').json()['projects'][0]['id'] == project['id']
    assert client.get(f'/api/hardware/projects/{project["id"]}').json() == project
    assert command(client, project, 'read_project').json() == project
    assert command(client, project, 'read_firmware').json() == project['firmware']
    assert command(client, project, 'read_compiler_errors').json()['status'] == 'not_compiled'


def test_catalog_vendor_and_fallback(client, tmp_path):
    catalog = client.get('/api/hardware/catalog?q=led').json()
    assert any(item['id'] == 'led' and item['pins'] == ['A', 'C'] for item in catalog['components'])
    assert catalog['boards'][0]['id'] == 'arduino-uno'
    assert len(ComponentCatalog().components) >= 10
    assert ComponentCatalog(tmp_path / 'missing.json').origin == 'builtin-fallback'
    assert client.get('/api/hardware/catalog?limit=1000').status_code == 400


def test_component_wire_mutations_revisions_and_undo(client, service):
    project = service.create_project(board='arduino-uno')
    result = command(client, project, 'add_component', {'id': 'led1', 'type': 'led', 'expected_revision': 1})
    assert result.status_code == 200
    assert result.json()['revision'] == 2
    assert command(client, project, 'modify_component', {'id': 'led1', 'x': 42, 'expected_revision': 1}).status_code == 409
    modified = command(client, project, 'modify_component', {'id': 'led1', 'x': 42, 'y': 24, 'rotation': 90}).json()
    assert modified['components'][1]['x'] == 42
    wire_args = {'id': 'wire1', 'from': {'component': 'board', 'pin': '13'},
                 'to': {'component': 'led1', 'pin': 'A'}}
    assert command(client, project, 'connect_wire', wire_args).status_code == 200
    assert command(client, project, 'connect_wire', wire_args).status_code == 409
    bad = copy.deepcopy(wire_args)
    bad['to']['pin'] = 'made-up'
    assert command(client, project, 'connect_wire', bad).status_code == 400
    assert command(client, project, 'remove_component', {'id': 'board'}).status_code == 400
    removed = command(client, project, 'remove_component', {'id': 'led1'}).json()
    assert removed['wires'] == []
    assert len(removed['components']) == 1
    restored = command(client, project, 'undo').json()
    assert restored['revision'] == removed['revision'] + 1
    assert restored['wires'][0]['id'] == 'wire1'
    assert restored['history'][-1]['operation'] == 'undo'
    assert command(client, project, 'remove_wire', {'id': 'wire1'}).json()['wires'] == []
    assert HardwareService(service.data_dir).get_project(project['id'])['wires'] == []


def test_firmware_replacement_and_undo(client, service):
    project = service.create_project(board='arduino-uno')
    changed = command(client, project, 'generate_firmware', {'source': 'void setup() {}\nvoid loop() {}'}).json()
    assert changed['firmware']['revision'] == 2
    edited = command(client, project, 'edit_firmware', {'old': 'void loop() {}', 'new': 'void loop() { delay(1); }'}).json()
    assert edited['firmware']['revision'] == 3
    assert 'delay(1)' in edited['firmware']['source']
    restored = command(client, project, 'undo').json()
    assert restored['firmware']['revision'] == 4
    assert restored['firmware']['source'] == changed['firmware']['source']
    assert command(client, project, 'edit_firmware', {'old': 'missing', 'new': 'anything'}).status_code == 400
    for source in ['', '\x00', '#include "../../secret.h"', '#include <C:/secret.h>', 'x' * 100001]:
        assert command(client, project, 'generate_firmware', {'source': source}).status_code == 400


def test_validation_and_paths(client, service):
    project = service.create_project(board='arduino-uno')
    assert command(client, project, 'add_component', {'type': 'arduino-uno'}).status_code == 409
    for args in [{'type': 'does-not-exist'}, {'type': 'led', 'id': '../outside'},
                 {'type': 'led', 'x': 'not-a-number'}, {'type': 'led', 'properties': {'nonexistent': True}}]:
        assert command(client, project, 'add_component', args).status_code == 400
    assert client.post('/api/hardware/projects', json={'board': 'unsupported-board'}).status_code == 400
    assert command(client, project, 'not-a-tool').status_code == 400
    assert command(client, project, 'remove_wire', {'id': 'missing'}).status_code == 404
    assert command(client, project, 'undo').status_code == 409
    with pytest.raises(HTTPException) as error:
        service.get_project('../outside')
    assert error.value.status_code == 400
    assert len(TOOLS) == 17
    assert 'check_connections' in TOOLS


@pytest.mark.parametrize('expression,result', [('2 + 3 * 4', 14), ('(5 - 1) ** 2', 16), ('-10 / 4', -2.5), ('7 % 4', 3)])
def test_calculator(expression, result):
    assert calculator(expression)['result'] == result


@pytest.mark.parametrize('expression', ['__import__("os")', 'open("file")', '2 ** 1000000', '1/0', 'True + 1',
                                        '[1,2]', '1e999', '(-1) ** 0.5', '9' * 257, '1+' * 100 + '1'])
def test_calculator_rejects_unsafe_expressions(expression):
    with pytest.raises(HTTPException) as error:
        calculator(expression)
    assert error.value.status_code == 400


def test_compile_artifact_revision_diagnostics_and_staleness(client, service):
    project = service.create_project(board='arduino-uno')
    compiled = command(client, project, 'compile_firmware').json()
    assert compiled['status'] == 'simulation_ready'
    assert compiled['source_revision'] == compiled['project_revision'] == 1
    assert compiled['source'] == DEFAULT_SOURCE
    assert not compiled['stale']
    artifact = client.get(compiled['artifact']['url'])
    assert artifact.text == ':00000001FF\n'
    assert artifact.headers['x-source-revision'] == '1'
    assert command(client, project, 'read_compiler_errors').json()['status'] == 'simulation_ready'
    command(client, project, 'modify_component', {'id': 'board', 'x': 20})
    response = command(client, project, 'run_simulation', token=project['runtime_token'])
    assert response.status_code == 503
    assert 'No browser runtime' in response.json()['detail']
    command(client, project, 'edit_firmware', {'source': 'void setup(){} void loop(){}'})
    assert command(client, project, 'run_simulation', token=project['runtime_token']).status_code == 409
    assert service.get_project(project['id'])['compiler']['artifact']['project_revision'] == 1
    assert command(client, project, 'compile_firmware', {'expected_revision': 1}).status_code == 409


def test_compile_preserves_snapshot_when_mutated(service):
    project = service.create_project(board='arduino-uno')
    class EditingCompiler(FakeCompiler):
        async def compile(self, snapshot):
            await service.command(project['id'], 'edit_firmware', {'source': 'void setup(){} void loop(){}'})
            return await super().compile(snapshot)
    service.compiler = EditingCompiler()
    result = asyncio.run(service.command(project['id'], 'compile_firmware'))
    assert result['stale']
    assert result['source'] == DEFAULT_SOURCE
    assert result['source_revision'] == 1
    assert service.get_project(project['id'])['firmware']['revision'] == 2


def test_no_runtime_and_authorization(client, service):
    project = service.create_project(board='arduino-uno')
    command(client, project, 'compile_firmware')
    for name in ('run_simulation', 'stop_simulation', 'read_simulation_results'):
        assert command(client, project, name).status_code == 403
        assert command(client, project, name, token='wrong').status_code == 403
        assert command(client, project, name, token=project['runtime_token']).status_code == 503
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f'/api/hardware/project/{project["id"]}/runtime?token=wrong'):
            pass
    assert error.value.code == 4403


def test_websocket_command_acknowledgement_ownership_and_rejection(client, service):
    project = service.create_project(board='arduino-uno')
    command(client, project, 'compile_firmware')
    url = f'/api/hardware/project/{project["id"]}/runtime?token={project["runtime_token"]}'
    with client.websocket_connect(url) as websocket:
        assert websocket.receive_json()['type'] == 'runtime_connected'
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(url) as other:
                other.receive_json()
        assert error.value.code == 4409
        with ThreadPoolExecutor() as pool:
            for name in ('run_simulation', 'read_simulation_results', 'stop_simulation'):
                response = pool.submit(command, client, project, name, None, project['runtime_token'])
                message = websocket.receive_json()
                assert message['name'] == name
                if name == 'run_simulation':
                    assert message['args']['artifact']['hex'] == ':00000001FF\n'
                websocket.send_json({'type': 'ack', 'id': 'wrong-id', 'ok': True, 'result': {'fake': True}})
                websocket.send_json({'type': 'ack', 'id': message['id'], 'ok': True, 'result': {'serial': 'real browser result'}})
                result = response.result().json()
                assert result['status'] == 'acknowledged'
                assert result['result']['serial'] == 'real browser result'
            response = pool.submit(command, client, project, 'run_simulation', None, project['runtime_token'])
            message = websocket.receive_json()
            websocket.send_json({'type': 'ack', 'id': message['id'], 'ok': False, 'error': 'Emulator failed'})
            assert response.result().status_code == 502


def test_runtime_timeout_and_disconnect(client, service):
    project = service.create_project(board='arduino-uno')
    url = f'/api/hardware/project/{project["id"]}/runtime?token={project["runtime_token"]}'
    with client.websocket_connect(url) as websocket:
        websocket.receive_json()
        assert command(client, project, 'read_simulation_results', token=project['runtime_token']).status_code == 504
        assert service.runtime.pending[project['id']] == {}
        with ThreadPoolExecutor() as pool:
            response = pool.submit(command, client, project, 'stop_simulation', None, project['runtime_token'])
            websocket.receive_json()
            websocket.close()
            assert response.result().status_code == 503


def test_missing_compiler(service):
    service.compiler = ArduinoCompiler(executable='Z:/missing/arduino-cli.exe')
    project = service.create_project(board='arduino-uno')
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'], 'compile_firmware'))
    assert error.value.status_code == 503
    assert service.get_project(project['id'])['compiler']['errors']


def test_catalog_properties_are_normalized_and_validated(client, service):
    project = service.create_project(board='arduino-uno')
    added = command(client, project, 'add_component', {'id': 'led1', 'type': 'led',
                                                     'properties': {'brightness': 0.5, 'value': True, 'color': 'green'}})
    assert added.status_code == 200
    assert added.json()['components'][1]['properties']['brightness'] == 0.5
    assert command(client, project, 'modify_component', {'id': 'led1', 'properties': {'color': 'invalid'}}).status_code == 400
    assert command(client, project, 'modify_component', {'id': 'led1', 'properties': {'value': 'true'}}).status_code == 400
    assert command(client, project, 'calculator', {'expression': '1000 / 220'}).json()['result'] == 1000 / 220
    assert command(client, project, 'search_components', {'query': 'led', 'limit': 2}).status_code == 200


def test_runtime_tokens_cannot_cross_projects(client, service):
    first, second = service.create_project(board='arduino-uno'), service.create_project(board='arduino-uno')
    response = command(client, second, 'stop_simulation', token=first['runtime_token'])
    assert response.status_code == 403
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f'/api/hardware/project/{second["id"]}/runtime?token={first["runtime_token"]}'):
            pass
    assert error.value.code == 4403


def test_revision_compare_and_swap_is_atomic(service):
    project = service.create_project(board='arduino-uno')
    def mutate(component_id):
        try:
            return asyncio.run(service.command(project['id'], 'add_component',
                                                {'id': component_id, 'type': 'led', 'expected_revision': 1}))
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(mutate, ['led1', 'led2']))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(409) == 1
    assert service.get_project(project['id'])['revision'] == 2


@pytest.mark.parametrize('board,valid,invalid', [('arduino-nano', 'A7', 'A8'),
                                                ('arduino-mega', '53', '54'),
                                                ('pi-pico', 'GP28', 'GP25')])
def test_supported_board_projects_and_pins(client, service, board, valid, invalid):
    response = client.post('/api/hardware/projects', json={'board': board})
    assert response.status_code == 200
    project = response.json()
    assert project['components'][0]['type'] == board
    assert HardwareService(service.data_dir).get_project(project['id'])['board'] == board
    command(client, project, 'add_component', {'id': 'led1', 'type': 'led'})
    wire = {'from': {'component': 'board', 'pin': valid}, 'to': {'component': 'led1', 'pin': 'A'}}
    assert command(client, project, 'connect_wire', wire).status_code == 200
    wire['from']['pin'] = invalid
    assert command(client, project, 'connect_wire', wire).status_code == 400
    assert command(client, project, 'add_component', {'type': 'arduino-nano'}).status_code == 409


def test_exact_board_catalog_and_legacy_uno_alias(client, service):
    catalog = service.catalog
    assert set(BOARD_CONFIG) == {'arduino-uno', 'arduino-nano', 'arduino-mega', 'pi-pico', 'pi-pico-w',
                                 'esp32-devkit-v1', 'esp32-devkit-c-v4', 'esp32-s3', 'esp32-c3',
                                 'raspberry-pi-3', 'raspberry-pi-4', 'raspberry-pi-5'}
    assert 'GND.5' in catalog.components['arduino-mega']['pins']
    assert 'RESET.3' in catalog.components['arduino-nano']['pins']
    assert len(catalog.components['pi-pico']['pins']) == 40
    assert 'GP23' not in catalog.components['pi-pico']['pins']
    assert 'AGND' not in catalog.components['pi-pico']['pins']
    assert catalog.components['esp32-devkit-v1']['simulation'] == 'unavailable'
    for board in BOARD_CONFIG:
        assert catalog.components[board]['supported_board']
        assert catalog.components[board]['tagName'] == BOARD_CONFIG[board]['tagName']
    project = service.create_project(board='arduino-uno')
    command(client, project, 'add_component', {'id': 'led1', 'type': 'led'})
    response = command(client, project, 'connect_wire', {'from': {'component': 'board', 'pin': 'D13'},
                                                       'to': {'component': 'led1', 'pin': 'A'}})
    assert response.json()['wires'][0]['from']['pin'] == '13'


@pytest.mark.parametrize('board', ['arduino-nano', 'arduino-mega', 'pi-pico'])
def test_multiboard_runtime_artifact_bridge(client, service, board):
    project = service.create_project(board=board)
    compiled = command(client, project, 'compile_firmware').json()
    artifact = compiled['artifact']
    download = client.get(artifact['url'])
    if board == 'pi-pico':
        assert artifact['format'] == 'bin'
        assert download.content == base64.b64decode(artifact['bin'])
        assert download.headers['content-type'] == 'application/octet-stream'
        assert 'sketch.ino.bin' in download.headers['content-disposition']
    else:
        assert artifact['format'] == 'hex'
    url = f'/api/hardware/project/{project["id"]}/runtime?token={project["runtime_token"]}'
    with client.websocket_connect(url) as websocket:
        websocket.receive_json()
        with ThreadPoolExecutor() as pool:
            response = pool.submit(command, client, project, 'run_simulation', None, project['runtime_token'])
            message = websocket.receive_json()
            assert message['args']['project']['board'] == board
            assert 'runtime_token' not in message['args']['project']
            assert message['args']['artifact']['format'] == artifact['format']
            websocket.send_json({'type': 'ack', 'id': message['id'], 'ok': True, 'result': {'board': board}})
            assert response.result().json()['result']['board'] == board


def test_local_only_token_exposure(client, service):
    project = service.create_project(board='arduino-uno')
    path = f'/api/hardware/projects/{project["id"]}'
    response = client.get(path)
    assert response.json()['runtime_token'] == project['runtime_token']
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'runtime_token' not in client.get('/api/hardware/projects').text
    assert client.get(path, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get(path, headers={'Host': 'evil.example'}).status_code == 403
    assert client.get(path, headers={'Origin': 'null'}).status_code == 403
    assert client.get(path, headers={'Origin': 'http://localhost:5173'}).status_code == 200
    app = FastAPI()
    app.include_router(create_router(service))
    with TestClient(app, client=('203.0.113.5', 50000)) as remote:
        assert remote.get(path).status_code == 403
        assert remote.post('/api/hardware/projects', json={}).status_code == 403
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f'/api/hardware/project/{project["id"]}/runtime?token={project["runtime_token"]}',
                                      headers={'Origin': 'https://evil.example'}):
            pass
    assert error.value.code == 4403


class FakeStream:
    def __init__(self, data):
        self.data = data

    async def read(self, size):
        data, self.data = self.data[:size], self.data[size:]
        return data


class FakeProcess:
    pid = 99999999
    def __init__(self, code=1, output=b'', hang=False):
        self.stdout = FakeStream(output)
        self.stderr = FakeStream(b'sketch.ino:2: error: bad syntax\n')
        self.returncode = None
        self.code = code
        self.hang = hang
        self.killed = False
        self.done = asyncio.Event()

    async def wait(self):
        if self.hang:
            await self.done.wait()
        self.returncode = self.code
        return self.code

    def kill(self):
        self.killed = True
        self.done.set()


def test_compiler_failure_diagnostics_no_shell(tmp_path, monkeypatch):
    executable = tmp_path / 'arduino-cli.exe'
    executable.touch()
    process = FakeProcess()
    captured = {}
    async def launch(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    project = HardwareService(tmp_path / 'projects').create_project(board='arduino-uno')
    result = asyncio.run(ArduinoCompiler(executable=str(executable)).compile(project))
    assert result['status'] == 'error'
    assert 'bad syntax' in result['errors'][0]
    assert result['artifact'] is None
    assert captured['args'][1:4] == ('compile', '--fqbn', 'arduino:avr:uno')
    assert 'shell' not in captured['kwargs']
    assert not Path(captured['args'][-1]).exists()


@pytest.mark.parametrize('mode,status', [('timeout', 504), ('output', 502), ('cancel', None)])
def test_compiler_timeout_output_limit_and_cancellation(tmp_path, monkeypatch, mode, status):
    executable = tmp_path / 'arduino-cli.exe'
    executable.touch()
    process = FakeProcess(output=b'x' * 100 if mode == 'output' else b'', hang=True)
    async def launch(*args, **kwargs):
        return process
    async def terminate(self, proc):
        proc.kill()
        await proc.wait()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(ArduinoCompiler, '_terminate', terminate)
    project = HardwareService(tmp_path / 'projects').create_project(board='arduino-uno')
    compiler = ArduinoCompiler(executable=str(executable), timeout=0.03, output_limit=20 if mode == 'output' else 1000)
    async def run():
        task = asyncio.create_task(compiler.compile(project))
        if mode == 'cancel':
            await asyncio.sleep(0.005)
            task.cancel()
        await task
    with pytest.raises(asyncio.CancelledError if mode == 'cancel' else HTTPException) as error:
        asyncio.run(run())
    if status:
        assert error.value.status_code == status
    assert process.killed


@pytest.mark.parametrize('valid,status', [(True, None), (False, 502)])
def test_pico_compiler_artifact_loader_contract(tmp_path, monkeypatch, valid, status):
    executable = tmp_path / 'arduino-cli.exe'
    executable.touch()
    binary = bytearray(512)
    if valid:
        binary[256:260] = (0x20041000).to_bytes(4, 'little')
        binary[260:264] = (0x10000101).to_bytes(4, 'little')
    captured = {}
    async def launch(*args, **kwargs):
        captured['args'] = args
        captured['source'] = (Path(args[-1]) / 'sketch.ino').read_text(encoding='utf-8')
        output = Path(args[args.index('--output-dir') + 1])
        output.mkdir()
        (output / 'sketch.ino.bin').write_bytes(binary)
        return FakeProcess(code=0)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    project = HardwareService(tmp_path / 'projects').create_project(board='pi-pico')
    compiler = ArduinoCompiler(executable=str(executable))
    if status:
        with pytest.raises(HTTPException) as error:
            asyncio.run(compiler.compile(project))
        assert error.value.status_code == status
    else:
        result = asyncio.run(compiler.compile(project))
        assert base64.b64decode(result['artifact']['bin']) == binary
        assert result['compile_source'] == captured['source'] == '#define Serial Serial1\n' + DEFAULT_SOURCE
    assert captured['args'][3] == 'rp2040:rp2040:rpipico'


@pytest.mark.parametrize('board', [key for key, config in BOARD_CONFIG.items() if config['compile']])
def test_real_arduino_cli_blink(tmp_path, board):
    compiler = ArduinoCompiler()
    if not compiler.executable:
        pytest.skip('Arduino CLI is not installed.')
    service = HardwareService(tmp_path / 'real', compiler=compiler)
    project = service.create_project(board=board)
    result = asyncio.run(service.command(project['id'], 'compile_firmware'))
    config = BOARD_CONFIG[board]
    assert result['status'] == ('simulation_ready' if config['simulation'] == 'browser' else 'compilation_complete'), result['stderr']
    assert result['fqbn'] == config['fqbn']
    assert result['artifact']['format'] == config['format']
    if config['runtime'] == 'rp2040js':
        binary = base64.b64decode(result['artifact']['bin'], validate=True)
        assert len(binary) > 264
        assert result['artifact']['load_address'] == 0x10000000
        assert result['compile_source'] == '#define Serial Serial1\n' + DEFAULT_SOURCE
        stack = int.from_bytes(binary[256:260], 'little')
        entry = int.from_bytes(binary[260:264], 'little')
        assert 0x20000000 <= stack <= 0x20042000
        assert entry & 1 and 0x10000100 <= entry < 0x10200000
    elif config.get('chip'):
        binary = base64.b64decode(result['artifact']['bin'], validate=True)
        assert len(binary) == 4 * 1024 * 1024
        assert binary[config['bootloader_offset']] == binary[0x10000] == 0xe9
        assert binary[0x8000:0x8002] == b'\xaa\x50'
        assert result['artifact']['load_address'] == 0
        assert result['artifact']['image_kind'] == 'merged-flash'
        assert result['compile_source'] == project['firmware']['source']
    else:
        assert result['artifact']['hex'].startswith(':')
        assert ':00000001FF' in result['artifact']['hex']
    assert result['artifact']['source_revision'] == 1
    assert result['source'] == DEFAULT_BOARD_SOURCES.get(board, DEFAULT_SOURCE)
