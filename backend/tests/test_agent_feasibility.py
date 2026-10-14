import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from backend import agent


OPERATIONS = ['add_component', 'wire_circuit', 'generate_firmware', 'edit_firmware', 'compile_firmware', 'run_simulation']
PLAN = {'board': 'arduino-uno', 'behavior': 'Blink an LED', 'parts': [
    {'type': 'led', 'quantity': 1, 'purpose': 'Indicator'},
    {'type': 'resistor', 'quantity': 1, 'purpose': 'Limit LED current'},
], 'libraries': [], 'operations': OPERATIONS}


def call(name, args=None, identity='call'):
    return AIMessageChunk(content='', tool_call_chunks=[{
        'name': name, 'args': json.dumps(args or {}), 'id': identity, 'index': 0,
    }])


class Model:
    def __init__(self, turns):
        self.turns = turns
        self.bound = []
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        self.bound.append({tool.name for tool in tools})
        return self

    async def astream(self, history):
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        yield turn


class Hardware:
    def __init__(self):
        self.revision = 1
        self.board = 'arduino-uno'
        self.calls = []
        self.fail_compile = False
        self.valid = True
        self.components = []

    def get_project(self, project_id):
        return {'id': project_id, 'revision': self.revision, 'board': self.board, 'components': self.components, 'wires': [], 'firmware': {'revision': self.revision}}

    async def command(self, project_id, name, args, runtime_token=None):
        self.calls.append((name, args))
        if name in agent._MUTATIONS:
            self.revision += 1
        if name == 'add_component':
            self.components.append({'id': str(len(self.components)), 'type': args['type']})
        if name == 'validate_circuit':
            return {'valid': self.valid, 'errors': [] if self.valid else ['Short circuit'], 'revision': self.revision}
        if name == 'compile_firmware':
            return {'success': not self.fail_compile, 'error': 'Missing library' if self.fail_compile else None,
                    'project_revision': self.revision, 'source_revision': self.revision,
                    'artifact': None if self.fail_compile else {'id': 'artifact'}}
        return self.get_project(project_id)


class Feasibility:
    def __init__(self):
        self.status = 'approved'
        self.record = None
        self.checks = []
        self.authorizations = []
        self.deny_wire = False

    def assess(self, hardware, project_id, plan):
        self.record = {'id': 'review', 'project_id': project_id, 'revision': hardware.revision, 'status': self.status,
                       'plan': copy.deepcopy(plan), 'issues': [] if self.status == 'approved' else [{'code': 'runtime', 'message': 'Not simulated'}],
                       'choices': [{'id': 'hardware_only'}, {'id': 'cancel'}, {'id': 'revise'}]}
        return copy.deepcopy(self.record)

    def authorize(self, hardware, project_id, approval):
        self.authorizations.append(approval)
        assert approval['assessment_id'] == self.record['id']
        if self.record['revision'] != hardware.revision:
            raise ValueError('Review is stale.')
        self.record['status'] = 'approved' if approval['choice'] == 'hardware_only' else approval['choice']
        return copy.deepcopy(self.record)

    def validate_approval(self, hardware, project_id, identity):
        if not self.record or identity != self.record['id'] or self.record['status'] != 'approved' or self.record['revision'] != hardware.revision:
            raise ValueError('Review is stale or unapproved.')
        return copy.deepcopy(self.record)

    def check_operation(self, hardware, project_id, identity, name, args):
        self.validate_approval(hardware, project_id, identity)
        self.checks.append((name, args))
        allowed = {part['type'] for part in self.record['plan']['parts']} | {self.record['plan']['board']}
        if name == 'add_component' and args['type'].removeprefix('wokwi-') not in allowed:
            raise ValueError('Part outside reviewed plan.')
        if name == 'wire_circuit' and self.deny_wire:
            raise ValueError('Unknown pins cannot be wired through approval.')

    def advance(self, hardware, project_id, identity, previous, revision):
        assert previous == self.record['revision']
        assert revision == hardware.revision
        self.record['revision'] = revision


@pytest.fixture
def services(monkeypatch):
    hardware, feasibility = Hardware(), Feasibility()
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    monkeypatch.setattr(agent, '_get_feasibility', lambda: feasibility)
    return hardware, feasibility


