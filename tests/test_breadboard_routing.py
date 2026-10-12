import asyncio
import copy
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from backend.hardware import HardwareService, RuntimeBridge, TOOLS


@pytest.fixture
def service(tmp_path):
    return HardwareService(tmp_path / 'projects', runtime=RuntimeBridge(timeout=0.2))


def run(awaitable):
    return asyncio.run(awaitable)


def command(service, project, name, args=None):
    return run(service.command(project['id'], name, args))


def wire(left, left_pin, right, right_pin, color='green'):
    return {'from': {'component': left, 'pin': left_pin},
            'to': {'component': right, 'pin': right_pin}, 'color': color}


def resistor_project(service):
    project = service.create_project(board='arduino-uno')
    project = command(service, project, 'add_component', {'id': 'r1', 'type': 'resistor'})
    for item in [wire('board', '13', 'r1', '1', 'red'),
                 wire('r1', '2', 'board', 'GND.1', 'black')]:
        project = command(service, project, 'connect_wire', item)
    return project


def electrical_partition(service, project, original_parts):
    parents, originals = {}, set()

    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            key = parents[key]
        return key

    def union(left, right):
        parents[root(right)] = root(left)

    for part in project['components']:
        internal = {}
        for pin in service.catalog.components[part['type']]['pins']:
            key = part['id'], pin
            root(key)
            if part['id'] in original_parts:
                originals.add(key)
            group = None
            if part['type'] in ('breadboard', 'breadboard-mini'):
                hole = re.fullmatch(r'(\d+[tb])\.[a-j]', pin)
                rail = re.fullmatch(r'([tb][pn])\.\d+', pin)
                group = hole[1] if hole else rail[1] if rail else None
            elif part['type'] in ('pushbutton', 'pushbutton-6mm'):
                group = pin.split('.')[0]
            if group is not None:
                union(key, internal.setdefault(group, key))
    for item in project['wires']:
        union((item['from']['component'], item['from']['pin']),
              (item['to']['component'], item['to']['pin']))
    groups = {}
    for key in originals:
        groups.setdefault(root(key), set()).add(key)
    return {frozenset(members) for members in groups.values()}


@pytest.mark.parametrize('board,with_part,with_wire', [
    ('unselected', False, False), ('arduino-uno', False, False),
    ('arduino-uno', True, False), ('arduino-uno', False, True),
])
def test_no_external_parts_or_wires_is_unchanged(service, board, with_part, with_wire):
    project = service.create_project(board=board)
    if with_part:
        project = command(service, project, 'add_component', {'id': 'r1', 'type': 'resistor'})
    if with_wire:
        project = command(service, project, 'connect_wire', wire('board', '13', 'board', '12'))
    before = service._path(project['id']).read_bytes()
    result = command(service, project, 'route_breadboard')
    assert result['project'] == project
    assert result['changed'] is False and result['existing'] is False
    assert result['breadboard_id'] is None and result['reason']
    assert service._path(project['id']).read_bytes() == before


@pytest.mark.parametrize('kind', ['breadboard', 'breadboard-mini'])
def test_existing_breadboard_is_not_rewritten(service, kind):
    project = resistor_project(service)
    project = command(service, project, 'add_component', {'id': 'bb1', 'type': kind})
    project = command(service, project, 'connect_wire', wire('board', '12', 'bb1', '1t.a'))
    before = service._path(project['id']).read_bytes()
    result = command(service, project, 'route_breadboard', {'expected_revision': project['revision']})
    assert result['project'] == project and not result['changed']
    assert result['existing'] and result['breadboard_id'] == 'bb1'
    assert service._path(project['id']).read_bytes() == before


