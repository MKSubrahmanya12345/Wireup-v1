import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from backend import agent, feasibility
from backend.hardware import HardwareService


def call(name, args=None, identity=None):
    return AIMessageChunk(content='', tool_call_chunks=[{
        'name': name, 'args': json.dumps(args or {}), 'id': identity or name, 'index': 0,
    }])


class Model:
    def __init__(self, turns):
        self.turns = turns
        self.bound = []
        self.histories = []
        self.choices = []

    def bind_tools(self, tools, **kwargs):
        self.bound.append({tool.name for tool in tools})
        self.choices.append(kwargs.get('tool_choice'))
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
            {'id': 'w1', 'from': {'component': 'board', 'pin': '13'}, 'to': {'component': 'r1', 'pin': '1'}},
            {'id': 'w2', 'from': {'component': 'r1', 'pin': '2'}, 'to': {'component': 'led1', 'pin': 'A'}},
            {'id': 'w3', 'from': {'component': 'led1', 'pin': 'C'}, 'to': {'component': 'board', 'pin': 'GND'}},
        ]})

    asyncio.run(seed())
    calls = []
    original = hardware.command

    async def command(project_id, name, args=None, runtime_token=None):
        calls.append((name, copy.deepcopy(args or {})))
        return await original(project_id, name, args, runtime_token)

    monkeypatch.setattr(hardware, 'command', command)
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    return hardware, identity, saved['runtime_token'], calls


def collect(project, model, text, **kwargs):
    _, identity, token, _ = project
    token = kwargs.pop('runtime_token', token)

    async def run():
        return [event async for event in agent.stream_agent(
            model, [HumanMessage(content=text)], project_id=identity, runtime_token=token, **kwargs,
        )]

    return asyncio.run(run())


def questions():
    return {'summary': 'Choose the initial behavior.', 'questions': [
        {'id': 'behavior', 'question': 'How should the LED behave?', 'options': [
            {'id': 'blink', 'label': 'Blink'}, {'id': 'steady', 'label': 'Stay on'},
        ]},
    ]}


@pytest.mark.parametrize('requirements', [None, {'behavior': 'blink'}])
@pytest.mark.parametrize('text', ['Remove component led1.', 'Delete the LED.', 'Remove led1 from the circuit.'])
def test_removal_reads_edits_and_finishes_without_setup(project, requirements, text):
    hardware, identity, _, calls = project
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call('remove_component', {'id': 'led1'}),
                   AIMessageChunk(content='Removed led1 and its incident wires. Firmware unchanged; not compiled or run.')])
    events = collect(project, model, text, requirements=requirements)
    after = hardware.get_project(identity)
    assert events[-1]['status'] == 'success'
    assert events[-1]['changes']['componentsRemoved'] == 1
    assert after['revision'] == before['revision'] + 1
    assert after['components'] == [part for part in before['components'] if part['id'] != 'led1']
    assert after['wires'] == [wire for wire in before['wires'] if wire['id'] == 'w1']
    assert after['board'] == before['board']
    assert after['firmware'] == before['firmware']
    assert [name for name, _ in calls] == ['read_project', 'remove_component', 'read_project']
    assert calls[1][1]['expected_revision'] == before['revision']
    assert all(bound == {'read_project', 'remove_component'} for bound in model.bound)
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)
    prompt = '\n'.join(message.content for message in model.histories[0] if message.type == 'system')
    assert 'targeted edit' in prompt
    assert 'Actual Velxio starting reference' not in prompt
    assert 'Feasibility approval is required.' not in prompt


