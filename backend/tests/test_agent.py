import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_groq import ChatGroq

from backend import agent


def tool(name, args, call_id='call-1', prose=''):
    return AIMessageChunk(content=prose, tool_call_chunks=[{
        'name': name, 'args': json.dumps(args), 'id': call_id, 'index': 0,
    }])


class FakeModel:
    def __init__(self, turns):
        self.turns = turns
        self.histories = []
        self.tools = []
        self.closed = 0
        self.tool_choices = []
        self.tool_sets = []

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        self.tool_sets.append({item.name for item in tools})
        self.tool_choice = kwargs.get('tool_choice')
        self.tool_choices.append(self.tool_choice)
        return self

    async def astream(self, history):
        self.histories.append(list(history))
        index = len(self.histories) - 1
        turn = self.turns[min(index, len(self.turns) - 1)]
        try:
            for chunk in turn:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk
        finally:
            self.closed += 1


def assessment(board='arduino-uno', parts=('led',), operations=('add_component',)):
    return tool('assess_project_feasibility', {'plan': {
        'board': board, 'behavior': 'Perform the requested project operation.',
        'parts': [{'type': kind, 'quantity': 1, 'purpose': 'Requested component'} for kind in parts],
        'libraries': [], 'operations': list(operations),
    }}, 'assess')


class FakeFeasibility:
    def __init__(self):
        self.record = None
        self.checks = []

    def assess(self, hardware, project_id, plan):
        self.record = {'id': 'review', 'project_id': project_id, 'revision': hardware.revision,
                       'status': 'approved', 'plan': copy.deepcopy(plan), 'issues': []}
        return copy.deepcopy(self.record)

    def validate_approval(self, hardware, project_id, identity):
        if (not self.record or identity != self.record['id'] or project_id != self.record['project_id']
                or self.record['status'] != 'approved' or self.record['revision'] != hardware.revision):
            raise ValueError('Review is stale or unapproved.')
        return copy.deepcopy(self.record)

    def check_operation(self, hardware, project_id, identity, name, args):
        record = self.validate_approval(hardware, project_id, identity)
        if name not in record['plan']['operations']:
            raise ValueError('Operation outside reviewed plan.')
        allowed = {part['type'] for part in record['plan']['parts']} | {record['plan']['board']}
        if name == 'add_component' and args['type'].removeprefix('wokwi-') not in allowed:
            raise ValueError('Part outside reviewed plan.')
        self.checks.append((name, args))

    def advance(self, hardware, project_id, identity, previous, revision):
        assert identity == self.record['id']
        assert project_id == self.record['project_id']
        assert previous == self.record['revision']
        assert revision == hardware.revision
        self.record['revision'] = revision


class FakeService:
    def __init__(self):
        self.calls = []
        self.result = {'success': True}
        self.board = 'arduino-uno'
        self.revision = 4
        self.components = []
        self.source_revision = 4
        self.feasibility = FakeFeasibility()

    def get_project(self, project_id):
        return {'id': project_id, 'board': self.board, 'revision': self.revision,
                'components': copy.deepcopy(self.components), 'firmware': {'revision': self.source_revision}}

    async def command(self, project_id, name, args, runtime_token=None):
        self.calls.append((project_id, name, args, runtime_token))
        if name == 'read_project':
            return {**self.get_project(project_id), 'runtime_token': runtime_token}
        if name == 'calculator':
            return {'expression': args['expression'], 'result': 4}
        if name == 'validate_circuit':
            return {'valid': True, 'errors': [], 'revision': self.revision}
        if self.result.get('status') == 'error':
            return self.result
        if name in agent._MUTATIONS:
            assert args['expected_revision'] == self.revision
            self.revision += 1
            if name == 'add_component':
                kind = args['type'].removeprefix('wokwi-')
                self.components.append({'id': str(len(self.components)), 'type': kind})
                if kind in agent._board_catalog():
                    self.board = kind
            if name in ('generate_firmware', 'edit_firmware'):
                self.source_revision = self.revision
            return self.get_project(project_id)
        if name == 'compile_firmware':
            return {'success': True, 'project_revision': self.revision, 'source_revision': self.source_revision,
                    'artifact': {'id': 'compiled-artifact'}}
        return self.result


def collect(model, history=None, **kwargs):
    async def run():
        return [item async for item in agent.stream_agent(model, history or [HumanMessage(content='Hi')], **kwargs)]
    return asyncio.run(run())


@pytest.fixture
def service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(agent, '_get_service', lambda: service)
    monkeypatch.setattr(agent, '_get_feasibility', lambda: service.feasibility)

    async def calculator(expression):
        return await service.command(None, 'calculator', {'expression': expression})

    monkeypatch.setattr(agent, '_calculator', calculator)
    return service


def questionnaire():
    return {
        'summary': 'Choose the remaining LED behavior settings.',
        'questions': [
            {'id': 'behavior', 'question': 'How should the LED behave?', 'options': [
                {'id': 'blink', 'label': 'Blink'}, {'id': 'steady', 'label': 'Stay on'},
            ]},
            {'id': 'timing', 'question': 'Which blink interval?', 'options': [
                {'id': 'slow', 'label': 'One second'}, {'id': 'fast', 'label': 'Half a second'},
            ]},
            {'id': 'runtime', 'question': 'What should run after building?', 'options': [
                {'id': 'compile', 'label': 'Compile only'}, {'id': 'simulate', 'label': 'Start simulation'},
            ]},
        ],
    }


def test_greeting_has_no_persistent_model_row(service):
    model = FakeModel([[AIMessageChunk(content='Hello '), AIMessageChunk(content='there')]])
    events = collect(model)
    assert [event['type'] for event in events] == ['text', 'text', 'done']
    assert events[-1]['status'] == 'success'
    assert events[-1]['modelCalls'] == 1
    assert service.calls == []
    assert {item.name for item in model.tools} == set(agent.TOOL_NAMES)
    assert len(model.tools) == len(agent.TOOL_NAMES)
    assert all('project_id' not in item.args and 'runtime_token' not in item.args for item in model.tools)
    assert model.histories[0][0].type == 'system'
    assert all(json.dumps(event) for event in events)


