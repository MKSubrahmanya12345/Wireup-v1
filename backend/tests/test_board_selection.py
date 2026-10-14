import asyncio
import base64
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.hardware import ArduinoCompiler, BOARD_CONFIG, DEFAULT_BOARD_SOURCES, DEFAULT_SOURCE, HardwareService, PIN_LAYOUTS
from backend.tests.test_hardware import FakeProcess


@pytest.mark.parametrize('board', list(BOARD_CONFIG))
def test_empty_project_first_board_placement_and_undo(tmp_path, board):
    service = HardwareService(tmp_path)
    project = service.create_project()
    assert project['board'] == 'unselected'
    assert project['components'] == project['wires'] == project['history'] == []
    assert project['firmware']['source'] == ''
    assert project['revision'] == project['firmware']['revision'] == 1
    assert project['compiler'] is None
    assert HardwareService(tmp_path).get_project(project['id']) == project

    placed = asyncio.run(service.command(project['id'], 'add_component',
                                         {'type': board, 'x': 400, 'y': 250, 'rotation': 90,
                                          'expected_revision': 1}))
    assert placed['board'] == board
    assert placed['components'] == [{'id': 'board', 'type': board, 'x': 400, 'y': 250,
                                      'rotation': 90, 'properties': {}}]
    assert placed['firmware']['source'] == ('' if board.startswith('raspberry-pi-') else DEFAULT_BOARD_SOURCES.get(board, DEFAULT_SOURCE))
    assert placed['firmware']['filename'] == ('main.py' if board.startswith('raspberry-pi-') else 'sketch.ino')
    assert placed['revision'] == placed['firmware']['revision'] == 2
    assert placed['wires'] == []
    assert placed['compiler'] is None
    assert placed['runtime_token'] == project['runtime_token']
    assert HardwareService(tmp_path).get_project(project['id']) == placed

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'], 'add_component', {'type': board}))
    assert error.value.status_code == 409
    assert service.get_project(project['id']) == placed

    restored = asyncio.run(service.command(project['id'], 'undo', {'expected_revision': 2}))
    assert restored['board'] == 'unselected'
    assert restored['components'] == restored['wires'] == []
    assert restored['firmware']['source'] == ''
    assert restored['revision'] == restored['firmware']['revision'] == 3
    assert restored['compiler'] is None
    assert HardwareService(tmp_path).get_project(project['id']) == restored


def test_empty_project_cannot_compile_before_board_placement(tmp_path, monkeypatch):
    async def launch(*args, **kwargs):
        pytest.fail('Unselected project must not execute the host compiler.')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    service = HardwareService(tmp_path, compiler=ArduinoCompiler(executable='missing'))
    project = service.create_project()
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'], 'compile_firmware'))
    assert error.value.status_code == 400
    rejected = service.get_project(project['id'])
    assert rejected['board'] == 'unselected'
    assert rejected['components'] == []
    assert rejected['firmware']['source'] == ''
    assert rejected['revision'] == 1
    assert rejected['compiler']['status'] == 'error'
    assert rejected['compiler']['errors']
    assert rejected['compiler']['artifact'] is None


def test_board_replacement_is_revisioned_and_undoable(tmp_path):
    service = HardwareService(tmp_path)
    project = service.create_project(board='arduino-uno')
    next_project = asyncio.run(service.command(project['id'], 'add_component', {'type': 'arduino-mega', 'expected_revision': 1}))
    assert next_project['board'] == 'arduino-mega'
    assert next_project['components'][0]['id'] == 'board'
    assert next_project['components'][0]['type'] == 'arduino-mega'
    assert next_project['revision'] == 2
    restored = asyncio.run(service.command(project['id'], 'undo', {'expected_revision': 2}))
    assert restored['board'] == 'arduino-uno'
    assert restored['revision'] == 3


def test_catalog_ranks_actual_led_ahead_of_incidental_matches():
    service = HardwareService()
    assert service.catalog.search('LED red', 5)['components'][0]['id'] == 'led'
    assert service.catalog.search('wokwi-led', 5)['components'][0]['id'] == 'led'


@pytest.mark.parametrize('board', list(BOARD_CONFIG))
def test_all_registered_boards_can_be_selected_and_replaced(tmp_path, board):
    service = HardwareService(tmp_path)
    project = service.create_project(board=board)
    assert project['components'][0]['type'] == project['board'] == board
    if board != 'arduino-uno':
        replaced = asyncio.run(service.command(project['id'], 'add_component', {'type': 'arduino-uno'}))
        assert len(replaced['components']) == 1
        restored = asyncio.run(service.command(project['id'], 'undo'))
        assert restored['board'] == restored['components'][0]['type'] == board


@pytest.mark.parametrize('alias,canonical', [('raspberry-pi-pico', 'pi-pico'),
                                             ('wokwi-pi-pico', 'pi-pico'),
                                             ('raspberry-pi-pico-w', 'pi-pico-w'),
                                             ('esp32', 'esp32-devkit-v1')])