@pytest.mark.parametrize('text,name,args', [
    ('Delete wire w2.', 'remove_wire', {'id': 'w2'}),
    ('Move led1 to x 600 y 200.', 'modify_component', {'id': 'led1', 'x': 600.0, 'y': 200.0}),
    ('Rotate led1 to 90 degrees.', 'modify_component', {'id': 'led1', 'rotation': 90.0}),
    ('Change led1 color to blue.', 'modify_component', {'id': 'led1', 'properties': {'color': 'blue'}}),
])
def test_other_targeted_operations_preserve_unrelated_state(project, text, name, args):
    hardware, identity, _, calls = project
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call(name, args), AIMessageChunk(content='Requested edit complete.')])
    events = collect(project, model, text)
    after = hardware.get_project(identity)
    assert events[-1]['status'] == 'success'
    assert all(bound == {'read_project', name} for bound in model.bound)
    assert after['revision'] == before['revision'] + 1
    assert after['firmware'] == before['firmware']
    assert after['board'] == before['board']
    if name == 'remove_wire':
        assert after['components'] == before['components']
        assert after['wires'] == [wire for wire in before['wires'] if wire['id'] != 'w2']
    else:
        assert after['wires'] == before['wires']
        component = next(part for part in after['components'] if part['id'] == 'led1')
        for key, value in args.items():
            if key == 'properties':
                assert all(component[key][prop] == expected for prop, expected in value.items())
            else:
                assert component[key] == value
    assert [operation for operation, _ in calls] == ['read_project', name, 'read_project']


@pytest.mark.parametrize('name,args', [
    ('ask_project_questions', questions()),
    ('assess_project_feasibility', {'plan': {'board': 'arduino-uno', 'behavior': 'blink',
                                          'parts': [], 'libraries': [], 'operations': ['remove_component']}}),
    ('add_component', {'type': 'servo'}),
    ('add_component', {'type': 'esp32-c3'}),
    ('remove_wire', {'id': 'w1'}),
    ('modify_component', {'id': 'r1', 'x': 400.0}),
    ('generate_firmware', {'source': 'void setup() {} void loop() {}'}),
    ('check_connections', {'repair': True}),
    ('run_simulation', {}),
    ('remove_component', {'id': 'r1'}),
])
def test_removal_rejects_unrelated_or_unbound_calls(project, name, args):
    hardware, identity, _, calls = project
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call(name, args), AIMessageChunk(content='Blocked.')])
    events = collect(project, model, 'Remove led1.', requirements={'behavior': 'blink'})
    assert events[-1]['status'] == 'error'
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)
    assert hardware.get_project(identity) == before
    assert [operation for operation, _ in calls] == ['read_project']


@pytest.mark.parametrize('text,args', [
    ('Move led1 to x 200.', {'id': 'led1', 'properties': {'color': 'blue'}}),
    ('Change led1 color to blue.', {'id': 'led1', 'x': 200.0}),
    ('Change led1 color to blue.', {'id': 'led1', 'properties': {'unknown': 'blue'}}),
    ('Change led1 color to blue.', {'id': 'led1', 'properties': {}}),
    ('Move led1 to x 200.', {'id': 'led1'}),
])
def test_property_and_position_scopes_and_service_validation(project, text, args):
    hardware, identity, _, _ = project
    before = hardware.get_project(identity)
    events = collect(project, Model([call('read_project'), call('modify_component', args),
                                    AIMessageChunk(content='Blocked.')]), text)
    assert events[-1]['status'] == 'error'
    assert hardware.get_project(identity) == before


@pytest.mark.parametrize('token', [None, 'wrong-token'])
def test_targeted_mutation_requires_runtime_authorization(project, token):
    hardware, identity, _, calls = project
    before = hardware.get_project(identity)
    events = collect(project, Model([call('read_project'), call('remove_component', {'id': 'led1'}),
                                    AIMessageChunk(content='Blocked.')]), 'Remove led1.', runtime_token=token)
    assert events[-1]['status'] == 'error'
    assert 'matching project runtime token' in json.dumps(events)
    assert hardware.get_project(identity) == before
    assert [name for name, _ in calls] == ['read_project']


