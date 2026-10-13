import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import agent
from backend.hardware import HardwareService, TOOLS, create_router


def run(awaitable):
    return asyncio.run(awaitable)


def wire(wire_id, start='13', end='12', **metadata):
    return {'id': wire_id, 'from': {'component': 'board', 'pin': start},
            'to': {'component': 'board', 'pin': end}, **metadata}


@pytest.fixture
def service(tmp_path):
    return HardwareService(tmp_path / 'projects')


def stored_project(service, wires):
    project = service.create_project(board='arduino-uno')
    with service.lock:
        stored = service._load(project['id'])
        stored['wires'] = copy.deepcopy(wires)
        stored['compiler'] = {'stale': False}
        service._save(stored)
    return service.get_project(project['id'])


def check(service, project, **args):
    return run(service.command(project['id'], 'check_connections', args))


def test_default_read_only_canonical_alias_and_existing_diagnostics(service):
    project = stored_project(service, [wire('alias', 'D13'), wire('short', '5V', 'GND')])
    original = service._path(project['id']).read_bytes()
    result = check(service, project)
    assert result['status'] == 'needs_review'
    assert not result['changed'] and result['repairs'] == [] and result['recheck'] is None
    assert result['proposed_repairs'][0]['operation'] == 'canonicalize_endpoints'
    assert {item['code'] for item in result['validation']['errors']} == {'power_short', 'noncanonical_endpoint'}
    assert service._path(project['id']).read_bytes() == original
    assert result['project']['revision'] == project['revision']
    assert '_undo' not in result['project'] and 'runtime_token' not in result['validation']


def test_safe_deterministic_repair_recheck_one_save_undo_and_idempotence(service, monkeypatch):
    project = stored_project(service, [wire('first', 'D13', color='green'),
                                       wire('duplicate', '12', '13', color='green'),
                                       wire('self', 'D13', '13')])
    saves = []
    save = service._save

    def tracked(candidate):
        saves.append(copy.deepcopy(candidate))
        save(candidate)

    monkeypatch.setattr(service, '_save', tracked)
    result = check(service, project, repair=True, expected_revision=project['revision'])
    assert result['changed'] and result['status'] == 'repaired'
    assert result['validation']['valid'] and result['recheck']['errors'] == []
    assert not result['before']['valid']
    assert [item['operation'] for item in result['repairs']] == [
        'canonicalize_endpoints', 'remove_duplicate_wire', 'remove_self_wire']
    changed = result['project']
    assert changed['wires'] == [wire('first', color='green')]
    assert changed['revision'] == project['revision'] + 1
    assert result['validation']['revision'] == changed['revision']
    assert len(saves) == 1 and len(saves[0]['_undo']) == 1
    assert changed['history'][-1]['operation'] == 'check_connections'
    assert changed['compiler']['stale']
    assert changed['firmware'] == project['firmware']
    original = service._path(project['id']).read_bytes()
    replay = check(service, changed, repair=True, expected_revision=changed['revision'])
    assert not replay['changed'] and replay['status'] == 'checked'
    assert len(saves) == 1 and service._path(project['id']).read_bytes() == original
    restored = run(service.command(project['id'], 'undo'))
    assert restored['wires'] == project['wires']
    assert restored['revision'] == changed['revision'] + 1


@pytest.mark.parametrize('unrepairable', [
    wire('short', '5V', 'GND'),
    wire('invalid', 'unknown'),
    wire('alias', '11', '10'),
    wire('conflict', '12', '13', color='red'),
])
def test_ambiguous_or_electrical_errors_block_all_repairs(service, unrepairable):
    project = stored_project(service, [wire('alias', 'D13', color='green'), unrepairable])
    original = service._path(project['id']).read_bytes()
    result = check(service, project, repair=True, expected_revision=project['revision'])
    assert not result['changed'] and result['status'] == 'needs_review'
    assert result['repairs'] == [] and result['proposed_repairs']
    assert result['recheck']['errors']
    assert result['validation'] == result['before']
    assert service._path(project['id']).read_bytes() == original


@pytest.mark.parametrize('bad_wire', [None, [], {'id': 'bad'},
                                       wire('bad', end='missing'),
                                       {**wire('bad'), 'from': {'component': 'missing', 'pin': '13'}}])
def test_malformed_or_dangling_wires_report_without_deleting(service, bad_wire):
    project = stored_project(service, [bad_wire])
    original = service._path(project['id']).read_bytes()
    result = check(service, project, repair=True, expected_revision=project['revision'])
    assert not result['changed']
    assert result['validation']['errors'][0]['code'] == 'invalid_wire'
    assert service._path(project['id']).read_bytes() == original