def test_calculator_feedback_and_selection_prose_suppressed(service):
    model = FakeModel([
        [AIMessageChunk(content='', additional_kwargs={'reasoning_content': 'Provider reasoning'}),
         tool('calculator', {'expression': '2+2'}, prose='Let me calculate.')],
        [AIMessageChunk(content='It is 4.')],
    ])
    events = collect(model)
    assert [event['type'] for event in events] == ['text', 'step_start', 'step_end', 'text', 'done']
    assert events[0]['channel'] == 'thinking'
    assert 'Let me' not in json.dumps(events)
    assert service.calls == [(None, 'calculator', {'expression': '2+2'}, None)]
    feedback = model.histories[1][-1]
    assert isinstance(feedback, ToolMessage)
    assert feedback.tool_call_id == 'call-1'
    assert json.loads(feedback.content)['result'] == 4
    assert 'reasoning_content' not in model.histories[1][-2].additional_kwargs
    assert events[-1]['toolCalls'] == 1


def test_hardware_sequence_scoped_unique_and_redacted(service):
    model = FakeModel([
        [tool('read_project', {}, 'read')],
        [assessment(operations=('add_component', 'generate_firmware', 'compile_firmware'))],
        [tool('add_component', {'type': 'wokwi-led', 'expected_revision': 4}, 'add')],
        [tool('generate_firmware', {'source': 'void setup() {}\nvoid loop() {}'}, 'source')],
        [tool('compile_firmware', {}, 'compile')],
        [AIMessageChunk(content='Compiled, not running.')],
    ])
    events = collect(model, project_id='current', runtime_token='private-token', requirements={'behavior': 'blink'})
    assert [call[1] for call in service.calls] == [
        'read_project', 'add_component', 'generate_firmware', 'validate_circuit', 'compile_firmware', 'read_project', 'validate_circuit',
    ]
    assert [name for name, _ in service.feasibility.checks] == ['add_component', 'generate_firmware', 'compile_firmware']
    assert events[-1]['status'] == 'success'
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])
    assert events[-1]['modelCalls'] == len(model.turns)
    assert model.tool_sets[:2] == [set(agent._ASSESSMENT_TOOLS)] * 2
    assert all(names == set(agent.TOOL_NAMES) - {'assess_project_feasibility', 'ask_project_questions'} for names in model.tool_sets[2:])
    assert all(call[0] == 'current' and call[3] == 'private-token' for call in service.calls)
    starts = [item for item in events if item['type'] == 'step_start']
    ends = [item for item in events if item['type'] == 'step_end']
    assert len({item['id'] for item in starts}) == 7
    assert [item['id'] for item in starts] == [item['id'] for item in ends]
    assert all(item['status'] == 'success' for item in ends)
    assert 'private-token' not in json.dumps(events)
    assert 'private-token' not in str(model.histories)


@pytest.mark.parametrize('name,args,project,error', [
    ('read_project', {}, None, 'Select or create'),
    ('add_component', {'type': 'wokwi-led'}, 'current', 'read_project'),
    ('calculator', {'expression': '2+2', 'project_id': 'other'}, None, 'Invalid tool arguments'),
    ('calculator', {'expression': 3}, None, 'Invalid tool arguments'),
    ('edit_firmware', {'old': 'x'}, 'current', 'either source'),
    ('shell', {'command': 'whoami'}, 'current', 'Unknown tool'),
])
def test_validation_feedback(service, name, args, project, error):
    model = FakeModel([[tool(name, args)], [AIMessageChunk(content='Cannot complete the operation.')]])
    events = collect(model, project_id=project)
    end = next(item for item in events if item['type'] == 'step_end')
    assert end['status'] == 'error'
    assert error in end['output']
    assert model.histories[1][-1].status == 'error'
    assert not service.calls


def test_failed_tool_result_is_error_and_feedback(service):
    service.result = {'status': 'error', 'error': 'Compiler is unavailable'}
    model = FakeModel([[tool('read_project', {}, 'r')], [assessment(parts=(), operations=('compile_firmware',))],
                       [tool('compile_firmware', {}, 'c')], [AIMessageChunk(content='Compiler unavailable.')]])
    events = collect(model, project_id='current', requirements={'behavior': 'blink'})
    assert [item['status'] for item in events if item['type'] == 'step_end'] == ['success', 'success', 'error']
    assert [call[1] for call in service.calls] == ['read_project', 'validate_circuit', 'compile_firmware']
    assert service.feasibility.checks[0][0] == 'compile_firmware'
    assert events[-1]['status'] == 'error'
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])
    assert 'Compiler is unavailable' in json.dumps(events)
    assert model.histories[-1][-1].status == 'error'


def test_duplicate_ids_never_replay_mutation(service):
    model = FakeModel([
        [tool('read_project', {}, 'read')],
        [assessment()],
        [tool('add_component', {'type': 'wokwi-led'}, 'same')],
        [tool('add_component', {'type': 'wokwi-led'}, 'same')],
        [AIMessageChunk(content='Finished.')],
    ])
    events = collect(model, project_id='current', requirements={'behavior': 'blink'})
    assert [item[1] for item in service.calls].count('add_component') == 1
    assert [name for name, _ in service.feasibility.checks] == ['add_component']
    assert events[-1]['changes']['componentsAdded'] == 1
    assert events[-1]['toolCalls'] == len(service.calls) + 2
    assert 'Duplicate tool call ID' in json.dumps(events)


def test_model_failure_does_not_retry_completed_tool(service):
    model = FakeModel([[tool('calculator', {'expression': '2+2'})], [RuntimeError('secret-key')]])
    events = collect(model)
    assert len(service.calls) == 1
    assert len(model.histories) == 2
    assert events[-1]['status'] == 'error'
    assert 'secret-key' not in json.dumps(events)


def test_call_bounds(service, monkeypatch):
    monkeypatch.setattr(agent, 'MAX_TOOL_CALLS', 1)
    model = FakeModel([[tool('calculator', {'expression': '2+2'}, 'one')], [tool('calculator', {'expression': '2+2'}, 'two')]])
    events = collect(model)
    assert len(service.calls) == 1
    assert events[-1]['reason'] == 'limit'
    monkeypatch.setattr(agent, 'MAX_MODEL_CALLS', 1)
    model = FakeModel([[tool('calculator', {'expression': '2+2'})]])
    events = collect(model)
    assert len(model.histories) == 1
    assert events[-1]['reason'] == 'limit'