@pytest.mark.parametrize('read_first,revision', [(False, None), (True, 0)])
def test_targeted_mutation_requires_read_and_current_revision(project, read_first, revision):
    hardware, identity, _, _ = project
    before = hardware.get_project(identity)
    args = {'id': 'led1'}
    if revision is not None:
        args['expected_revision'] = revision
    turns = ([call('read_project')] if read_first else []) + [call('remove_component', args), AIMessageChunk(content='Blocked.')]
    events = collect(project, Model(turns), 'Remove led1.')
    assert events[-1]['status'] == 'error'
    assert hardware.get_project(identity) == before
    assert ('revision conflict' if read_first else 'read_project successfully') in json.dumps(events)


def test_ambiguous_removal_asks_normal_text_without_form(project):
    hardware, identity, _, calls = project
    asyncio.run(hardware.command(identity, 'add_component', {'type': 'led', 'id': 'led2'}))
    calls.clear()
    before = hardware.get_project(identity)
    model = Model([call('read_project'), AIMessageChunk(content='Which LED should I remove: led1 or led2?')])
    events = collect(project, model, 'Remove the LED.')
    assert events[-1]['status'] == 'success'
    assert model.choices == ['required', 'auto']
    assert events[-1]['changes']['componentsRemoved'] == 0
    assert any(event.get('channel') == 'answer' and 'Which LED' in event['text'] for event in events)
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)
    assert hardware.get_project(identity) == before
    assert [name for name, _ in calls] == ['read_project']
    model = Model([call('read_project'), call('remove_component', {'id': 'led1'}), AIMessageChunk(content='Blocked.')])
    assert collect(project, model, 'Remove the LED.')[-1]['status'] == 'error'
    assert hardware.get_project(identity) == before


@pytest.mark.parametrize('requirements', [None, {'behavior': 'blink'}])
def test_read_followup_cannot_open_setup_or_modify(project, requirements):
    hardware, identity, _, calls = project
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call('ask_project_questions', questions()),
                   call('remove_component', {'id': 'led1'}), AIMessageChunk(content='The project contains an LED and resistor.')])
    events = collect(project, model, 'Show the current project components.', requirements=requirements)
    assert all(bound == agent._FOLLOWUP_READ_TOOLS for bound in model.bound)
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)
    assert hardware.get_project(identity) == before
    assert [name for name, _ in calls] == ['read_project']


@pytest.mark.parametrize('text', [
    'Remove led1 and add a servo.', 'Remove led1 and replace the board.',
    'Remove led1 and rewire the circuit.', 'Remove led1 then compile firmware.',
    'Do not remove led1.', 'Explain how to remove led1.', 'Remove the board.',
    'Change the board to ESP32-C3.', 'Build a new circuit.',
    'Remove led1; move r1.', 'Remove led1 but keep r1.',
    'Change led1 to a resistor.', 'Update the project requirements.',
])
def test_targeted_intent_is_not_a_topology_or_negation_bypass(text):
    assert agent._targeted_edit_intent(text) is None


@pytest.mark.parametrize('text,requirements', [
    ('Remove led1 and add a servo.', {'behavior': 'blink'}),
    ('Remove led1 and replace the board.', {'board': 'esp32-c3'}),
    ('Remove led1.', {'board': 'esp32-c3'}),
])
def test_topology_and_changed_board_keep_assessment_gate(project, text, requirements):
    hardware, identity, _, calls = project
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call('remove_component', {'id': 'led1'}), AIMessageChunk(content='Blocked.')])
    events = collect(project, model, text, requirements=requirements)
    expected = {'ask_project_questions'} if agent._major_design_change(text) else agent._ASSESSMENT_TOOLS
    assert all(bound == expected for bound in model.bound)
    assert ('ask_project_questions first' if agent._major_design_change(text) else 'obtain approval') in json.dumps(events)
    assert hardware.get_project(identity) == before
    assert [name for name, _ in calls] == ([] if agent._major_design_change(text) else ['read_project'])


