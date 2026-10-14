import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from backend import agent, feasibility
from backend.hardware import HardwareService


SOURCE = 'void setup() {}\nvoid loop() { delay(1000); }\n'
UPDATED = SOURCE.replace('1000', '500')


def call(name, args=None, identity=None):
    return AIMessageChunk(content='', tool_call_chunks=[{
        'name': name, 'args': json.dumps(args or {}), 'id': identity or name, 'index': 0,
    }])


class Model:
    def __init__(self, turns):
        self.turns = turns
        self.bound = []
        self.histories = []

    def bind_tools(self, tools, **kwargs):
        self.bound.append({tool.name for tool in tools})
        return self

    async def astream(self, history):
        self.histories.append(list(history))
        yield self.turns[min(len(self.histories) - 1, len(self.turns) - 1)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    hardware = HardwareService(tmp_path)
    saved = hardware.create_project(board='arduino-uno')
    identity = saved['id']

    async def seed():
        await hardware.command(identity, 'add_component', {'type': 'led', 'id': 'led1'})
        await hardware.command(identity, 'add_component', {'type': 'resistor', 'id': 'r1'})
        await hardware.command(identity, 'wire_circuit', {'batch': [
            {'from': {'component': 'board', 'pin': '13'}, 'to': {'component': 'r1', 'pin': '1'}},
            {'from': {'component': 'r1', 'pin': '2'}, 'to': {'component': 'led1', 'pin': 'A'}},
            {'from': {'component': 'led1', 'pin': 'C'}, 'to': {'component': 'board', 'pin': 'GND'}},
        ]})
        await hardware.command(identity, 'generate_firmware', {'source': SOURCE})

    asyncio.run(seed())
    calls = []
    original = hardware.command

    async def command(project_id, name, args=None, runtime_token=None):
        calls.append((name, copy.deepcopy(args or {})))
        return await original(project_id, name, args, runtime_token)

    monkeypatch.setattr(hardware, 'command', command)
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    return hardware, identity, calls


def collect(model, identity, text='Change the firmware to blink faster.', **kwargs):
    requirements = kwargs.pop('requirements', {'behavior': 'blink'})

    async def run():
        return [event async for event in agent.stream_agent(
            model, [HumanMessage(content=text)], project_id=identity,
            requirements=requirements, **kwargs,
        )]

    return asyncio.run(run())


def edit_turns(name='edit_firmware', args=None):
    return [call('read_project'), call('read_firmware'),
            call(name, args or {'old': '1000', 'new': '500'}), AIMessageChunk(content='Firmware updated; circuit unchanged.')]


@pytest.mark.parametrize('name,args,text', [
    ('edit_firmware', {'old': '1000', 'new': '500'}, 'Change the firmware to blink faster.'),
    ('edit_firmware', {'source': UPDATED}, 'Update the code timing. Do not replace the board.'),
    ('generate_firmware', {'source': UPDATED}, 'Generate firmware for the existing circuit.'),
    ('generate_firmware', {'source': UPDATED}, 'Create firmware. Do not compile or run simulation.'),
])
def test_targeted_write_preserves_project_without_assessment(project, name, args, text):
    hardware, identity, calls = project
    before = hardware.get_project(identity)
    model = Model(edit_turns(name, args))
    events = collect(model, identity, text)
    after = hardware.get_project(identity)
    assert events[-1]['status'] == 'success'
    assert events[-1]['changes']['firmwareWrites'] == 1
    assert all(value == 0 for key, value in events[-1]['changes'].items() if key != 'firmwareWrites')
    assert after['firmware']['source'] == UPDATED
    assert after['revision'] == before['revision'] + 1
    assert after['firmware']['revision'] == before['firmware']['revision'] + 1
    assert all(after[key] == before[key] for key in ('board', 'components', 'wires'))
    assert [name for name, _ in calls] == ['read_project', 'read_firmware', name, 'read_project']
    assert all(bound <= agent._FIRMWARE_TOOLS for bound in model.bound)
    prompt = '\n'.join(message.content for message in model.histories[0] if message.type == 'system')
    assert 'targeted firmware update' in prompt
    assert 'Actual Velxio starting reference' not in prompt
    assert 'Feasibility approval is required.' not in prompt
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)


@pytest.mark.parametrize('name,args', [
    ('add_component', {'type': 'servo'}),
    ('wire_circuit', {'batch': [{'from': {'component': 'board', 'pin': '13'}, 'to': {'component': 'led1', 'pin': 'A'}}]}),
    ('check_connections', {'repair': True}),
    ('run_simulation', {}),
    ('assess_project_feasibility', {'plan': {'board': 'arduino-uno', 'behavior': 'blink', 'parts': [], 'libraries': [], 'operations': ['generate_firmware']}}),
])
def test_unbound_topology_and_runtime_calls_are_refused(project, name, args):
    hardware, identity, calls = project
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call(name, args), AIMessageChunk(content='Blocked.')])
    events = collect(model, identity)
    assert events[-1]['status'] == 'error'
    assert 'firmware-only' in json.dumps(events)
    assert [name for name, _ in calls] == ['read_project']
    assert hardware.get_project(identity) == before


@pytest.mark.parametrize('text', [
    'Change firmware and add a servo.',
    'Update code and rewire the LED.',
    'Replace the board and generate firmware.',
    'Generate firmware and change the pins.',
])
def test_actual_topology_request_still_requires_assessment(project, text):
    _, identity, calls = project
    model = Model([call('read_project'), call('generate_firmware', {'source': UPDATED}), AIMessageChunk(content='Blocked.')])
    events = collect(model, identity, text)
    assert all(bound == agent._ASSESSMENT_TOOLS for bound in model.bound)
    assert 'obtain approval' in json.dumps(events)
    assert [name for name, _ in calls] == ['read_project']