def test_recursion_bound(service, monkeypatch):
    monkeypatch.setattr(agent, 'RECURSION_LIMIT', 1)
    model = FakeModel([[tool('calculator', {'expression': '2+2'})]])
    events = collect(model)
    assert events[-1]['reason'] == 'limit'
    assert 'recursion limit' in json.dumps(events)


def test_fragmented_multiple_tool_calls(service):
    model = FakeModel([
        [AIMessageChunk(content='Selecting tools'),
         AIMessageChunk(content='', tool_call_chunks=[{'name': 'calculator', 'args': '{"expression":', 'id': 'a', 'index': 0}]),
         AIMessageChunk(content='', tool_call_chunks=[{'name': None, 'args': '"2+2"}', 'id': None, 'index': 0}]),
         AIMessageChunk(content='', tool_call_chunks=[{'name': 'calculator', 'args': '{"expression":"2*2"}', 'id': 'b', 'index': 1}])],
        [AIMessageChunk(content='Both are 4.')],
    ])
    events = collect(model)
    assert len(service.calls) == 2
    assert [call[2]['expression'] for call in service.calls] == ['2+2', '2*2']
    assert 'Selecting tools' not in json.dumps(events)
    assert len([item for item in model.histories[1] if isinstance(item, ToolMessage)]) == 2


def test_timeout_closes_model(service, monkeypatch):
    deadline = asyncio.timeout
    contexts = []

    def injected_timeout(seconds):
        context = deadline(None)
        contexts.append(context)
        return context

    monkeypatch.setattr(agent.asyncio, 'timeout', injected_timeout)

    class SlowModel(FakeModel):
        async def astream(self, history):
            contexts[-1].reschedule(asyncio.get_running_loop().time() + 0.1)
            try:
                await asyncio.Event().wait()
                yield AIMessageChunk(content='never')
            finally:
                self.closed += 1

    model = SlowModel([])
    events = collect(model)
    assert events[-1]['reason'] == 'timeout'
    assert model.closed == 1


def test_cancel_active_tool_and_generator_close(service, monkeypatch):
    async def run(close):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def command(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(service, 'command', command)
        model = FakeModel([[tool('calculator', {'expression': '2+2'})]])
        stream = agent.stream_agent(model, [HumanMessage(content='Calculate')])
        first = await anext(stream)
        assert first['type'] == 'step_start'
        await entered.wait()
        if close:
            await stream.aclose()
        else:
            task = asyncio.create_task(anext(stream))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), 1)

    asyncio.run(run(True))
    asyncio.run(run(False))


def test_actual_groq_binding_disables_transport_retries(monkeypatch):
    model = ChatGroq(api_key='fake-key', model='openai/gpt-oss-120b', max_retries=2,
                     model_kwargs={'include_reasoning': True})
    captured = {}

    async def astream(self, history, config=None, **kwargs):
        captured['tools'] = kwargs['tools']
        captured['retries'] = self.async_client._client.max_retries
        captured['model_retries'] = self.max_retries
        captured['reasoning'] = self.model_kwargs['include_reasoning']
        yield AIMessageChunk(content='Hello')

    monkeypatch.setattr(ChatGroq, 'astream', astream)
    events = collect(model)
    assert events[-1]['status'] == 'success'
    assert len(captured['tools']) == len(agent.TOOL_NAMES)
    assert captured['retries'] == captured['model_retries'] == 0
    assert captured['reasoning'] is True
    assert model.max_retries == model.async_client._client.max_retries == 2


def test_real_calculator_without_project():
    model = FakeModel([[tool('calculator', {'expression': '(2+3)*4'})], [AIMessageChunk(content='20')]])
    events = collect(model)
    end = next(item for item in events if item['type'] == 'step_end')
    assert end['status'] == 'success'
    assert json.loads(end['output'])['result'] == 20


def test_real_hardware_source_mutation(tmp_path, monkeypatch):
    from backend.hardware import HardwareService

    service = HardwareService(data_dir=tmp_path)
    project = service.create_project()
    monkeypatch.setattr(agent, '_get_service', lambda: service)
    source = 'void setup() {}\nvoid loop() {}'
    model = FakeModel([
        [tool('read_project', {}, 'read')],
        [assessment(board=project['board'], parts=(), operations=('edit_firmware',))],
        [tool('edit_firmware', {'source': source, 'expected_revision': 1}, 'edit')],
        [tool('read_firmware', {}, 'firmware')],
        [AIMessageChunk(content='Firmware updated.')],
    ])
    events = collect(model, project_id=project['id'], runtime_token=project['runtime_token'],
                     requirements={'behavior': 'idle'})
    assert all(item['status'] == 'success' for item in events if item['type'] == 'step_end')
    updated = service.get_project(project['id'])
    assert updated['revision'] == 2
    assert updated['firmware']['source'] == source
    assert updated['runtime_token'] not in json.dumps(events)


def test_timeout_active_tool_has_matching_error_end(service, monkeypatch):
    deadline = asyncio.timeout
    contexts = []
    closed = []

    def injected_timeout(seconds):
        context = deadline(None)
        contexts.append(context)
        return context

    monkeypatch.setattr(agent.asyncio, 'timeout', injected_timeout)

    async def command(*args, **kwargs):
        contexts[-1].reschedule(asyncio.get_running_loop().time() + 0.1)
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(service, 'command', command)
    model = FakeModel([[tool('calculator', {'expression': '2+2'})]])
    events = collect(model)
    start = next(item for item in events if item['type'] == 'step_start')
    end = next(item for item in events if item['type'] == 'step_end')
    assert start['id'] == end['id']
    assert end['status'] == 'error'
    assert events[-1]['reason'] == 'timeout'
    assert closed == [True]


