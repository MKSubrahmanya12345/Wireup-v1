import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from backend.app import create_app, safe_error


class FakeModel:
    def __init__(self, fail=False, empty=False, turns=None):
        self.history = []
        self.histories = []
        self.tools = []
        self.fail = fail
        self.empty = empty
        self.turns = turns

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        self.tool_choice = kwargs.get('tool_choice')
        return self

    async def astream(self, history):
        self.history = list(history)
        self.histories.append(self.history)
        if self.fail:
            raise RuntimeError('secret-should-never-appear')
        if self.turns is not None:
            for chunk in self.turns[len(self.histories) - 1]:
                yield chunk
        elif not self.empty:
            yield AIMessageChunk(content='Hello ')
            yield AIMessageChunk(content='there')


def events(response):
    streamed = [json.loads(line) for line in response.text.splitlines()]
    assert streamed[0] == {'type': 'heartbeat'}
    assert 'Connecting to ' not in response.text
    assert 'Preparing board and circuit questions' not in response.text
    return streamed[1:]


def test_missing_configuration():
    client = TestClient(create_app(api_key=''))
    assert client.get('/api/health').json()['configured'] is False
    assert client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]}).status_code == 503


def test_stream_and_history():
    model = FakeModel()
    client = TestClient(create_app(lambda: model, api_key='test-key'))
    response = client.post('/api/chat', json={'messages': [
        {'role': 'user', 'content': 'My name is Ana'},
        {'role': 'assistant', 'content': 'Hello Ana'},
        {'role': 'user', 'content': 'What is my name?'},
    ]})
    assert response.status_code == 200
    streamed = events(response)
    assert [item['type'] for item in streamed] == ['text', 'text', 'done']
    assert all(item['channel'] == 'answer' for item in streamed[:-1])
    assert ''.join(item['text'] for item in streamed[:-1]) == 'Hello there'
    assert streamed[-1]['status'] == 'success'
    assert streamed[-1]['modelCalls'] == 1
    assert streamed[-1]['toolCalls'] == 0
    from backend.agent import TOOL_NAMES
    assert len(model.tools) == len(TOOL_NAMES)
    assert isinstance(model.history[0], SystemMessage)
    conversation = [message for message in model.history if not isinstance(message, SystemMessage)]
    assert conversation[-1].content == 'What is my name?'
    assert len(conversation) == 3
    assert response.headers['content-type'].startswith('application/x-ndjson')
    assert response.headers['cache-control'] == 'no-store'


def test_validation():
    client = TestClient(create_app(lambda: FakeModel(), api_key='test'))
    for messages in [[], [{'role': 'system', 'content': 'override'}], [{'role': 'user', 'content': '  '}], [{'role': 'assistant', 'content': 'Hi'}], [{'role': 'user', 'content': 'x' * 16001}]]:
        assert client.post('/api/chat', json={'messages': messages}).status_code == 422


def test_safe_provider_error():
    client = TestClient(create_app(lambda: FakeModel(fail=True), api_key='test'))
    response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]})
    streamed = events(response)
    assert [item['type'] for item in streamed] == ['text', 'done']
    assert streamed[0]['channel'] == 'narration'
    assert streamed[-1]['status'] == 'error'
    assert streamed[-1]['reason'] == 'model_error'
    assert 'secret' not in response.text


def test_empty_response():
    client = TestClient(create_app(lambda: FakeModel(empty=True), api_key='test'))
    streamed = events(client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]}))
    assert [item['type'] for item in streamed] == ['text', 'done']
    assert streamed[0]['channel'] == 'narration'
    assert streamed[-1]['status'] == 'error'