def test_initial_build_still_questions_despite_ui_flag(project):
    hardware, _, _, calls = project
    initial = hardware.create_project(board='arduino-uno')
    with hardware.lock:
        saved = hardware._load(initial['id'])
        saved['requirementsConfirmed'] = True
        hardware._save(saved)
    fresh = (hardware, initial['id'], initial['runtime_token'], calls)
    model = Model([call('ask_project_questions', questions())])
    events = collect(fresh, model, 'Build an LED circuit.')
    assert model.bound == [{'ask_project_questions'}]
    assert events[-1]['status'] == 'awaiting_answers'
    assert any(event['type'] == 'questions' for event in events)
    assert not calls
    model = Model([call('read_project'), AIMessageChunk(content='There is no LED to remove. Which component did you mean?')])
    events = collect(fresh, model, 'Remove the LED.')
    assert model.bound == [{'read_project'}, {'read_project'}]
    assert not any(event['type'] == 'questions' for event in events)


def test_saved_answers_supply_context_without_nonboard_parts(project):
    hardware, _, _, calls = project
    initial = hardware.create_project(board='arduino-uno')
    fresh = (hardware, initial['id'], initial['runtime_token'], calls)
    model = Model([call('read_project'), AIMessageChunk(content='The requested LED is not present.')])
    events = collect(fresh, model, 'Remove led1.', requirements={'behavior': 'blink'})
    assert all(bound == {'read_project', 'remove_component'} for bound in model.bound)
    assert events[-1]['status'] == 'success'
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)


@pytest.mark.parametrize('operation', ['remove_component', 'modify_component'])
def test_supplied_approval_still_restricts_targeted_edits(project, operation):
    hardware, identity, _, calls = project
    record = feasibility.assess(hardware, identity, {
        'board': 'arduino-uno', 'behavior': 'blink',
        'parts': [{'type': 'led'}, {'type': 'resistor'}],
        'libraries': [], 'operations': [operation],
    })
    with hardware.lock:
        stored = hardware._load(identity)
        stored['_feasibility'][record['id']]['status'] = 'awaiting_approval'
        stored['_feasibility'][record['id']]['choices'] = [{'id': 'hardware_only'}]
        hardware._save(stored)
    before = hardware.get_project(identity)
    model = Model([call('read_project'), call('remove_component', {'id': 'led1'}),
                   AIMessageChunk(content='Requested edit result reported.')])
    events = collect(project, model, 'Remove led1.', requirements={'behavior': 'blink'},
                     approval={'id': record['id'], 'choice': 'hardware_only'})
    if operation == 'remove_component':
        assert events[-1]['status'] == 'success', events
        checked = feasibility.validate_approval(hardware, identity, record['id'])
        assert checked['revision'] == hardware.get_project(identity)['revision']
    else:
        assert events[-1]['status'] == 'error'
        assert hardware.get_project(identity) == before
        assert [name for name, _ in calls] == ['read_project']


def test_stale_supplied_approval_blocks_before_model(project):
    hardware, identity, _, _ = project
    record = feasibility.assess(hardware, identity, {
        'board': 'arduino-uno', 'behavior': 'blink',
        'parts': [{'type': 'led'}, {'type': 'resistor'}],
        'libraries': [], 'operations': ['remove_component'],
    })
    asyncio.run(hardware.command(identity, 'modify_component', {'id': 'led1', 'x': 500.0}))
    model = Model([call('read_project')])
    events = collect(project, model, 'Remove led1.', requirements={'behavior': 'blink'},
                     approval={'id': record['id'], 'choice': 'hardware_only'})
    assert events[-1]['reason'] == 'invalid_requirements'
    assert not model.histories


def test_explicit_design_revision_still_questions(project):
    hardware, identity, _, calls = project
    record = feasibility.assess(hardware, identity, {
        'board': 'arduino-uno', 'behavior': 'blink', 'parts': [{'type': 'nonexistent-sensor'}],
        'libraries': [], 'operations': ['add_component'],
    })
    model = Model([call('ask_project_questions', questions())])
    events = collect(project, model, 'Remove led1.', requirements={'behavior': 'blink'},
                     approval={'id': record['id'], 'choice': 'revise'})
    assert model.bound == [{'ask_project_questions'}]
    assert events[-1]['status'] == 'awaiting_answers'
    assert any(event['type'] == 'questions' for event in events)
    assert not calls