def test_initial_build_asks_mcqs_and_pauses_without_hardware(service):
    questions = questionnaire()
    model = FakeModel([
        [tool('ask_project_questions', questions, 'questions')],
        [RuntimeError('The agent must pause before another model call.')],
    ])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build a blinking LED circuit')])
    assert [item.name for item in model.tools] == ['ask_project_questions']
    assert model.tool_choice == 'required'
    assert service.calls == []
    activity = [item for item in events if item['type'] != 'text']
    assert [item['type'] for item in activity] == ['step_start', 'step_end', 'questions', 'done']
    assert activity[0]['id'] == activity[1]['id']
    assert activity[1]['status'] == 'success'
    assert activity[2] == {'type': 'questions', **questions}
    assert events[-1]['status'] == 'awaiting_answers'
    assert events[-1]['reason'] == 'requirements_required'
    assert events[-1]['modelCalls'] == events[-1]['toolCalls'] == 1
    assert len(model.histories) == 1
    assert 'I will build' not in json.dumps(events)


def test_question_turn_ignores_following_hardware_calls(service):
    question_call = tool('ask_project_questions', questionnaire(), 'questions')
    mutation = AIMessageChunk(content='', tool_call_chunks=[{
        'name': 'add_component', 'args': '{"type":"wokwi-led"}', 'id': 'add', 'index': 1,
    }])
    model = FakeModel([[question_call, mutation], [RuntimeError('Must not resume yet')]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build an LED circuit')])
    assert service.calls == []
    assert len([item for item in events if item['type'] == 'step_start']) == 1
    assert events[-1]['status'] == 'awaiting_answers'
    assert events[-1]['toolCalls'] == 1


@pytest.mark.parametrize('invalid', ['no_questions', 'too_many', 'duplicate_questions', 'duplicate_options', 'single_option'])
def test_invalid_mcqs_do_not_pause_or_dispatch(service, invalid):
    questions = questionnaire()
    if invalid == 'no_questions':
        questions['questions'].clear()
    elif invalid == 'too_many':
        questions['questions'] *= 2
    elif invalid == 'duplicate_questions':
        questions['questions'][1]['id'] = questions['questions'][0]['id']
    elif invalid == 'duplicate_options':
        questions['questions'][0]['options'][1]['id'] = 'blink'
    else:
        questions['questions'][0]['options'].pop()
    model = FakeModel([[tool('ask_project_questions', questions)], [AIMessageChunk(content='Please retry.')]])
    events = collect(model)
    end = next(item for item in events if item['type'] == 'step_end')
    assert end['status'] == 'error'
    assert not any(item['type'] == 'questions' for item in events)
    assert model.histories[1][-1].status == 'error'
    assert service.calls == []


@pytest.mark.parametrize('name,args', [
    ('add_component', {'type': 'wokwi-led'}),
    ('edit_firmware', {'source': 'void setup() {}\nvoid loop() {}'}),
    ('compile_firmware', {}),
    ('run_simulation', {}),
])
def test_mutation_compile_and_run_require_user_confirmation(service, name, args):
    model = FakeModel([
        [tool('read_project', {}, 'read')], [tool(name, args, 'operation')],
        [AIMessageChunk(content='Requirements are needed.')],
    ])
    events = collect(model, project_id='current')
    assert [call[1] for call in service.calls] == ['read_project']
    ends = [item for item in events if item['type'] == 'step_end']
    assert [item['status'] for item in ends] == ['success', 'error']
    assert 'User-confirmed requirements' in ends[-1]['output']
    assert model.histories[-1][-1].status == 'error'


def test_initial_build_rejects_unbound_hardware_tool(service):
    model = FakeModel([[tool('read_project', {})], [AIMessageChunk(content='No changes were made.')]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build an LED circuit')])
    assert service.calls == []
    end = next(item for item in events if item['type'] == 'step_end')
    assert end['status'] == 'error'
    assert 'ask_project_questions first' in end['output']
    assert events[-1]['status'] == 'error'


@pytest.mark.parametrize('prefix', [False, True])
def test_confirmed_answers_are_context_and_resume_actual_mutation(service, prefix):
    answers = {'behavior': 'blink', 'timing': 'slow', 'runtime': 'not_run'}
    content = 'Build the LED circuit now'
    kwargs = {'requirements': answers}
    if prefix:
        content = 'Project requirements confirmed: ' + json.dumps(answers) + '\n' + content
        kwargs = {}
    model = FakeModel([
        [tool('read_project', {}, 'read')],
        [assessment()],
        [tool('add_component', {'type': 'wokwi-led'}, 'add')],
        [AIMessageChunk(content='Added the LED.')],
    ])
    events = collect(model, project_id='current', history=[HumanMessage(content=content)], **kwargs)
    assert len(model.tools) == len(agent.TOOL_NAMES) - 2
    assert [call[1] for call in service.calls] == ['read_project', 'add_component', 'read_project', 'validate_circuit']
    assert service.calls[1][2]['expected_revision'] == 4
    assert model.tool_sets == [set(agent._ASSESSMENT_TOOLS)] * 2 + [set(agent.TOOL_NAMES) - {'assess_project_feasibility', 'ask_project_questions'}] * 2
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])
    assert events[-1]['changes']['componentsAdded'] == 1
    assert not any(item['type'] == 'questions' for item in events)
    assert events[-1]['status'] == 'success'
    context = next(message for message in model.histories[0]
                   if message.type == 'system' and 'The user confirmed these project requirements:' in message.content)
    assert json.dumps(answers) in context.content
    assert 'Do not ask requirements questions again' in context.content


def test_confirmed_requirements_cannot_be_requested_again(service):
    model = FakeModel([[tool('ask_project_questions', questionnaire())], [AIMessageChunk(content='Confirmed already.')]])
    events = collect(model, requirements={'behavior': 'blink'})
    end = next(item for item in events if item['type'] == 'step_end')
    assert end['status'] == 'error'
    assert 'already confirmed' in end['output']
    assert not any(item['type'] == 'questions' for item in events)
    assert service.calls == []


def test_confirmed_build_cannot_succeed_with_advice_only(service):
    model = FakeModel([[AIMessageChunk(content='Connect an LED and upload this code.')]])
    events = collect(model, project_id='current', requirements={'behavior': 'blink'},
                     history=[HumanMessage(content='Build an LED circuit')])
    assert events[-1]['status'] == 'error'
    assert events[-1]['reason'] == 'build_not_executed'
    assert not any(item.get('channel') == 'answer' for item in events)
    assert service.calls == []


def test_injected_model_call_bound_is_enforced(service):
    model = FakeModel([[tool('calculator', {'expression': '2+2'})], [AIMessageChunk(content='4')]])
    events = collect(model, max_model_calls=1)
    assert len(model.histories) == 1
    assert len(service.calls) == 1
    assert events[-1]['status'] == 'error'
    assert events[-1]['reason'] == 'limit'


def test_injected_timeout_bound_cancels_model(service, monkeypatch):
    deadline = asyncio.timeout
    requested = []
    contexts = []

    def injected_timeout(seconds):
        requested.append(seconds)
        context = deadline(None)
        contexts.append(context)
        return context

    class SlowModel(FakeModel):
        async def astream(self, history):
            contexts[-1].reschedule(asyncio.get_running_loop().time() + 0.1)
            try:
                await asyncio.Event().wait()
                yield AIMessageChunk(content='never')
            finally:
                self.closed += 1

    monkeypatch.setattr(agent.asyncio, 'timeout', injected_timeout)
    model = SlowModel([])
    events = collect(model, timeout_seconds=0.1)
    assert model.closed == 1
    assert events[-1]['reason'] == 'timeout'
    assert requested == [0.1]


def test_empty_project_injects_first_canonical_board_question(service):
    service.board = 'unselected'
    model = FakeModel([[tool('ask_project_questions', questionnaire())]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build a esp32-s3 circuit')])
    questions = next(event['questions'] for event in events if event['type'] == 'questions')
    assert questions[0]['id'] == 'board'
    options = questions[0]['options']
    assert options[0]['id'] == 'esp32-s3'
    assert len(options) <= 6
    assert all(option['id'] in agent._board_catalog() or option['id'] == 'none' for option in options)
    assert any(option['id'] == 'none' for option in options)
    assert service.calls == []
    assert events[-1]['status'] == 'awaiting_answers'


def test_dynamic_board_only_question_is_valid_and_reordered(service):
    service.board = 'unselected'
    board_question = {'id': 'board', 'question': 'Which controller?', 'options': [
        {'id': 'esp32-s3', 'label': 'ESP32-S3'}, {'id': 'none', 'label': 'Analog only'},
    ]}
    model = FakeModel([[tool('ask_project_questions', {'summary': 'Select hardware.', 'questions': [board_question]})]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build this circuit')])
    assert next(event['questions'] for event in events if event['type'] == 'questions') == [board_question]
    assert events[-1]['status'] == 'awaiting_answers'
    questions = questionnaire()
    questions['questions'].append(board_question)
    model = FakeModel([[tool('ask_project_questions', questions)]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build this circuit')])
    assert next(event['questions'] for event in events if event['type'] == 'questions')[0] == board_question


def test_existing_project_omits_board_question(service):
    questions = questionnaire()
    questions['questions'].insert(0, agent._board_question(''))
    model = FakeModel([[tool('ask_project_questions', questions)]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build an LED circuit')])
    assert all(question['id'] != 'board' for event in events if event['type'] == 'questions'
               for question in event['questions'])
    assert 'already uses board arduino-uno' in str(model.histories[0])
    assert service.calls == []


@pytest.mark.parametrize('board_key', ['esp32', 'wokwi-arduino-uno', 'invented-board'])
def test_invalid_board_options_are_rejected(service, board_key):
    service.board = 'unselected'
    questions = {'summary': 'Select a board.', 'questions': [{'id': 'board', 'question': 'Which board?',
                 'options': [{'id': board_key, 'label': 'Controller'}, {'id': 'none', 'label': 'None'}]}]}
    model = FakeModel([[tool('ask_project_questions', questions)], [AIMessageChunk(content='Unable to plan.')]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Build a circuit')])
    assert not any(event['type'] == 'questions' for event in events)
    assert 'canonical catalog board keys' in json.dumps(events)
    assert service.calls == []


@pytest.mark.parametrize('board_key', ['esp32', 'unselected', 'invented-board'])
def test_invalid_confirmed_board_is_rejected_before_model(service, board_key):
    model = FakeModel([[RuntimeError('Must not call model')]])
    events = collect(model, project_id='current', requirements={'board': board_key},
                     history=[HumanMessage(content='Build this circuit')])
    assert events[-1]['reason'] == 'invalid_requirements'
    assert model.histories == []
    assert service.calls == []


def test_confirmed_board_is_placed_after_assessment_with_regular_events(service):
    service.board = 'unselected'
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(board='esp32-s3', parts=())],
                       [tool('add_component', {'type': 'esp32-s3'}, 'board')],
                       [AIMessageChunk(content='Selected Pico W; runtime remains unavailable.')]])
    events = collect(model, project_id='current', requirements={'board': 'esp32-s3'},
                     history=[HumanMessage(content='Build this circuit')])
    assert [call[1] for call in service.calls] == ['read_project', 'add_component', 'read_project', 'validate_circuit']
    assert service.calls[1][2] == {'type': 'esp32-s3', 'expected_revision': 4}
    starts = [event for event in events if event['type'] == 'step_start']
    ends = [event for event in events if event['type'] == 'step_end']
    assert len(starts) == len(ends) == 5
    assert [event['id'] for event in starts] == [event['id'] for event in ends]
    assert all(event['status'] == 'success' for event in ends)
    assert model.tool_choices == ['required', 'required', 'required', 'auto']
    assert model.tool_sets == [set(agent._ASSESSMENT_TOOLS)] * 2 + [set(agent.TOOL_NAMES) - {'assess_project_feasibility', 'ask_project_questions'}] * 2
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])
    assert events[-1]['changes']['componentsAdded'] == 1
    assert events[-1]['status'] == 'success'
    assert isinstance(model.histories[3][-1], ToolMessage)
    assert model.histories[3][-1].name == 'add_component'


@pytest.mark.parametrize('board_key', [None, 'none'])
def test_no_board_is_added_without_a_confirmed_board_key(service, board_key):
    service.board = 'unselected'
    answers = {'behavior': 'analog'} if board_key is None else {'board': board_key}
    model = FakeModel([[tool('read_project', {}, 'read')],
                       [assessment(board='none', parts=('arduino-uno',))],
                       [tool('add_component', {'type': 'arduino-uno'}, 'add')],
                       [AIMessageChunk(content='No board was authorized.')]])
    events = collect(model, project_id='current', requirements=answers,
                     history=[HumanMessage(content='Build an analog circuit')])
    assert [call[1] for call in service.calls] == ['read_project']
    assert 'canonical key in the confirmed board answer' in json.dumps(events)
    assert service.feasibility.checks[0][0] == 'add_component'
    assert service.feasibility.record['plan']['board'] == 'none'
    assert model.tool_choices == ['required', 'required', 'required', 'required']
    assert model.tool_sets == [set(agent._ASSESSMENT_TOOLS)] * 2 + [set(agent.TOOL_NAMES) - {'assess_project_feasibility', 'ask_project_questions'}] * 2
    assert events[-1]['changes']['componentsAdded'] == 0


def test_existing_board_is_not_automatically_replaced_by_confirmation(service):
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(board='esp32-c3', parts=('arduino-uno', 'led'))],
                       [tool('add_component', {'type': 'led'}, 'add')], [AIMessageChunk(content='Added LED.')]])
    events = collect(model, project_id='current', requirements={'board': 'esp32-c3'},
                     history=[HumanMessage(content='Build an LED circuit')])
    assert [call[2]['type'] for call in service.calls if call[1] == 'add_component'] == ['led']
    assert service.board == 'arduino-uno'
    assert model.tool_choices == ['required', 'required', 'required', 'auto']
    assert events[-1]['status'] == 'success'
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])


def test_failed_board_placement_reports_error_without_successful_mutation(service):
    service.board = 'unselected'
    service.result = {'status': 'error', 'error': 'Board placement unavailable'}
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(parts=())],
                       [tool('add_component', {'type': 'arduino-uno'}, 'board')],
                       [AIMessageChunk(content='Build blocked.')]])
    events = collect(model, project_id='current', requirements={'board': 'arduino-uno'},
                     history=[HumanMessage(content='Build this circuit')])
    assert [call[1] for call in service.calls] == ['read_project', 'add_component']
    assert events[-1]['changes']['componentsAdded'] == 0
    assert events[-1]['status'] == 'error'
    assert model.tool_choices == ['required', 'required', 'required', 'required']
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])
    assert service.feasibility.checks[0][0] == 'add_component'
    assert 'Board placement unavailable' in json.dumps(events)