def collect(model, **kwargs):
    async def run():
        return [event async for event in agent.stream_agent(model, [HumanMessage(content='Build this circuit')],
                project_id='project', requirements={'behavior': 'blink'}, **kwargs)]
    return asyncio.run(run())


def approved_turns(plan=None):
    return [call('read_project', identity='read'), call('assess_project_feasibility', {'plan': plan or PLAN}, 'assess')]


def test_gate_rejects_unbound_mutation_and_no_automatic_board(services):
    hardware, _ = services
    hardware.board = 'unselected'
    model = Model([call('read_project', identity='read'), call('add_component', {'type': 'arduino-uno'}, 'add'), AIMessageChunk(content='Blocked.')])
    events = asyncio.run(_board_events(model))
    assert [name for name, _ in hardware.calls] == ['read_project']
    assert all(bound == agent._ASSESSMENT_TOOLS for bound in model.bound)
    assert 'obtain approval' in json.dumps(events)


async def _board_events(model):
    return [event async for event in agent.stream_agent(model, [HumanMessage(content='Build LED circuit')], project_id='project', requirements={'board': 'arduino-uno'})]


def test_documented_limitations_do_not_require_a_second_confirmation(services):
    hardware, feasibility = services
    feasibility.status = 'awaiting_approval'
    placement_plan = {**PLAN, 'operations': ['add_component']}
    assessment = call('assess_project_feasibility', {'plan': placement_plan}, 'assess')
    assessment.tool_call_chunks.append({'name': 'add_component', 'args': json.dumps({'type': 'led'}), 'id': 'add', 'index': 1})
    assessment = AIMessageChunk(content='', tool_call_chunks=assessment.tool_call_chunks)
    model = Model([call('read_project', identity='read'), assessment, AIMessageChunk(content='The approved component was placed.')])
    events = collect(model)
    assert events[-1]['status'] == 'success'
    assert all(event.get('status') != 'awaiting_approval' for event in events if event['type'] == 'feasibility')
    assert 'add_component' in [name for name, _ in hardware.calls]
    assert events[-1]['changes']['componentsAdded'] == 1


def test_safe_actual_plan_autoapproves_and_verifies_final_readback(services):
    hardware, feasibility = services
    plan = copy.deepcopy(PLAN)
    plan['operations'] = ['add_component']
    model = Model([*approved_turns(plan), call('add_component', {'type': 'wokwi-led'}, 'add'), AIMessageChunk(content='Added the LED; no compilation requested.')])
    events = collect(model)
    assert events[-1]['status'] == 'success'
    assert {key: value for key, value in feasibility.record['plan'].items() if key != 'requirements_text'} == plan
    assert [name for name, _ in hardware.calls] == ['read_project', 'add_component', 'read_project', 'validate_circuit']
    assert feasibility.checks[0][0] == 'add_component'
    assert events[-1]['changes']['componentsAdded'] == 1


@pytest.mark.parametrize('choice,status', [('cancel', 'cancelled')])
def test_consent_cancel_or_revise_never_calls_model(services, choice, status):
    hardware, feasibility = services
    feasibility.status = 'awaiting_approval'
    feasibility.assess(hardware, 'project', PLAN)
    model = Model([AIMessageChunk(content='Must not run')])
    events = collect(model, approval={'id': 'review', 'choice': choice})
    assert events[-1]['status'] == status
    assert model.calls == 0
    assert not hardware.calls


def test_change_design_asks_revised_questions_instead_of_ending(services):
    hardware, feasibility = services
    feasibility.status = 'awaiting_approval'
    feasibility.assess(hardware, 'project', PLAN)
    question = {'summary':'Choose a supported output.', 'questions':[{'id':'output','question':'Use serial output instead of the unavailable display?','options':[{'id':'serial','label':'Serial monitor'},{'id':'ai_choose','label':'Let AI choose'}]}]}
    model = Model([call('ask_project_questions',question,'revise-questions')])
    events = collect(model, approval={'id':'review','choice':'revise'})
    assert model.bound == [{'ask_project_questions'}]
    assert any(event['type'] == 'questions' for event in events)
    assert events[-1]['status'] == 'awaiting_answers'
    assert not hardware.calls