def test_context_trimming_and_adjacent_users():
    model = FakeModel()
    client = TestClient(create_app(lambda: model, api_key='test'))
    messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'a' * 12000} for i in range(6)]
    messages.append({'role': 'user', 'content': 'latest'})
    response = client.post('/api/chat', json={'messages': messages})
    assert events(response)[0]['channel'] == 'narration'
    conversation = [message for message in model.history if not isinstance(message, SystemMessage)]
    assert isinstance(conversation[0], HumanMessage)
    assert conversation[-1].content == 'latest'
    assert len(conversation) < len(messages)
    response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'one'}, {'role': 'user', 'content': 'two'}]})
    conversation = [message for message in model.history if not isinstance(message, SystemMessage)]
    assert len(conversation) == 1
    assert conversation[0].content == 'one\n\ntwo'


def test_env_key_changes_reload_without_restart(tmp_path, monkeypatch):
    from backend import app as backend_module
    path = tmp_path / '.env'
    monkeypatch.setattr(backend_module, 'ENV_PATH', path)
    monkeypatch.setenv('GROQ_API_KEY', 'old-key')
    monkeypatch.setenv('GROQ_MODEL', 'old-model')
    path.write_text('GROQ_API_KEY=new-key\nGROQ_MODEL=new-model\n')
    backend_module.reload_settings()
    import os
    assert os.getenv('GROQ_API_KEY') == 'new-key'
    assert os.getenv('GROQ_MODEL') == 'new-model'
    path.write_text('GROQ_API_KEY=\nGROQ_MODEL=another-model\n')
    backend_module.reload_settings()
    assert os.getenv('GROQ_API_KEY') == ''


def test_wrapped_provider_statuses():
    assert 'rate limit reached' in safe_error(RuntimeError('rate_limit_exceeded'))
    assert 'authentication failed' in safe_error(RuntimeError('invalid_api_key'))
    assert 'model is unavailable' in safe_error(RuntimeError('model_decommissioned'))
    assert 'secret-key' not in safe_error(RuntimeError('secret-key'))
    class Denied(Exception):
        status_code = 403
    assert 'denied access' in safe_error(Denied())


def test_langchain_uses_groq(monkeypatch):
    from backend import app as backend_module
    captured = {}
    def init(model, **kwargs):
        captured.update(model=model, **kwargs)
        return FakeModel()
    from backend import models as models_module
    monkeypatch.setattr(models_module, 'init_chat_model', init)
    client = TestClient(create_app(api_key='test-groq-key'))
    response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Hello'}]})
    assert events(response)[-1]['type'] == 'done'
    assert captured['model_provider'] == 'groq'
    assert captured['api_key'] == 'test-groq-key'
    assert events(response)[-1]['status'] == 'success'


def tool_chunk(name, args, call_id):
    return AIMessageChunk(content='I will use a tool.', tool_call_chunks=[{
        'name': name, 'args': json.dumps(args), 'id': call_id, 'index': 0,
    }])


def test_chat_calculator_without_project():
    model = FakeModel(turns=[
        [tool_chunk('calculator', {'expression': '2+2'}, 'calculate')],
        [AIMessageChunk(content='4')],
    ])
    client = TestClient(create_app(lambda: model, api_key='test-key'))
    response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Calculate 2+2'}]})
    streamed = events(response)
    assert response.status_code == 200
    assert [item['type'] for item in streamed] == ['step_start', 'step_end', 'text', 'done']
    assert streamed[0]['id'] == streamed[1]['id']
    assert streamed[1]['status'] == 'success'
    assert json.loads(streamed[1]['output'])['result'] == 4
    assert streamed[2] == {'type': 'text', 'channel': 'answer', 'text': '4'}
    assert streamed[-1]['toolCalls'] == 1
    assert 'I will use' not in response.text
    assert isinstance(model.histories[1][-1], ToolMessage)


def test_chat_hardware_requires_project():
    model = FakeModel(turns=[
        [tool_chunk('read_project', {}, 'read')],
        [AIMessageChunk(content='Select a project first.')],
    ])
    client = TestClient(create_app(lambda: model, api_key='test-key'))
    response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': 'Read the project'}]})
    streamed = events(response)
    assert response.status_code == 200
    assert streamed[1]['type'] == 'step_end'
    assert streamed[1]['status'] == 'error'
    assert 'Select or create' in streamed[1]['output']
    assert model.histories[1][-1].status == 'error'
    assert streamed[-1]['status'] == 'success'