@pytest.mark.parametrize('board,requirements', [
    ('unselected', {'behavior': 'blink'}),
    ('arduino-uno', {'board': 'esp32-c3', 'behavior': 'blink'}),
])
def test_missing_or_changed_board_still_requires_assessment(project, board, requirements):
    hardware, identity, calls = project
    with hardware.lock:
        saved = hardware._load(identity)
        saved['board'] = board
        hardware._save(saved)
    model = Model([call('read_project'), call('generate_firmware', {'source': UPDATED}), AIMessageChunk(content='Blocked.')])
    events = collect(model, identity, requirements=requirements)
    assert all(bound == agent._ASSESSMENT_TOOLS for bound in model.bound)
    assert 'obtain approval' in json.dumps(events)
    assert [name for name, _ in calls] == ['read_project']


def test_unconfirmed_edit_still_requires_questions(project):
    _, identity, calls = project
    model = Model([call('generate_firmware', {'source': UPDATED}), AIMessageChunk(content='Blocked.')])
    events = collect(model, identity, requirements=None)
    assert all(bound == {'ask_project_questions'} for bound in model.bound)
    assert not calls
    assert events[-1]['status'] == 'error'


def review(hardware, identity, operations):
    record = feasibility.assess(hardware, identity, {
        'board': 'arduino-uno', 'behavior': 'Blink', 'parts': [
            {'type': 'led', 'quantity': 1}, {'type': 'resistor', 'quantity': 1},
        ], 'libraries': [], 'operations': operations,
    })
    with hardware.lock:
        saved = hardware._load(identity)
        pending = saved['_feasibility'][record['id']]
        pending['status'] = 'awaiting_approval'
        pending['choices'] = [{'id': 'hardware_only'}]
        hardware._save(saved)
    return feasibility.authorize(hardware, identity, {'assessment_id': record['id'], 'choice': 'hardware_only'})


def test_supplied_approval_advances_and_does_not_force_old_build_compile(project):
    hardware, identity, _ = project
    record = review(hardware, identity, ['edit_firmware', 'compile_firmware'])
    events = collect(Model(edit_turns()), identity, approval={'id': record['id'], 'choice': 'hardware_only'})
    assert events[-1]['status'] == 'success'
    checked = feasibility.validate_approval(hardware, identity, record['id'])
    assert checked['revision'] == hardware.get_project(identity)['revision']


def test_supplied_approval_restricts_firmware_operations(project):
    hardware, identity, calls = project
    record = review(hardware, identity, ['compile_firmware'])
    events = collect(Model(edit_turns()), identity, approval={'id': record['id'], 'choice': 'hardware_only'})
    assert events[-1]['status'] == 'error'
    assert 'outside the reviewed plan' in json.dumps(events)
    assert not any(name == 'edit_firmware' for name, _ in calls)


def test_stale_supplied_approval_stops_before_model(project):
    hardware, identity, _ = project
    record = review(hardware, identity, ['edit_firmware'])
    asyncio.run(hardware.command(identity, 'edit_firmware', {'source': UPDATED}))
    model = Model(edit_turns())
    events = collect(model, identity, approval={'id': record['id'], 'choice': 'hardware_only'})
    assert events[-1]['reason'] == 'invalid_requirements'
    assert not model.histories


@pytest.mark.parametrize('valid,success,stale', [(False, True, False), (True, False, False), (True, True, False), (True, True, True)])
def test_compile_keeps_circuit_and_current_artifact_guards(project, monkeypatch, valid, success, stale):
    hardware, identity, calls = project
    original = hardware.command

    async def command(project_id, name, args=None, runtime_token=None):
        if name in ('validate_circuit', 'compile_firmware'):
            calls.append((name, args or {}))
            saved = hardware.get_project(project_id)
            if name == 'validate_circuit':
                return {'valid': valid, 'errors': [] if valid else ['Short circuit'], 'revision': saved['revision']}
            return {'success': success, 'project_revision': saved['revision'] - int(stale),
                    'source_revision': saved['firmware']['revision'],
                    'artifact': {'id': 'artifact'} if success else None}
        return await original(project_id, name, args, runtime_token)

    monkeypatch.setattr(hardware, 'command', command)
    turns = edit_turns()[:-1] + [call('compile_firmware'), AIMessageChunk(content='Compiled.')]
    events = collect(Model(turns), identity, 'Edit firmware and compile it.')
    assert events[-1]['status'] == ('success' if valid and success and not stale else 'error')
    assert any(name == 'compile_firmware' for name, _ in calls) is valid
    assert any(name == 'validate_circuit' for name, _ in calls)
    if not valid:
        assert 'proven connection error' in json.dumps(events)
    if not success or stale:
        assert 'no successful artifact' in json.dumps(events)


def test_edit_requires_read_and_revision_match(project):
    hardware, identity, calls = project
    before = hardware.get_project(identity)
    model = Model([call('edit_firmware', {'source': UPDATED}, 'unread'), call('read_project'),
                   call('edit_firmware', {'source': UPDATED, 'expected_revision': 0}, 'stale'), AIMessageChunk(content='Blocked.')])
    events = collect(model, identity)
    assert events[-1]['status'] == 'error'
    assert 'read_project successfully' in json.dumps(events)
    assert hardware.get_project(identity) == before


@pytest.mark.parametrize('text', [
    'Explain how to edit firmware.', 'Do not edit firmware.',
    'Build a circuit and generate firmware.',
])
def test_firmware_intent_is_not_a_general_build_bypass(text):
    assert not agent._firmware_intent(text)