def test_request_consent_authorizes_then_checks_every_operation(services):
    hardware, feasibility = services
    feasibility.status = 'awaiting_approval'
    feasibility.assess(hardware, 'project', PLAN)
    model = Model([call('read_project', identity='read'), call('generate_firmware', {'source': 'void setup(){} void loop(){}'}, 'write'),
                   call('compile_firmware', identity='compile'), AIMessageChunk(content='Compiled current firmware, not run.')])
    events = collect(model, approval={'assessment_id': 'review', 'choice': 'hardware_only'})
    assert events[-1]['status'] == 'success'
    assert [name for name, _ in feasibility.checks] == ['generate_firmware', 'compile_firmware']
    assert feasibility.authorizations == [{'assessment_id': 'review', 'choice': 'hardware_only'}]
    assert all('assess_project_feasibility' not in tools and 'ask_project_questions' not in tools for tools in model.bound)
    assert feasibility.record['revision'] == hardware.revision


def test_unreviewed_parts_blocked_after_approval(services):
    hardware, _ = services
    model = Model([*approved_turns(), call('add_component', {'type': 'servo'}, 'new'), AIMessageChunk(content='Cannot substitute.')])
    events = collect(model)
    assert 'outside reviewed plan' in json.dumps(events)
    assert not any(name == 'add_component' for name, _ in hardware.calls)


def test_unknown_pin_check_and_from_alias_preserved(services):
    hardware, feasibility = services
    feasibility.deny_wire = True
    batch = [{'from': {'component': 'led1', 'pin': 'unknown'}, 'to': {'component': 'uno', 'pin': '13'}}]
    model = Model([*approved_turns(), call('wire_circuit', {'batch': batch}, 'wire'), AIMessageChunk(content='Cannot wire unknown pins.')])
    events = collect(model)
    assert 'Unknown pins' in json.dumps(events)
    assert feasibility.checks[-1][1]['batch'] == batch
    assert not any(name == 'wire_circuit' for name, _ in hardware.calls)


def test_repeated_identical_failures_stop_at_three(services):
    hardware, _ = services
    model = Model([*approved_turns(), *[call('add_component', {'type': 'servo'}, str(index)) for index in range(6)]])
    events = collect(model)
    assert events[-1]['reason'] == 'limit'
    assert 'Three identical failed' in json.dumps(events)
    assert model.calls == 5
    assert not any(name == 'add_component' for name, _ in hardware.calls)


def test_compile_allows_at_most_two_corrections(services):
    hardware, _ = services
    hardware.fail_compile = True
    turns = approved_turns()
    for index in range(4):
        turns.extend([call('edit_firmware', {'source': 'void setup(){} void loop(){' + str(index) + ';}'}, 'edit' + str(index)),
                      call('compile_firmware', identity='compile' + str(index))])
    model = Model(turns)
    events = collect(model)
    assert events[-1]['reason'] == 'limit'
    assert len([name for name, _ in hardware.calls if name == 'compile_firmware']) == 3


def test_failed_compile_cannot_claim_success(services):
    hardware, _ = services
    hardware.fail_compile = True
    model = Model([*approved_turns(), call('generate_firmware', {'source': 'void setup(){} void loop(){}'}, 'write'),
                   call('compile_firmware', identity='compile'), AIMessageChunk(content='Compiled successfully.')])
    events = collect(model)
    assert events[-1]['status'] == 'error'
    assert not any(event.get('channel') == 'answer' for event in events)
    assert 'no successful artifact' in json.dumps(events)


def test_mutation_invalidates_prior_compile(services):
    hardware, _ = services
    model = Model([*approved_turns(), call('compile_firmware', identity='compile'), call('add_component', {'type': 'led'}, 'add'),
                   AIMessageChunk(content='Everything compiled.')])
    events = collect(model)
    assert events[-1]['status'] == 'error'
    assert 'current project' in json.dumps(events)