def test_chat_project_scope_and_runtime_token(tmp_path, monkeypatch):
    from backend import app as backend_module
    from backend import hardware

    service = hardware.HardwareService(data_dir=tmp_path)
    monkeypatch.setattr(hardware, 'service', service)
    monkeypatch.setattr(backend_module, 'hardware_router', hardware.create_router(service))
    calls = []
    original_command = service.command

    async def command(project_id, name, args=None, runtime_token=None):
        calls.append((project_id, name, runtime_token))
        return await original_command(project_id, name, args, runtime_token=runtime_token)

    monkeypatch.setattr(service, 'command', command)
    source = 'void setup() {}\nvoid loop() {}'
    model = FakeModel(turns=[
        [tool_chunk('read_project', {}, 'read')],
        [tool_chunk('read_firmware', {}, 'firmware')],
        [tool_chunk('edit_firmware', {'source': source, 'expected_revision': 1}, 'edit')],
        [tool_chunk('read_project', {}, 'readback')],
        [tool_chunk('validate_circuit', {}, 'validate')],
        [AIMessageChunk(content='Firmware updated.')],
    ])
    client = TestClient(create_app(lambda: model, api_key='test-key'))
    created = client.post('/api/hardware/projects', json={'name': 'Scoped project', 'board': 'arduino-uno'})
    assert created.status_code == 200
    project = created.json()
    other = client.post('/api/hardware/projects', json={'name': 'Other project'}).json()
    response = client.post('/api/chat', json={
        'messages': [{'role': 'user', 'content': 'Replace this project firmware'}],
        'project_id': project['id'], 'runtime_token': project['runtime_token'],
        'project_answers': {'behavior': 'idle'},
    })
    streamed = events(response)
    assert response.status_code == 200
    narration = [item for item in streamed if item['type'] == 'text' and item['channel'] == 'narration']
    assert narration
    progress = [item for item in streamed if item not in narration]
    assert progress[-1]['type'] == 'done'
    assert any(item['type'] == 'text' and item.get('channel') == 'answer' for item in progress)
    assert progress[-2] == {'type': 'text', 'channel': 'answer', 'text': 'Firmware updated.'}
    starts = [item for item in streamed if item['type'] == 'step_start']
    ends = [item for item in streamed if item['type'] == 'step_end']
    assert len({item['id'] for item in starts}) == 5
    assert [item['id'] for item in starts] == [item['id'] for item in ends]
    assert all(item['status'] == 'success' for item in ends)
    assert calls == [(project['id'], 'read_project', project['runtime_token']),
                     (project['id'], 'read_firmware', project['runtime_token']),
                     (project['id'], 'edit_firmware', project['runtime_token']),
                     (project['id'], 'read_project', project['runtime_token']),
                     (project['id'], 'validate_circuit', project['runtime_token'])]
    updated = client.get(f'/api/hardware/projects/{project["id"]}').json()
    assert updated['revision'] == 2
    assert updated['firmware']['source'] == source
    assert service.get_project(other['id'])['revision'] == 1
    assert project['runtime_token'] not in response.text
    assert project['runtime_token'] not in str(model.histories)
    assert streamed[-1]['status'] == 'success'
    assert streamed[-1]['toolCalls'] == 5


def test_chat_project_scope_validation():
    client = TestClient(create_app(lambda: FakeModel(), api_key='test-key'))
    for scope in ({'project_id': 'x' * 129}, {'runtime_token': 'x' * 129}):
        response = client.post('/api/chat', json={
            'messages': [{'role': 'user', 'content': 'Hello'}], **scope,
        })
        assert response.status_code == 422