def test_actual_routing_distinct_resistor_nets_one_save_and_undo(service, monkeypatch):
    project = resistor_project(service)
    with service.lock:
        stored = service._load(project['id'])
        stored['compiler'] = {'stale': False, 'source': project['firmware']['source']}
        stored['_artifacts'] = {'artifact': {'program': 'preserved', 'project_revision': project['revision']}}
        service._save(stored)
    before = copy.deepcopy(stored)
    saves, save = [], service._save

    def tracked_save(candidate):
        saves.append(copy.deepcopy(candidate))
        save(candidate)

    monkeypatch.setattr(service, '_save', tracked_save)
    service.runtime.sessions[project['id']] = object()
    result = command(service, project, 'route_breadboard', {'expected_revision': project['revision']})
    changed = result['project']
    assert result['changed'] and not result['existing']
    assert len(saves) == 1
    assert changed['revision'] == project['revision'] + 1
    assert changed['updated_at'] != project['updated_at']
    assert changed['history'][:-1] == project['history']
    assert changed['history'][-1]['operation'] == 'route_breadboard'
    assert len(saves[0]['_undo']) == len(before['_undo']) + 1
    assert changed['compiler']['stale'] and changed['firmware'] == project['firmware']
    assert saves[0]['_artifacts'] == before['_artifacts']
    assert '_undo' not in changed and '_artifacts' not in changed
    bb = next(part for part in changed['components'] if part['id'] == result['breadboard_id'])
    assert bb == {'id': result['breadboard_id'], 'type': 'breadboard-mini', 'x': 370,
                  'y': 300, 'rotation': 0,
                  'properties': service.catalog.components['breadboard-mini']['defaultValues']}
    assert changed['components'][:-1] == project['components']
    assert len(changed['wires']) == 4
    assert not {item['id'] for item in changed['wires']} & {item['id'] for item in project['wires']}
    assert len({item['to']['pin'] for item in changed['wires']}) == 4
    assert all(item['to']['component'] == bb['id'] for item in changed['wires'])
    for item in changed['wires']:
        assert service._endpoint(changed, item['from']) == item['from']
        assert service._endpoint(changed, item['to']) == item['to']
        assert item['color'] == ('red' if item['from']['pin'] in ('13', '1') else 'black')
    original_ids = {part['id'] for part in project['components']}
    assert electrical_partition(service, changed, original_ids) == electrical_partition(service, project, original_ids)
    routed_bytes = service._path(project['id']).read_bytes()
    assert not command(service, changed, 'route_breadboard')['changed']
    assert service._path(project['id']).read_bytes() == routed_bytes
    restored = command(service, changed, 'undo')
    for key in ('name', 'board', 'components', 'wires', 'firmware'):
        assert restored[key] == project[key]
    assert service._load(project['id'])['_undo'] == before['_undo']


@pytest.mark.parametrize('kind', ['pushbutton', 'pushbutton-6mm'])
def test_pushbutton_internal_pairs_and_transitive_edges_preserved(service, kind):
    project = resistor_project(service)
    project = command(service, project, 'add_component', {'id': 'sw', 'type': kind})
    for item in [wire('board', '12', 'sw', '1.l'), wire('sw', '1.r', 'board', '11'),
                 wire('sw', '2.l', 'board', 'GND.1')]:
        project = command(service, project, 'connect_wire', item)
    changed = command(service, project, 'route_breadboard')['project']
    original_ids = {part['id'] for part in project['components']}
    assert electrical_partition(service, changed, original_ids) == electrical_partition(service, project, original_ids)
    first_side = [item for item in changed['wires'] if item['from'] in (
        {'component': 'sw', 'pin': '1.l'}, {'component': 'sw', 'pin': '1.r'})]
    assert len(first_side) == 2
    assert first_side[0]['to']['pin'].split('.')[0] == first_side[1]['to']['pin'].split('.')[0]