def test_build_model_text_streams_live_narration_without_generic_model_step(service):
    model = FakeModel([[tool('read_project', {}, 'read', prose='Checking the current circuit.')], [assessment()],
                       [tool('add_component', {'type': 'led'}, 'add')], [AIMessageChunk(content='Added LED.')]])
    events = collect(model, project_id='current', requirements={'behavior': 'steady'},
                     history=[HumanMessage(content='Build an LED circuit')])
    narration = [event['text'] for event in events if event.get('channel') == 'narration']
    assert 'Checking the current circuit.' in narration
    assert [event['label'] for event in events if event['type'] == 'step_start'] == [
        'Read project', 'Assess project feasibility', 'Add component', 'Read project', 'Validate circuit',
    ]
    assert model.tool_choices == ['required', 'required', 'required', 'auto']
    assert events[-1]['status'] == 'success'
    assert events[-1]['toolCalls'] == len([event for event in events if event['type'] == 'step_start'])


def test_board_placement_replans_preflight_batched_mutations_for_new_revision(service):
    service.board = 'unselected'
    batched = AIMessageChunk(content='', tool_call_chunks=[
        {'name': 'read_project', 'args': '{}', 'id': 'read', 'index': 0},
        {'name': 'add_component', 'args': '{"type":"led","expected_revision":4}', 'id': 'led', 'index': 1},
    ])
    model = FakeModel([[batched], [assessment(board='esp32-c3')],
                       [tool('add_component', {'type': 'esp32-c3'}, 'board')],
                       [tool('add_component', {'type': 'led'}, 'replanned-led')],
                       [AIMessageChunk(content='Board and LED selected.')]])
    events = collect(model, project_id='current', requirements={'board': 'esp32-c3'},
                     history=[HumanMessage(content='Build this circuit')])
    assert [call[1] for call in service.calls] == ['read_project', 'add_component', 'add_component', 'read_project', 'validate_circuit']
    placements = [call[2] for call in service.calls if call[1] == 'add_component']
    assert placements == [{'type': 'esp32-c3', 'expected_revision': 4}, {'type': 'led', 'expected_revision': 5}]
    feedback = [message for message in model.histories[1] if isinstance(message, ToolMessage)]
    assert {message.tool_call_id for message in feedback} >= {'read', 'led'}
    assert next(message for message in feedback if message.tool_call_id == 'led').status == 'error'
    assert events[-1]['changes']['componentsAdded'] == 2
    assert events[-1]['toolCalls'] == len(service.calls) + 2
    assert events[-1]['status'] == 'success'