@pytest.mark.parametrize('args', [
    {'repair': 'true'}, {'repair': 1}, {'repair': None}, {'unknown': True},
    {'repair': True}, {'repair': True, 'expected_revision': None},
    {'expected_revision': True}, {'expected_revision': -1},
])
def test_strict_arguments_no_write(service, args):
    project = service.create_project(board='arduino-uno')
    original = service._path(project['id']).read_bytes()
    with pytest.raises(HTTPException) as error:
        check(service, project, **args)
    assert error.value.status_code == 400
    assert service._path(project['id']).read_bytes() == original


def test_revision_conflict_and_concurrent_repairs(service):
    project = stored_project(service, [wire('alias', 'D13')])

    def apply():
        try:
            return check(service, project, repair=True, expected_revision=project['revision'])
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 409 in results
    assert service.get_project(project['id'])['revision'] == project['revision'] + 1


def test_warning_only_circuit_is_not_rewired(service):
    project = service.create_project(board='arduino-uno')
    project = run(service.command(project['id'], 'add_component', {'id': 'led', 'type': 'led'}))
    for start, end in [({'component': 'board', 'pin': '13'}, {'component': 'led', 'pin': 'A'}),
                       ({'component': 'led', 'pin': 'C'}, {'component': 'board', 'pin': 'GND'})]:
        project = run(service.command(project['id'], 'connect_wire', {'from': start, 'to': end}))
    original = service._path(project['id']).read_bytes()
    result = check(service, project, repair=True, expected_revision=project['revision'])
    assert result['validation']['valid'] and not result['changed']
    assert result['validation']['warnings'][0]['code'] == 'led_missing_series_resistor'
    assert service._path(project['id']).read_bytes() == original


@pytest.mark.parametrize('component', [None, {'id': 'board', 'type': 'led'},
                                      {'id': 'unknown', 'type': 'not-in-catalog'},
                                      {'id': '../bad', 'type': 'led'}])
def test_invalid_component_identity_blocks_net_analysis_and_repair(service, component):
    project = stored_project(service, [wire('alias', 'D13')])
    with service.lock:
        stored = service._load(project['id'])
        stored['components'].append(component)
        service._save(stored)
    original = service._path(project['id']).read_bytes()
    result = check(service, project, repair=True, expected_revision=project['revision'])
    assert not result['changed'] and result['proposed_repairs'] == []
    assert result['validation']['errors'][0]['code'] == 'invalid_component'
    assert result['validation']['nets'] == []
    assert service._path(project['id']).read_bytes() == original


def test_incomplete_servo_and_breadboard_limits_are_explicit(service):
    project = service.create_project(board='arduino-uno')
    project = run(service.command(project['id'], 'add_component', {'id': 'servo', 'type': 'servo'}))
    project = run(service.command(project['id'], 'add_component', {'id': 'breadboard', 'type': 'breadboard-mini'}))
    result = check(service, project)
    warnings = result['validation']['warnings']
    assert sum(item['code'] == 'unconnected_required_pin' for item in warnings) == 3
    assert {'servo_power_unverified', 'breadboard_connectivity_unverified'} <= {item['code'] for item in warnings}
    assert not result['changed'] and result['proposed_repairs'] == []


def test_command_api_and_agent_schema_registration(service):
    assert 'check_connections' in TOOLS and 'check_connections' in agent.HARDWARE_TOOL_NAMES
    schema = agent._SCHEMAS['check_connections']
    assert schema().model_dump(exclude_none=True) == {'repair': False}
    with pytest.raises(ValidationError):
        schema(repair='true')
    with pytest.raises(ValidationError):
        schema(project_id='other')
    assert 'check_connections' in agent._Plan.model_fields['operations'].annotation.__args__[0].__args__
    project = stored_project(service, [wire('alias', 'D13')])
    app = FastAPI()
    app.include_router(create_router(service))
    with TestClient(app) as client:
        url = f'/api/hardware/project/{project["id"]}/command'
        result = client.post(url, json={'name': 'check_connections', 'args': {}})
        assert result.status_code == 200 and not result.json()['changed']
        result = client.post(url, json={'name': 'check_connections', 'args': {
            'repair': True, 'expected_revision': project['revision']}})
        assert result.status_code == 200 and result.json()['changed']