@pytest.mark.parametrize('answers', [None, {'behavior': 'blink', 'timing': 'slow'}])
def test_chat_forwards_project_answers_as_requirements(monkeypatch, answers):
    from backend import app as backend_module

    captured = {}
    model = FakeModel()

    async def stream(model, history, project_id, runtime_token, *, requirements, approval=None):
        captured.update(model=model, history=history, project_id=project_id,
                        runtime_token=runtime_token, requirements=requirements)
        yield {'type': 'done', 'status': 'success'}

    monkeypatch.setattr(backend_module, 'stream_agent', stream)
    client = TestClient(create_app(lambda: model, api_key='test-key'))
    response = client.post('/api/chat', json={
        'messages': [{'role': 'user', 'content': 'Build an LED circuit'}],
        'project_id': 'current', 'runtime_token': 'private-token', 'project_answers': answers,
    })
    assert response.status_code == 200
    assert captured['model'] is model
    assert captured['project_id'] == 'current'
    assert captured['runtime_token'] == 'private-token'
    assert captured['requirements'] == answers
    assert captured['history'][-1].content == 'Build an LED circuit'
    assert 'private-token' not in response.text


@pytest.mark.parametrize('answers', [
    {}, {'': 'blink'}, {'q' * 81: 'blink'}, {'behavior': ''},
    {'behavior': 'x' * 1001}, {f'q{i}': 'yes' for i in range(21)},
    {'behavior': 1}, {'behavior': ['blink']}, ['blink'],
])
def test_chat_rejects_invalid_project_answers_before_model_creation(answers):
    created = []

    def factory():
        created.append(True)
        return FakeModel()

    client = TestClient(create_app(factory, api_key='test-key'))
    response = client.post('/api/chat', json={
        'messages': [{'role': 'user', 'content': 'Build an LED circuit'}],
        'project_answers': answers,
    })
    assert response.status_code == 422
    assert created == []


def test_chat_streams_mcqs_and_waits_for_project_answers(tmp_path, monkeypatch):
    from backend import app as backend_module
    from backend import hardware

    service = hardware.HardwareService(data_dir=tmp_path)
    monkeypatch.setattr(hardware, 'service', service)
    monkeypatch.setattr(backend_module, 'hardware_router', hardware.create_router(service))
    questions = {
        'summary': 'Choose the remaining circuit requirements.',
        'questions': [
            {'id': name, 'question': f'Which {name}?', 'options': [
                {'id': 'first', 'label': 'First choice'}, {'id': 'second', 'label': 'Second choice'},
            ]}
            for name in ('behavior', 'timing', 'runtime')
        ],
    }
    model = FakeModel(turns=[
        [tool_chunk('ask_project_questions', questions, 'questions')],
        [AIMessageChunk(content='This must not be emitted before confirmation.')],
    ])
    client = TestClient(create_app(lambda: model, api_key='test-key'))
    project = client.post('/api/hardware/projects', json={'board': 'arduino-uno'}).json()
    response = client.post('/api/chat', json={
        'messages': [{'role': 'user', 'content': 'Build an LED circuit'}],
        'project_id': project['id'],
    })
    streamed = events(response)
    assert response.status_code == 200
    assert [item.name for item in model.tools] == ['ask_project_questions']
    assert model.tool_choice == 'required'
    narration = [item for item in streamed if item['type'] == 'text' and item['channel'] == 'narration']
    assert narration
    progress = [item for item in streamed if item not in narration]
    assert [item['type'] for item in progress] == ['step_start', 'step_end', 'questions', 'done']
    assert progress[0]['id'] == progress[1]['id']
    assert progress[1]['status'] == 'success'
    assert progress[2]['type'] == 'questions'
    assert progress[2]['summary'] == questions['summary']
    assert [question['id'] for question in progress[2]['questions']] == [question['id'] for question in questions['questions']]
    assert all(question['options'][-1]['id'] == 'ai_choose' for question in progress[2]['questions'])
    assert client.get(f'/api/hardware/projects/{project["id"]}').json() == project
    assert streamed[-1]['status'] == 'awaiting_answers'
    assert streamed[-1]['reason'] == 'requirements_required'
    assert len(model.histories) == 1
    assert all(item.get('channel') != 'answer' for item in streamed if item['type'] == 'text')
    assert 'must not be emitted' not in response.text