def test_board_aliases_are_canonicalized(tmp_path, alias, canonical):
    service = HardwareService(tmp_path)
    project = service.create_project(board=alias)
    assert project['board'] == canonical
    first = service.create_project(board='arduino-uno')
    replaced = asyncio.run(service.command(first['id'], 'add_component', {'type': alias}))
    assert replaced['board'] == canonical
    assert canonical in service.catalog.components
    if not alias.startswith('wokwi-'):
        assert alias in service.catalog.components[canonical]['aliases']


@pytest.mark.parametrize('board', ['raspberry-pi-3', 'raspberry-pi-4', 'raspberry-pi-5'])
def test_unavailable_boards_never_dispatch_browser_runtime(tmp_path, board):
    class RejectRuntime:
        async def command(self, *args):
            pytest.fail('Unavailable board dispatched to browser runtime.')
    service = HardwareService(tmp_path, runtime=RejectRuntime())
    project = service.create_project(board=board)
    for command in ('run_simulation', 'stop_simulation', 'read_simulation_results'):
        with pytest.raises(HTTPException) as error:
            asyncio.run(service.command(project['id'], command, runtime_token=project['runtime_token']))
        assert error.value.status_code == 503
        assert error.value.detail == BOARD_CONFIG[board]['unavailable_reason']


@pytest.mark.parametrize('board', ['raspberry-pi-3', 'raspberry-pi-4', 'raspberry-pi-5'])
def test_linux_boards_reject_compilation_before_starting_cli(tmp_path, monkeypatch, board):
    async def launch(*args, **kwargs):
        pytest.fail('Linux board must not execute the host compiler.')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    service = HardwareService(tmp_path, compiler=ArduinoCompiler(executable='missing'))
    project = service.create_project(board=board)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'], 'compile_firmware'))
    assert error.value.status_code == 400
    assert 'guest Linux image' in error.value.detail
    assert service.get_project(project['id'])['compiler']['artifact'] is None
    assert not BOARD_CONFIG[board]['compile']


@pytest.mark.parametrize('board,count,valid,invalid', [
    ('esp32-devkit-v1', 30, 'RX2', 'D0'),
    ('esp32-devkit-c-v4', 38, 'CMD', '24'),
    ('esp32-s3', 44, '48', '22'),
    ('esp32-c3', 30, 'GND.10', '11'),
    ('pi-pico-w', 40, 'GP28', 'GP25'),
])
def test_board_pin_layout_and_wire_validation(tmp_path, board, count, valid, invalid):
    service = HardwareService(tmp_path)
    assert len(PIN_LAYOUTS[board]) == count
    project = service.create_project(board=board)
    asyncio.run(service.command(project['id'], 'add_component', {'id': 'led', 'type': 'led'}))
    wire = {'from': {'component': 'board', 'pin': valid}, 'to': {'component': 'led', 'pin': 'A'}}
    asyncio.run(service.command(project['id'], 'connect_wire', wire))
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'], 'add_component', {'type': 'arduino-uno'}))
    assert error.value.status_code == 409
    wire['from']['pin'] = invalid
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'], 'connect_wire', wire))
    assert error.value.status_code == 400