def test_final_invalid_circuit_is_not_success(services):
    hardware, _ = services
    hardware.valid = False
    plan = copy.deepcopy(PLAN)
    plan['operations'] = ['add_component']
    model = Model([*approved_turns(plan), call('add_component', {'type': 'led'}, 'add'), AIMessageChunk(content='Ready.')])
    events = collect(model)
    assert events[-1]['status'] == 'error'
    assert 'Short circuit' in json.dumps(events)
    starts = [event for event in events if event['type'] == 'step_start']
    ends = [event for event in events if event['type'] == 'step_end']
    assert [event['id'] for event in starts] == [event['id'] for event in ends]
    assert starts[-1]['toolName'] == 'validate_circuit'
    assert starts[-1]['args'] == {}
    assert ends[-1]['status'] == 'error'
    assert ends[-1]['result'] == json.loads(ends[-1]['output']) == {
        'valid': False, 'errors': ['Short circuit'], 'revision': hardware.revision,
    }
    assert not any(event.get('channel') == 'answer' for event in events)


def test_quantity_cannot_exceed_reviewed_plan(services):
    hardware, _ = services
    plan = copy.deepcopy(PLAN)
    plan['operations'] = ['add_component']
    model = Model([*approved_turns(plan), call('add_component', {'type': 'led'}, 'first'),
                   call('add_component', {'type': 'led'}, 'second'), AIMessageChunk(content='Added only the approved quantity.')])
    events = collect(model)
    assert len([name for name, _ in hardware.calls if name == 'add_component']) == 1
    assert 'exceeds the reviewed part quantity' in json.dumps(events)


def test_actual_unreviewed_parts_block_compile(services):
    hardware, _ = services
    hardware.components = [{'id': 'unexpected', 'type': 'servo'}]
    model = Model([*approved_turns(), call('compile_firmware', identity='compile'), AIMessageChunk(content='Blocked.')])
    events = collect(model)
    assert not any(name == 'compile_firmware' for name, _ in hardware.calls)
    assert 'actual project contains parts outside' in json.dumps(events)
    assert events[-1]['status'] == 'error'


def test_initial_questions_only_and_no_changes(services):
    hardware, _ = services
    questions = {'summary': 'Choose LED behavior', 'questions': [{'id': 'behavior', 'question': 'How should it behave?',
                 'options': [{'id': 'blink', 'label': 'Blink'}, {'id': 'steady', 'label': 'Stay on'}]}]}
    model = Model([call('ask_project_questions', questions)])
    async def run():
        return [event async for event in agent.stream_agent(model, [HumanMessage(content='Build an LED circuit')], project_id='project')]
    events = asyncio.run(run())
    assert events[-1]['status'] == 'awaiting_answers'
    assert model.bound == [{'ask_project_questions'}]
    assert not hardware.calls


def test_real_service_safe_plan_gate_and_revision_advance(tmp_path, monkeypatch):
    from backend.hardware import HardwareService
    hardware = HardwareService(data_dir=tmp_path)
    project = hardware.create_project(board='arduino-uno')
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    plan = copy.deepcopy(PLAN)
    plan['operations'] = ['add_component']
    model = Model([*approved_turns(plan), call('add_component', {'type': 'led'}, 'add'), AIMessageChunk(content='Added LED; not wired or compiled.')])
    async def run():
        return [event async for event in agent.stream_agent(model, [HumanMessage(content='Build LED circuit')],
                project_id=project['id'], requirements={'behavior': 'blink'})]
    events = asyncio.run(run())
    assert events[-1]['status'] == 'success'
    assert events[-1]['changes']['componentsAdded'] == 1
    assert any(event['type'] == 'feasibility' and event['status'] == 'approved' for event in events)
    assert hardware.get_project(project['id'])['revision'] == project['revision'] + 1


def test_stale_consent_fails_before_model_or_mutation(services):
    hardware, feasibility = services
    feasibility.status = 'awaiting_approval'
    feasibility.assess(hardware, 'project', PLAN)
    hardware.revision += 1
    model = Model([AIMessageChunk(content='Must not execute')])
    events = collect(model, approval={'id': 'review', 'choice': 'hardware_only'})
    assert events[-1]['reason'] == 'invalid_requirements'
    assert model.calls == 0
    assert not hardware.calls