def test_confirmed_board_placement_uses_real_hardware_and_feasibility_services(tmp_path, monkeypatch):
    from backend.hardware import HardwareService
    hardware = HardwareService(data_dir=tmp_path)
    project = hardware.create_project(board='unselected')
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(board='arduino-nano', parts=())],
                       [tool('add_component', {'type': 'arduino-nano'}, 'board')],
                       [AIMessageChunk(content='Arduino Nano selected.')]])
    events = collect(model, project_id=project['id'], requirements={'board': 'arduino-nano'},
                     history=[HumanMessage(content='Build this project')])
    updated = hardware.get_project(project['id'])
    assert updated['board'] == 'arduino-nano'
    assert updated['components'][0]['id'] == 'board'
    assert updated['components'][0]['type'] == 'arduino-nano'
    assert updated['revision'] == project['revision'] + 1
    assert events[-1]['changes']['componentsAdded'] == 1
    assert events[-1]['status'] == 'success'


def test_groq_quota_remains_an_explicit_blocker_without_mutation(service, monkeypatch):
    class QuotaError(Exception):
        status_code = 429

    delays = []

    async def no_wait(delay):
        delays.append(delay)

    monkeypatch.setattr(agent.asyncio, 'sleep', no_wait)
    model = FakeModel([[QuotaError('Quota exhausted')]])
    events = collect(model, project_id='current', requirements={'board': 'arduino-uno'},
                     history=[HumanMessage(content='Build this circuit')])
    assert len(delays) == 0
    assert service.calls == []
    assert events[-1]['status'] == 'error'
    assert 'Groq rate limit reached' in json.dumps(events)


