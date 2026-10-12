import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.hardware import HardwareService, RuntimeBridge, create_router
from backend.tests.test_hardware import FakeCompiler


@pytest.fixture
def service(tmp_path):
    return HardwareService(tmp_path / 'projects', compiler=FakeCompiler(), runtime=RuntimeBridge())


@pytest.fixture
def client(service):
    app = FastAPI()
    app.include_router(create_router(service))
    with TestClient(app) as client:
        yield client


def url(project):
    return f'/api/hardware/projects/{project["id"]}/build-guide'


def request(project, guide, **changes):
    return {'runtime_token': project['runtime_token'], 'version': guide['version'],
            'expected_progress_revision': guide['progress']['revision'], **changes}


def get(client, project):
    response = client.get(url(project))
    assert response.status_code == 200
    return response.json()


def mutate(service, project, name, args):
    return asyncio.run(service.command(project['id'], name, args))


def test_empty_project_is_structured_private_and_deterministic(service, client):
    project = service.create_project()
    guide = get(client, project)
    assert guide == get(client, project)
    assert guide['schema_version'] == 1
    assert guide['project_id'] == project['id']
    assert guide['project_revision'] == guide['firmware_revision'] == 1
    assert [step['id'] for step in guide['steps']] == ['introduction', 'preparation', 'wiring', 'verification', 'firmware', 'simulation', 'testing', 'completion']
    assert all(isinstance(step['instructions'], list) and isinstance(step['warnings'], list) and step['why'] for step in guide['steps'])
    assert guide['progress']['current_step'] == 'introduction'
    assert guide['progress']['revision'] == 1
    assert all(not state['confirmed'] and not state['outdated'] for state in guide['progress']['checks'].values())
    assert all(item['status'] == 'not_checked' for item in guide['progress']['evidence'].values())
    assert service.get_project(project['id']) == project
    assert '_build_guide' not in client.get(f'/api/hardware/projects/{project["id"]}').json()
    saved = json.loads(service._path(project['id']).read_text(encoding='utf-8'))
    assert saved['_build_guide']['progress'] == guide['progress']
    reloaded = HardwareService(service.data_dir)
    from backend.build_guide import synchronize
    with reloaded.lock:
        assert synchronize(reloaded, reloaded._load(project['id'])) == guide


def test_instructions_use_saved_inventory_connections_and_source(service, client):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'add_component', {'id': 'led1', 'type': 'led'})
    mutate(service, project, 'connect_wire', {'id': 'signal', 'from': {'component': 'board', 'pin': '13'}, 'to': {'component': 'led1', 'pin': 'A'}, 'color': 'red'})
    mutate(service, project, 'connect_wire', {'id': 'return', 'from': {'component': 'led1', 'pin': 'C'}, 'to': {'component': 'board', 'pin': 'GND'}})
    source = '#include <Wire.h>\nvoid setup() {}\nvoid loop() { delay(7); }'
    mutate(service, project, 'edit_firmware', {'source': source})
    guide = get(client, project)
    steps = {step['id']: step for step in guide['steps']}
    preparation = json.dumps(steps['preparation']['sections'])
    assert 'LED' in preparation
    wiring = ' '.join(action for section in steps['wiring']['sections'] for action in section['actions'])
    assert 'Arduino Uno pin 13 to LED pin A' in wiring
    assert 'LED pin C to Arduino Uno pin GND.1' in wiring
    assert 'led1' not in wiring
    explanation = ' '.join(steps['firmware']['instructions']) + json.dumps(steps['firmware']['sections'])
    assert 'setup' in explanation and 'loop' in explanation
    assert 'unknown' in explanation.lower()
    assert 'blink' not in explanation.lower()


def test_progress_persists_without_circuit_history_or_revision_changes(service, client):
    project = service.create_project(board='arduino-uno')
    guide = get(client, project)
    before = service._load(project['id'])
    response = client.patch(url(project) + '/progress', json=request(project, guide, current_step='wiring', checks={'preparation.inventory': True}))
    assert response.status_code == 200
    updated = response.json()
    assert updated['progress']['revision'] == 2
    assert updated['progress']['current_step'] == 'wiring'
    assert updated['progress']['checks']['preparation.inventory']['confirmed_at']
    assert updated == get(client, project)
    after = service._load(project['id'])
    assert {key: value for key, value in before.items() if key != '_build_guide'} == {key: value for key, value in after.items() if key != '_build_guide'}
    repeat = client.patch(url(project) + '/progress', json=request(project, updated, checks={'preparation.inventory': True})).json()
    assert repeat == updated
    cleared = client.patch(url(project) + '/progress', json=request(project, updated, checks={'preparation.inventory': False})).json()
    assert cleared['progress']['checks']['preparation.inventory'] == {'confirmed': False, 'outdated': False}