@pytest.mark.parametrize('board', ['esp32-devkit-v1', 'esp32-devkit-c-v4', 'esp32-s3', 'esp32-c3'])
@pytest.mark.parametrize('mode', ['valid', 'sketch-only', 'wrong-chip', 'bad-partitions', 'bad-merge', 'short-flash'])
def test_esp32_compiler_full_flash_contract(tmp_path, monkeypatch, board, mode):
    executable = tmp_path / 'arduino-cli.exe'
    executable.touch()
    config = BOARD_CONFIG[board]
    captured = {}
    async def launch(*args, **kwargs):
        captured['args'] = args
        captured['source'] = (Path(args[-1]) / 'sketch.ino').read_text(encoding='utf-8')
        output = Path(args[args.index('--output-dir') + 1])
        build = Path(args[args.index('--build-path') + 1])
        output.mkdir()
        build.mkdir()
        flash = bytearray(b'\xff' * config['flash_size_bytes'])
        chip_id = {'esp32': 0, 'esp32s3': 9, 'esp32c3': 5}[config['chip']]
        image = bytearray(256)
        image[0] = 0xe9
        image[12:14] = (99 if mode == 'wrong-chip' else chip_id).to_bytes(2, 'little')
        for offset, name, data in [
            (config['bootloader_offset'], 'sketch.ino.bootloader.bin', image),
            (0x8000, 'sketch.ino.partitions.bin', b'bad' if mode == 'bad-partitions' else b'\xaa\x50' + bytes(30)),
            (0xe000, 'boot_app0.bin', bytes(32)),
            (0x10000, 'sketch.ino.bin', image),
        ]:
            (build if name == 'boot_app0.bin' else output).joinpath(name).write_bytes(data)
            flash[offset:offset + len(data)] = data
        if mode == 'bad-merge':
            flash[0x10000] = 0
        if mode != 'sketch-only':
            (output / 'sketch.ino.merged.bin').write_bytes(flash[:-1] if mode == 'short-flash' else flash)
        return FakeProcess(code=0)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    profile = tmp_path / 'arduino-cli.yaml'
    profile.write_text('directories: {}\n', encoding='utf-8')
    boot_app = tmp_path / 'boot_app0.bin'
    boot_app.write_bytes(bytes(32))
    service = HardwareService(tmp_path / 'projects', compiler=ArduinoCompiler(
        executable=str(executable), classic_esp32_config=profile, classic_esp32_boot_app=boot_app))
    project = service.create_project(board=board)
    if mode not in ('valid', 'sketch-only'):
        with pytest.raises(HTTPException) as error:
            asyncio.run(service.command(project['id'], 'compile_firmware'))
        assert error.value.status_code == 502
        assert service.get_project(project['id'])['compiler']['status'] == 'error'
        return
    result = asyncio.run(service.command(project['id'], 'compile_firmware'))
    assert captured['source'] == result['compile_source'] == project['firmware']['source']
    assert captured['args'][captured['args'].index('--fqbn') + 1] == config['fqbn']
    if board in ('esp32-devkit-v1', 'esp32-devkit-c-v4'):
        assert '--config-file' in captured['args']
    else:
        assert '--config-file' not in captured['args']
    if mode == 'sketch-only' and board in ('esp32-c3', 'esp32-s3'):
        assert result['status'] == 'error'
        assert result['artifact'] is None
        assert 'merged.bin' in result['errors'][0]
        return
    assert result['status'] == 'simulation_ready'
    assert result['simulation'] == ('browser' if board in ('esp32-c3', 'esp32-s3') else 'native')
    artifact = result['artifact']
    assert artifact['format'] == 'bin'
    assert artifact['image_kind'] == 'merged-flash'
    assert artifact['load_address'] == 0
    assert len(base64.b64decode(artifact['bin'])) == 4 * 1024 * 1024
    assert [segment['offset'] for segment in artifact['flash_segments']] == [config['bootloader_offset'], 0x8000, 0xe000, 0x10000]
    assert service.get_artifact(project['id'], artifact['id']) == artifact


def test_esp32_pin_names_match_upstream_elements():
    root = Path(__file__).resolve().parents[2]
    wokwi = root / 'node_modules/@wokwi/elements/dist/esm/esp32-devkit-v1-element.js'
    wrapper = root / 'vendor/velxio/frontend/src/components/velxio-components/Esp32Element.ts'
    if not wokwi.is_file() or not wrapper.is_file():
        pytest.skip('Upstream board elements are unavailable.')
    assert set(PIN_LAYOUTS['esp32-devkit-v1']) == set(re.findall(r"\{ name: '([^']+)'", wokwi.read_text(encoding='utf-8')))
    source = wrapper.read_text(encoding='utf-8')
    for board, constant in [('esp32-devkit-c-v4', 'PINS_ESP32_DEVKIT_C_V4'),
                            ('esp32-s3', 'PINS_ESP32_S3'), ('esp32-c3', 'PINS_ESP32_C3')]:
        block = re.search(rf'const {constant} = \[(.*?)\];', source, re.DOTALL).group(1)
        assert PIN_LAYOUTS[board] == re.findall(r"\{ name: '([^']+)'", block)


def test_replacement_coordinates_and_default_source_are_board_specific(tmp_path):
    service = HardwareService(tmp_path)
    project = service.create_project(board='arduino-uno')
    changed = asyncio.run(service.command(project['id'], 'add_component',
                                           {'type': 'esp32-devkit-c-v4', 'x': 400, 'y': 250, 'rotation': 90}))
    assert changed['components'][0]['x'] == 400
    assert changed['components'][0]['y'] == 250
    assert changed['components'][0]['rotation'] == 90
    assert 'LED_BUILTIN' not in changed['firmware']['source']
    custom = 'void setup() {} void loop() {}'
    asyncio.run(service.command(project['id'], 'edit_firmware', {'source': custom}))
    changed = asyncio.run(service.command(project['id'], 'add_component', {'type': 'esp32-s3'}))
    assert changed['firmware']['source'] == custom


def test_pico_w_reports_verified_cpu_runtime_without_radio():
    entry = HardwareService().catalog.components['pi-pico-w']
    assert entry['fqbn'] == 'rp2040:rp2040:rpipicow'
    assert entry['compile'] is True
    assert entry['wifi'] is entry['bluetooth'] is False
    assert entry['simulation'] == 'browser'
    assert 'no CYW43' in entry['simulation_scope']