def test_full_message_astream_fallback_preserves_tools_and_usage(service):
    model = FakeModel([
        [AIMessage(content='', tool_calls=[{'name': 'calculator', 'args': {'expression': '2+2'},
                                          'id': 'full-call', 'type': 'tool_call'}],
                   usage_metadata={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12},
                   response_metadata={'model_name': 'fallback-model'})],
        [AIMessage(content='It is 4.', usage_metadata={'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 8})],
    ])
    events = collect(model)
    assert events[-1]['status'] == 'success'
    assert events[-1]['usage'] == {'input_tokens': 15, 'output_tokens': 5, 'total_tokens': 20}
    assert events[-1]['model'] == 'fallback-model'
    assert model.histories[1][-1].tool_call_id == 'full-call'
    assert model.histories[1][-2].tool_calls[0]['args'] == {'expression': '2+2'}
    assert [call[1] for call in service.calls] == ['calculator']


@pytest.mark.parametrize('full_message', [False, True])
@pytest.mark.parametrize('with_tool', [False, True])
def test_length_finish_reason_is_error_without_executing_truncated_tools(service, full_message, with_tool):
    chunk = tool('calculator', {'expression': '2+2'}) if with_tool else AIMessageChunk(content='Partial answer')
    chunk.response_metadata = {'finish_reason': 'length'}
    if full_message:
        chunk = AIMessage(**chunk.model_dump(exclude={'type', 'tool_call_chunks', 'chunk_position'}))
    events = collect(FakeModel([[chunk]]))
    assert events[-1]['status'] == 'error'
    assert events[-1]['reason'] == 'response_truncated'
    assert not any(event.get('channel') == 'answer' for event in events)
    assert service.calls == []


def test_plan_operations_schema_exposes_exact_executable_names():
    schema = agent._Assessment.model_json_schema()
    operations = schema['$defs']['_Plan']['properties']['operations']['items']['enum']
    assert set(operations) == set(agent.HARDWARE_TOOL_NAMES)
    assert 'wire_circuit' in operations


@pytest.mark.parametrize('operation', ['wire', 'compile', 'shell', 'ask_project_questions', 'assess_project_feasibility'])
def test_plan_operations_reject_nonexecutable_names(service, operation):
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(operations=(operation,))],
                       [AIMessageChunk(content='Invalid plan.')]])
    events = collect(model, project_id='current', requirements={'behavior': 'blink'})
    assert 'Invalid tool arguments' in json.dumps(events)
    assert service.feasibility.record is None
    assert [call[1] for call in service.calls] == ['read_project']


def test_ai_choose_board_placement_uses_approved_canonical_board(tmp_path, monkeypatch):
    from backend.hardware import HardwareService
    hardware = HardwareService(data_dir=tmp_path)
    project = hardware.create_project(board='unselected')
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(board='arduino-nano', parts=())],
                       [tool('add_component', {'type': 'wokwi-arduino-nano'}, 'board')],
                       [AIMessageChunk(content='Selected the approved Arduino Nano.')]])
    events = collect(model, project_id=project['id'], requirements={'board': 'ai_choose'},
                     history=[HumanMessage(content='Build this project')])
    assert events[-1]['status'] == 'success'
    assert hardware.get_project(project['id'])['board'] == 'arduino-nano'
    assert events[-1]['changes']['componentsAdded'] == 1


def test_compile_only_stops_requiring_tools_after_current_artifact(service):
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(parts=(), operations=('compile_firmware',))],
                       [tool('compile_firmware', {}, 'compile')], [AIMessageChunk(content='Compiled, not run.')]])
    events = collect(model, project_id='current', requirements={'runtime': 'compile'},
                     history=[HumanMessage(content='Compile the current firmware')])
    assert events[-1]['status'] == 'success'
    assert model.tool_choices == ['required', 'required', 'required', 'auto']
    assert events[-1]['changes']['firmwareWrites'] == 0
    assert events[-1]['successfulTools']['compile_firmware'] == 1


def test_compile_only_failed_artifact_still_requires_work(service):
    service.result = {'status': 'error', 'error': 'Compiler unavailable'}
    model = FakeModel([[tool('read_project', {}, 'read')], [assessment(parts=(), operations=('compile_firmware',))],
                       [tool('compile_firmware', {}, 'compile')], [AIMessageChunk(content='Blocked.')]])
    events = collect(model, project_id='current', requirements={'runtime': 'compile'},
                     history=[HumanMessage(content='Compile the current firmware')])
    assert events[-1]['status'] == 'error'
    assert model.tool_choices[-1] == 'required'


@pytest.mark.parametrize('requirements', [None, {'behavior': 'blink'}])
def test_emergency_stop_needs_no_requirements_or_feasibility(service, requirements):
    model = FakeModel([[tool('stop_simulation', {}, 'stop')], [AIMessageChunk(content='Simulation stopped.')]])
    events = collect(model, project_id='current', runtime_token='browser-token', requirements=requirements,
                     history=[HumanMessage(content='Stop simulation now')])
    assert events[-1]['status'] == 'success'
    assert model.tool_sets == [{'stop_simulation'}] * 2
    assert model.tool_choices == ['required', 'auto']
    assert service.calls == [('current', 'stop_simulation', {}, 'browser-token')]
    assert service.feasibility.record is None
    assert not any(event['type'] in ('questions', 'feasibility') for event in events)


def test_emergency_stop_does_not_expose_mutation_tools(service):
    model = FakeModel([[tool('add_component', {'type': 'led'})], [AIMessageChunk(content='Blocked.')]])
    events = collect(model, project_id='current', history=[HumanMessage(content='Stop simulation')])
    assert all(names == {'stop_simulation'} for names in model.tool_sets)
    assert service.calls == []
    assert 'read_project' in json.dumps(events)