@pytest.mark.parametrize('suffix,changes,status', [
    ('/progress', {'runtime_token': 'wrong'}, 403),
    ('/verify', {'runtime_token': 'wrong'}, 403),
    ('/progress', {'version': 'stale'}, 409),
    ('/verify', {'expected_progress_revision': 99}, 409),
    ('/progress', {'current_step': 'missing'}, 400),
    ('/progress', {'checks': {'topology': True}}, 400),
    ('/progress', {'checks': {'preparation.inventory': {'confirmed': True}}}, 422),
    ('/progress', {'checks': {'preparation.inventory': 'true'}}, 422),
    ('/progress', {'evidence': {'topology': {'status': 'passed'}}}, 422),
    ('/verify', {'status': 'passed'}, 422),
    ('/verify', {'checks': ['unknown']}, 400),
    ('/verify', {'checks': ['topology', 'topology']}, 400),
    ('/verify', {'checks': []}, 422),
    ('/progress', {'expected_progress_revision': True}, 422),
])
def test_rejects_unauthorized_stale_or_client_automatic_status(service, client, suffix, changes, status):
    project = service.create_project(board='arduino-uno')
    guide = get(client, project)
    method = client.patch if suffix == '/progress' else client.post
    assert method(url(project) + suffix, json=request(project, guide, **changes)).status_code == status
    assert get(client, project) == guide


@pytest.mark.parametrize('suffix', ['/progress', '/verify'])
def test_missing_token_rejected(service, client, suffix):
    project = service.create_project()
    guide = get(client, project)
    payload = request(project, guide)
    payload.pop('runtime_token')
    method = client.patch if suffix == '/progress' else client.post
    assert method(url(project) + suffix, json=payload).status_code == 422


def test_limited_topology_evidence_and_simulation_unavailable(service, client):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'connect_wire', {'id': 'short', 'from': {'component': 'board', 'pin': '5V'}, 'to': {'component': 'board', 'pin': 'GND'}})
    guide = get(client, project)
    before = service.get_project(project['id'])
    service.runtime.sessions[project['id']] = object()
    verified = client.post(url(project) + '/verify', json=request(project, guide)).json()
    evidence = verified['progress']['evidence']
    assert evidence['topology']['status'] == 'failed'
    assert evidence['topology']['details']['errors'][0]['code'] == 'power_short'
    assert 'general electrical correctness' in evidence['topology']['scope']
    assert evidence['compile']['status'] == 'failed'
    assert evidence['simulation']['status'] == 'unavailable'
    assert 'not proof' in evidence['simulation']['scope']
    assert all(item['checked_at'] for item in evidence.values())
    assert not any(state['confirmed'] for state in verified['progress']['checks'].values())
    assert service.get_project(project['id']) == before
    mutate(service, project, 'remove_wire', {'id': 'short'})
    guide = get(client, project)
    assert guide['progress']['evidence']['topology']['status'] == 'outdated'
    verified = client.post(url(project) + '/verify', json=request(project, guide, checks=['topology'])).json()
    assert verified['progress']['evidence']['topology']['status'] == 'passed'
    assert verified['progress']['evidence']['simulation']['status'] == 'outdated'


def test_compile_requires_real_current_successful_artifact(service, client):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'compile_firmware', {})
    guide = get(client, project)
    verified = client.post(url(project) + '/verify', json=request(project, guide, checks=['compile'])).json()
    assert verified['progress']['evidence']['compile']['status'] == 'passed'
    assert verified['progress']['evidence']['compile']['artifact_id'] == 'test-artifact'
    assert service.get_project(project['id'])['revision'] == 1
    mutate(service, project, 'modify_component', {'id': 'board', 'x': 30})
    guide = get(client, project)
    assert guide['progress']['evidence']['compile']['status'] == 'outdated'
    failed = client.post(url(project) + '/verify', json=request(project, guide, checks=['compile'])).json()
    assert failed['progress']['evidence']['compile']['status'] == 'failed'