def test_five_endpoint_net_uses_five_unique_holes(service):
    project = resistor_project(service)
    for item in [wire('board', '12', 'r1', '1'), wire('board', '11', 'r1', '1'),
                 wire('board', '10', 'r1', '1'), wire('board', '12', 'board', '11')]:
        project = command(service, project, 'connect_wire', item)
    result = command(service, project, 'route_breadboard')
    changed = result['project']
    assert result['changed'] and len(changed['wires']) == 7
    original_ids = {part['id'] for part in project['components']}
    assert electrical_partition(service, changed, original_ids) == electrical_partition(service, project, original_ids)
    groups = {}
    for item in changed['wires']:
        groups.setdefault(item['to']['pin'].split('.')[0], set()).add(item['to']['pin'])
    assert sorted(map(len, groups.values())) == [2, 5]


@pytest.mark.parametrize('revision', [0, True, '5', 999])
def test_revision_conflict_no_mutation(service, revision):
    project = resistor_project(service)
    before = service._path(project['id']).read_bytes()
    with pytest.raises(HTTPException) as error:
        command(service, project, 'route_breadboard', {'expected_revision': revision})
    assert error.value.status_code == 409
    assert service._path(project['id']).read_bytes() == before


@pytest.mark.parametrize('failure', ['large_net', 'too_few_strips', 'invalid_original_pin', 'invalid_catalog_pin', 'component_limit'])
def test_capacity_and_invalid_endpoints_fail_atomically(service, monkeypatch, failure):
    project = resistor_project(service)
    if failure == 'large_net':
        for pin in ('12', '11', '10', '9'):
            project = command(service, project, 'connect_wire', wire('board', pin, 'r1', '1'))
    elif failure == 'too_few_strips':
        monkeypatch.setitem(service.catalog.components['breadboard-mini'], 'pins',
                            ['1t.a', '1t.b', '1t.c', '1t.d', '1t.e'])
    elif failure == 'invalid_catalog_pin':
        endpoint = service._endpoint

        def checked_endpoint(candidate, value):
            if value.get('component') == 'breadboard':
                raise HTTPException(400, 'Invalid catalog pin.')
            return endpoint(candidate, value)

        monkeypatch.setattr(service, '_endpoint', checked_endpoint)
    else:
        stored = service._load(project['id'])
        if failure == 'invalid_original_pin':
            stored['wires'][-1]['to']['pin'] = 'nonexistent'
        else:
            stored['components'].extend({'id': f'extra{i}', 'type': 'resistor', 'x': 0,
                                         'y': 0, 'rotation': 0, 'properties': {}} for i in range(198))
        service._save(stored)
    before = service._path(project['id']).read_bytes()
    saves = []
    monkeypatch.setattr(service, '_save', lambda candidate: saves.append(candidate))
    with pytest.raises(HTTPException) as error:
        command(service, project, 'route_breadboard')
    assert error.value.status_code == 400
    assert not saves and service._path(project['id']).read_bytes() == before


def test_unique_breadboard_id_and_manual_only_command(service):
    assert 'route_breadboard' not in TOOLS
    project = resistor_project(service)
    project = command(service, project, 'add_component', {'id': 'breadboard', 'type': 'led'})
    result = command(service, project, 'route_breadboard')
    assert result['changed'] and result['breadboard_id'] != 'breadboard'
    assert len({part['id'] for part in result['project']['components']}) == len(result['project']['components'])


def test_concurrent_revision_checked_routing_commits_once(service):
    project = resistor_project(service)

    def apply(_):
        try:
            return command(service, project, 'route_breadboard', {'expected_revision': project['revision']})
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, range(2)))
    assert sum(isinstance(result, dict) and result['changed'] for result in results) == 1
    assert 409 in results
    assert service.get_project(project['id'])['revision'] == project['revision'] + 1


@pytest.mark.parametrize('args', [[], {'preview': True}, {'x': 0}])
def test_unknown_arguments_no_mutation(service, args):
    project = resistor_project(service)
    before = service._path(project['id']).read_bytes()
    with pytest.raises(HTTPException) as error:
        command(service, project, 'route_breadboard', args)
    assert error.value.status_code == 400
    assert service._path(project['id']).read_bytes() == before