def test_structured_http_errors_preserve_bounded_redacted_feedback(service, monkeypatch):
    from fastapi import HTTPException

    async def fail(*args, **kwargs):
        raise HTTPException(409, detail={'message': 'Stale project browser-secret', 'expected_revision': 4,
                            'actual_revision': 5, 'runtime_token': 'browser-secret',
                            'nested': {'api_key': 'private-key', 'errors': ['Use revision 5', 'x' * 10000]}})

    monkeypatch.setattr(service, 'command', fail)
    model = FakeModel([[tool('read_project', {})], [AIMessageChunk(content='Read blocked.')]])
    events = collect(model, project_id='current', runtime_token='browser-secret')
    feedback = json.loads(model.histories[1][-1].content)
    assert feedback['status'] == 'error'
    assert 'actual_revision' in feedback['error']
    assert 'Use revision 5' in feedback['error']
    assert len(feedback['error']) <= 4000
    assert 'browser-secret' not in json.dumps(events) + str(model.histories)
    assert 'private-key' not in json.dumps(events) + str(model.histories)
    assert 'runtime_token' not in feedback['error']


def test_search_components_retains_property_metadata(service, monkeypatch):
    metadata = {'id': 'resistor', 'name': 'Resistor', 'pins': ['1', '2'],
                'properties': [{'id': 'resistance', 'type': 'number', 'default': '1000'}],
                'defaultValues': {'resistance': '1000'}, 'internal_data': 'omit'}

    async def search(*args, **kwargs):
        return {'components': [metadata]}

    monkeypatch.setattr(service, 'command', search)
    model = FakeModel([[tool('search_components', {'query': 'resistor'})], [AIMessageChunk(content='Found resistor.')]])
    events = collect(model, project_id='current')
    result = json.loads(model.histories[1][-1].content)['components'][0]
    assert events[-1]['status'] == 'success'
    assert result['properties'] == metadata['properties']
    assert result['defaultValues'] == metadata['defaultValues']
    assert 'internal_data' not in result


def test_full_message_fallback_preserves_invalid_tool_feedback(service):
    message = AIMessage(content='', invalid_tool_calls=[{
        'name': 'calculator', 'args': '{"expression":', 'id': 'invalid-full',
        'error': 'Invalid JSON', 'type': 'invalid_tool_call',
    }])
    model = FakeModel([[message], [AIMessage(content='The tool arguments were invalid.')]])
    events = collect(model)
    assert events[-1]['toolCalls'] == 1
    assert model.histories[1][-1].tool_call_id == 'invalid-full'
    assert model.histories[1][-1].status == 'error'
    assert service.calls == []


def test_ai_choose_cannot_place_a_different_board_even_if_listed_as_part(service):
    service.board = 'unselected'
    model = FakeModel([[tool('read_project', {}, 'read')],
                       [assessment(board='arduino-nano', parts=('arduino-uno',))],
                       [tool('add_component', {'type': 'arduino-uno'}, 'board')],
                       [AIMessageChunk(content='Wrong board was blocked.')]])
    events = collect(model, project_id='current', requirements={'board': 'ai_choose'},
                     history=[HumanMessage(content='Build this circuit')])
    assert events[-1]['status'] == 'error'
    assert service.board == 'unselected'
    assert not any(call[1] == 'add_component' for call in service.calls)
    assert 'canonical key in the confirmed board answer' in json.dumps(events)


def test_warning_prompt_matches_automatic_server_resolution():
    assert 'Warnings require user consent' not in agent.SYSTEM_PROMPT
    assert 'without asking\nfor a second confirmation' in agent.SYSTEM_PROMPT
    assert 'Incompatible or blocked plans authorize no changes' in agent.SYSTEM_PROMPT


@pytest.mark.parametrize('repair', [False, True])
def test_connection_check_agent_revisions_and_repair_authorization(tmp_path, monkeypatch, repair):
    from backend.hardware import HardwareService
    hardware = HardwareService(data_dir=tmp_path)
    project = hardware.create_project(board='arduino-uno')
    with hardware.lock:
        stored = hardware._load(project['id'])
        stored['wires'] = [{'id': 'alias', 'from': {'component': 'board', 'pin': 'D13'},
                            'to': {'component': 'board', 'pin': '12'}}]
        hardware._save(stored)
    monkeypatch.setattr(agent, '_get_service', lambda: hardware)
    turns = [[tool('read_project', {}, 'read')]]
    if repair:
        turns.append([assessment(parts=(), operations=('check_connections',))])
    turns.extend([[tool('check_connections', {'repair': repair}, 'check')],
                  [AIMessageChunk(content='Connections checked; electrical behavior is unverified.')]])
    model = FakeModel(turns)
    events = collect(model, project_id=project['id'],
                     requirements={'behavior': 'idle'} if repair else None)
    assert events[-1]['status'] == 'success'
    updated = hardware.get_project(project['id'])
    assert updated['revision'] == project['revision'] + int(repair)
    assert updated['wires'][0]['from']['pin'] == ('13' if repair else 'D13')
    assert events[-1]['successfulTools']['check_connections'] == 1
    if repair:
        record = next(iter(hardware._load(project['id'])['_feasibility'].values()))
        assert record['revision'] == updated['revision']


def test_connection_repair_cannot_bypass_confirmed_requirements(service):
    model = FakeModel([[tool('read_project', {}, 'read')],
                       [tool('check_connections', {'repair': True}, 'repair')],
                       [AIMessageChunk(content='Repair requires confirmation.')]])
    events = collect(model, project_id='current')
    assert [call[1] for call in service.calls] == ['read_project']
    assert 'User-confirmed requirements' in json.dumps(events)


@pytest.mark.parametrize('read_first', [False, True])
def test_connection_repair_requires_read_and_reviewed_operation(service, read_first):
    turns = []
    if read_first:
        turns.extend([[tool('read_project', {}, 'read')],
                      [assessment(parts=(), operations=('validate_circuit',))]])
    turns.extend([[tool('check_connections', {'repair': True}, 'repair')],
                  [AIMessageChunk(content='Repair was not authorized.')]])
    events = collect(FakeModel(turns), project_id='current', requirements={'behavior': 'idle'})
    assert not any(call[1] == 'check_connections' for call in service.calls)
    assert ('outside reviewed plan' if read_first else 'read_project successfully') in json.dumps(events)