@pytest.mark.parametrize('corruption', ['missing_store', 'missing_payload', 'wrong_board', 'wrong_source', 'wrong_revision', 'failed_status', 'stale', 'errors'])
def test_compile_rejects_corrupt_or_mismatched_artifacts(service, client, corruption):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'compile_firmware', {})
    saved = service._load(project['id'])
    artifact = saved['compiler']['artifact']
    if corruption == 'missing_store':
        saved['_artifacts'] = {}
    elif corruption == 'missing_payload':
        artifact.pop('hex')
        saved['_artifacts'][artifact['id']] = copy.deepcopy(artifact)
    elif corruption == 'wrong_board':
        artifact['board'] = 'pi-pico'
        saved['_artifacts'][artifact['id']] = copy.deepcopy(artifact)
    elif corruption == 'wrong_source':
        saved['compiler']['source'] = 'different'
    elif corruption == 'wrong_revision':
        artifact['project_revision'] = 0
        saved['_artifacts'][artifact['id']] = copy.deepcopy(artifact)
    elif corruption == 'failed_status':
        saved['compiler']['status'] = 'error'
    elif corruption == 'stale':
        saved['compiler']['stale'] = True
    else:
        saved['compiler']['errors'] = ['failure']
    service._save(saved)
    guide = get(client, project)
    result = client.post(url(project) + '/verify', json=request(project, guide, checks=['compile']))
    assert result.status_code == 200
    assert result.json()['progress']['evidence']['compile']['status'] == 'failed'


def test_independent_fingerprints_stale_write_and_reconfirmation(service, client):
    project = service.create_project(board='arduino-uno')
    guide = get(client, project)
    confirmed = client.patch(url(project) + '/progress', json=request(project, guide, checks={key: True for key in guide['progress']['checks']})).json()
    mutate(service, project, 'edit_firmware', {'source': 'void setup() {} void loop() {}'})
    assert client.patch(url(project) + '/progress', json=request(project, confirmed, current_step='test')).status_code == 409
    updated = get(client, project)
    assert updated['version'] != confirmed['version']
    assert updated['progress']['revision'] == confirmed['progress']['revision'] + 1
    assert updated['progress']['checks']['preparation.inventory']['confirmed']
    assert updated['progress']['checks']['wiring.wiring_review']['confirmed']
    assert updated['progress']['checks']['firmware.source']['outdated']
    assert not updated['progress']['checks']['firmware.source']['confirmed']
    assert 'firmware' in updated['progress']['outdated_steps']
    assert get(client, project) == updated
    reconfirmed = client.patch(url(project) + '/progress', json=request(project, updated, checks={'firmware.source': True})).json()
    assert 'firmware' not in reconfirmed['progress']['outdated_steps']
    assert reconfirmed['progress']['checks']['firmware.source']['outdated'] is False
    mutate(service, project, 'undo', {})
    undone = get(client, project)
    assert undone['progress']['checks']['firmware.source']['outdated']
    assert undone['progress']['checks']['preparation.inventory']['confirmed']


def test_property_and_board_changes_invalidate_relevant_steps(service, client):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'add_component', {'id': 'r1', 'type': 'resistor'})
    guide = get(client, project)
    confirmed = client.patch(url(project) + '/progress', json=request(project, guide, checks={'preparation.inventory': True, 'wiring.wiring_review': True, 'firmware.source': True})).json()
    mutate(service, project, 'modify_component', {'id': 'r1', 'properties': {'value': '220'}})
    updated = get(client, project)
    assert updated['progress']['checks']['preparation.inventory']['outdated']
    assert updated['progress']['checks']['wiring.wiring_review']['outdated']
    assert updated['progress']['checks']['firmware.source']['confirmed']
    assert updated['version'] != confirmed['version']
    mutate(service, project, 'add_component', {'type': 'pi-pico'})
    updated = get(client, project)
    assert updated['progress']['checks']['firmware.source']['outdated']


def test_verify_stale_write_synchronizes_before_rejecting(service, client):
    project = service.create_project(board='arduino-uno')
    guide = get(client, project)
    guide = client.post(url(project) + '/verify', json=request(project, guide, checks=['topology'])).json()
    mutate(service, project, 'add_component', {'id': 'led1', 'type': 'led'})
    assert client.post(url(project) + '/verify', json=request(project, guide, checks=['topology'])).status_code == 409
    saved = service._load(project['id'])['_build_guide']['progress']
    assert saved['evidence']['topology']['status'] == 'outdated'
    assert saved['revision'] == guide['progress']['revision'] + 1


def test_concurrent_progress_writers_have_one_winner(service, client):
    project = service.create_project(board='arduino-uno')
    guide = get(client, project)
    payload = request(project, guide, checks={'preparation.inventory': True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.patch(url(project) + '/progress', json=payload).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert get(client, project)['progress']['revision'] == 2


@pytest.mark.parametrize('board', ['pi-pico', 'esp32-devkit-v1'])
def test_compile_accepts_current_artifacts_independent_of_simulator(service, client, board):
    project = service.create_project(board=board)
    mutate(service, project, 'compile_firmware', {})
    if board == 'esp32-devkit-v1':
        saved = service._load(project['id'])
        saved['compiler']['status'] = 'compilation_complete'
        artifact = saved['compiler']['artifact']
        artifact.pop('hex')
        artifact.update(format='bin', bin='AA==')
        saved['_artifacts'][artifact['id']] = copy.deepcopy(artifact)
        service._save(saved)
    guide = get(client, project)
    response = client.post(url(project) + '/verify', json=request(project, guide))
    assert response.status_code == 200
    evidence = response.json()['progress']['evidence']
    assert evidence['compile']['status'] == 'passed'
    assert evidence['simulation']['status'] == 'unavailable'


def test_recompilation_invalidates_compile_confirmation_and_evidence(service, client):
    project = service.create_project(board='arduino-uno')
    guide = get(client, project)
    guide = client.patch(url(project) + '/progress', json=request(project, guide, checks={'simulation.compile': True, 'firmware.source': True})).json()
    mutate(service, project, 'compile_firmware', {})
    refreshed = get(client, project)
    assert refreshed['progress']['checks']['simulation.compile']['outdated']
    assert refreshed['progress']['checks']['firmware.source']['confirmed']
    assert refreshed['progress']['revision'] == guide['progress']['revision'] + 1
    assert service.get_project(project['id'])['revision'] == 1


def test_removed_wire_checklist_is_pruned_and_remaining_wires_are_invalidated(service, client):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'add_component', {'id': 'led1', 'type': 'led'})
    for wire_id, board_pin, led_pin in [('signal', '13', 'A'), ('return', 'GND', 'C')]:
        mutate(service, project, 'connect_wire', {'id': wire_id, 'from': {'component': 'board', 'pin': board_pin}, 'to': {'component': 'led1', 'pin': led_pin}})
    guide = get(client, project)
    guide = client.patch(url(project) + '/progress', json=request(project, guide, checks={'wiring.wire.signal': True, 'wiring.wire.return': True})).json()
    mutate(service, project, 'remove_wire', {'id': 'signal'})
    updated = get(client, project)
    assert 'wiring.wire.signal' not in updated['progress']['checks']
    assert updated['progress']['checks']['wiring.wire.return']['outdated']
    assert updated['progress']['revision'] == guide['progress']['revision'] + 1
    assert client.patch(url(project) + '/progress', json=request(project, updated, checks={'wiring.wire.signal': True})).status_code == 400


def test_cosmetic_changes_preserve_electrical_confirmations_and_saved_version(service, client):
    project = service.create_project(board='arduino-uno')
    mutate(service, project, 'add_component', {'id': 'led1', 'type': 'led'})
    mutate(service, project, 'connect_wire', {'id': 'signal', 'from': {'component': 'board', 'pin': '13'}, 'to': {'component': 'led1', 'pin': 'A'}})
    guide = get(client, project)
    guide = client.patch(url(project) + '/progress', json=request(project, guide, checks={'preparation.inventory': True, 'wiring.wire.signal': True})).json()
    mutate(service, project, 'modify_component', {'id': 'led1', 'x': 600, 'y': 400, 'rotation': 90})
    updated = get(client, project)
    assert updated['progress']['checks']['preparation.inventory']['confirmed']
    assert updated['progress']['checks']['wiring.wire.signal']['confirmed']
    assert 'preparation' not in updated['progress']['outdated_steps']
    assert 'wiring' not in updated['progress']['outdated_steps']
    saved = service._load(project['id'])
    saved['wires'][0]['color'] = 'blue'
    service._save(saved)
    recolored = get(client, project)
    assert recolored['progress']['checks']['wiring.wire.signal']['confirmed']
    assert 'wiring' not in recolored['progress']['outdated_steps']
    assert service._load(project['id'])['_build_guide']['version'] == recolored['version']


def test_unknown_project_and_local_only_routes(client):
    assert client.get('/api/hardware/projects/missing/build-guide').status_code == 404
    assert client.get('/api/hardware/projects/missing/build-guide', headers={'Origin': 'https://evil.example'}).status_code == 403
