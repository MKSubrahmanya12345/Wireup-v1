import asyncio
import copy
import json

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessageChunk, HumanMessage

from backend import agent
from backend.hardware import HardwareService


ORIGINAL = '''#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

Adafruit_SSD1306 display(128, 64, &Wire, -1);

void setup() {
  display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  display.clearDisplay();
  display.display();
}

void keepExistingBehavior() {
  // Existing project behavior must survive the targeted edit.
}

void loop() {
  keepExistingBehavior();
}
'''

UPDATED = ORIGINAL.replace(
    '  display.clearDisplay();\n  display.display();',
    '''  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  const char *message = "Hello! WireUp";
  for (const char *p = message; *p; ++p) {
    display.write(*p);
    display.display();
    delay(140);
  }''',
)


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


class Compiler:
    def __init__(self, success=True):
        self.success = success
        self.calls = 0

    async def compile(self, project):
        self.calls += 1
        artifact = None
        if self.success:
            artifact = {
                'id': f'oled-{self.calls}', 'format': 'hex', 'board': project['board'],
                'source_revision': project['firmware']['revision'],
                'project_revision': project['revision'], 'hex': ':00000001FF',
            }
        return {
            'status': 'compilation_complete' if self.success else 'error',
            'success': self.success, 'errors': [] if self.success else ['Adafruit_SSD1306.h: No such file'],
            'stderr': '' if self.success else 'Adafruit_SSD1306.h: No such file',
            'stdout': '', 'artifact': artifact, 'board': project['board'],
            'source_revision': project['firmware']['revision'],
            'project_revision': project['revision'], 'stale': False,
        }


def seed_oled(service):
    project = service.create_project(board='arduino-uno')

    async def seed():
        current = await service.command(project['id'], 'add_component', {
            'type': 'ssd1306-i2c-4pin', 'id': 'oled',
        })
        current = (await service.command(project['id'], 'wire_circuit', {
            'expected_revision': current['revision'],
            'batch': [
                {'id': 'oled-vcc', 'from': {'component': 'board', 'pin': '5V'}, 'to': {'component': 'oled', 'pin': 'VCC'}},
                {'id': 'oled-gnd', 'from': {'component': 'board', 'pin': 'GND'}, 'to': {'component': 'oled', 'pin': 'GND'}},
                {'id': 'oled-sda', 'from': {'component': 'board', 'pin': 'A4'}, 'to': {'component': 'oled', 'pin': 'SDA'}},
                {'id': 'oled-scl', 'from': {'component': 'board', 'pin': 'A5'}, 'to': {'component': 'oled', 'pin': 'SCL'}},
            ],
        }))['project']
        await service.command(project['id'], 'generate_firmware', {
            'source': ORIGINAL, 'expected_revision': current['revision'],
        })

    asyncio.run(seed())
    return project['id']


def collect(service, model, project_id, text):
    original = service.command
    calls = []

    async def command(identity, name, args=None, runtime_token=None):
        calls.append((name, copy.deepcopy(args or {})))
        return await original(identity, name, args, runtime_token)

    old = agent._get_service
    agent._get_service = lambda: service
    service.command = command
    try:
        async def run():
            return [event async for event in agent.stream_agent(
                model, [HumanMessage(content=text)], project_id=project_id,
            )]
        return asyncio.run(run()), calls
    finally:
        service.command = original
        agent._get_service = old


def test_existing_oled_firmware_is_saved_compiled_and_preserves_hardware(tmp_path):
    compiler = Compiler()
    service = HardwareService(tmp_path, compiler=compiler)
    project_id = seed_oled(service)
    before = service.get_project(project_id)
    model = Model([
        call('read_project'), call('read_firmware'),
        call('edit_firmware', {'source': UPDATED}), call('compile_firmware'),
        AIMessageChunk(content='Saved revision and compiled it. Simulation was not run.'),
    ])

    events, calls = collect(
        service, model, project_id,
        'Update the firmware to initialize the OLED at 0x3C, reveal Hello! WireUp one character at a time every 140 ms, and leave it visible.',
    )
    after = service.get_project(project_id)

    assert events[-1]['status'] == 'success'
    assert not any(event['type'] == 'questions' for event in events)
    assert after['firmware']['source'] == UPDATED
    assert after['revision'] == before['revision'] + 1
    assert after['components'] == before['components']
    assert after['wires'] == before['wires']
    assert after['compiler']['artifact']['project_revision'] == after['revision']
    assert after['compiler']['artifact']['source_revision'] == after['firmware']['revision']
    assert [name for name, _ in calls] == [
        'read_project', 'read_firmware', 'edit_firmware',
        'validate_circuit', 'compile_firmware', 'read_project',
    ]
    assert events[-1]['changes']['firmwareWrites'] == 1
    assert 'run_simulation' not in events[-1]['successfulTools']


def test_combined_existing_change_uses_modification_review_not_new_questionnaire(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler())
    project_id = seed_oled(service)
    model = Model([call('read_project'), AIMessageChunk(content='The change requires a reviewed plan.')])

    events, calls = collect(
        service, model, project_id,
        'Modify this project: add a status LED, update the firmware, and compile it.',
    )

    assert all(bound == agent._ASSESSMENT_TOOLS for bound in model.bound)
    assert not any(event['type'] == 'questions' for event in events)
    assert [name for name, _ in calls] == ['read_project']
    prompt = '\n'.join(message.content for message in model.histories[0] if message.type == 'system')
    assert 'modification of an existing project' in prompt


def test_vague_existing_firmware_change_asks_focused_clarification(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler())
    project_id = seed_oled(service)
    before = service.get_project(project_id)
    model = Model([call('read_project'), call('read_firmware'),
                   AIMessageChunk(content='What should the OLED display, and at what update interval?')])

    events, _ = collect(service, model, project_id, 'Change the firmware.')

    assert events[-1]['status'] == 'success'
    assert service.get_project(project_id) == before
    assert not any(event['type'] == 'questions' for event in events)
    assert any(event.get('channel') == 'answer' and 'What should' in event['text'] for event in events)
    assert model.choices[-1] == 'auto'


def test_retrying_converged_firmware_does_not_create_duplicate_revision(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler())
    project_id = seed_oled(service)
    asyncio.run(service.command(project_id, 'edit_firmware', {'source': UPDATED}))
    before = service.get_project(project_id)
    model = Model([
        call('read_project'), call('read_firmware'),
        call('edit_firmware', {'source': UPDATED}), call('compile_firmware'),
        AIMessageChunk(content='The requested source was already saved; compilation succeeded.'),
    ])

    events, _ = collect(service, model, project_id, 'Update the firmware to show Hello! WireUp every 140 ms.')
    after = service.get_project(project_id)

    assert events[-1]['status'] == 'success'
    assert after['revision'] == before['revision']
    assert after['firmware']['revision'] == before['firmware']['revision']
    assert events[-1]['changes']['firmwareWrites'] == 0
    assert [part['id'] for part in after['components']].count('oled') == 1
    assert len(after['wires']) == len(before['wires'])


def test_generation_failure_preserves_working_source(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler())
    project_id = seed_oled(service)
    before = service.get_project(project_id)
    model = Model([
        call('read_project'), call('read_firmware'),
        call('edit_firmware', {'old': 'missing source segment', 'new': 'replacement'}),
        AIMessageChunk(content='The exact source segment was not found. Reload and retry with a full-source patch.'),
    ])

    events, _ = collect(service, model, project_id, 'Update the firmware to show Hello! WireUp every 140 ms.')

    assert events[-1]['status'] == 'error'
    assert service.get_project(project_id)['firmware'] == before['firmware']
    assert 'match exactly one' in json.dumps(events)


def test_compile_failure_returns_diagnostics_without_claiming_compiled(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler(success=False))
    project_id = seed_oled(service)
    model = Model([
        call('read_project'), call('read_firmware'),
        call('edit_firmware', {'source': UPDATED}), call('compile_firmware'),
        call('read_compiler_errors'),
        AIMessageChunk(content='The source was saved, but compilation failed because the OLED library is unavailable.'),
    ])

    events, _ = collect(service, model, project_id, 'Update the firmware to show Hello! WireUp every 140 ms.')

    assert events[-1]['status'] == 'error'
    assert service.get_project(project_id)['firmware']['source'] == UPDATED
    assert 'Adafruit_SSD1306.h' in json.dumps(events)
    assert not any(event.get('channel') == 'answer' for event in events)


def test_confirmed_requirements_are_saved_without_creating_design_revision(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler())
    project = service.create_project(board='arduino-uno')
    before = service.get_project(project['id'])

    saved = service.confirm_requirements(project['id'], {
        'behavior': 'show status', 'display_address': '0x3C',
    })
    after = service.get_project(project['id'])

    assert saved['status'] == 'confirmed'
    assert after['requirements']['answers']['display_address'] == '0x3C'
    assert after['revision'] == before['revision']
    assert after['firmware']['revision'] == before['firmware']['revision']


def test_stale_revision_error_is_structured_and_actionable(tmp_path):
    service = HardwareService(tmp_path, compiler=Compiler())
    project = service.create_project(board='arduino-uno')
    asyncio.run(service.command(project['id'], 'edit_firmware', {
        'source': ORIGINAL, 'expected_revision': project['revision'],
    }))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(service.command(project['id'], 'edit_firmware', {
            'source': UPDATED, 'expected_revision': project['revision'],
        }))

    detail = caught.value.detail
    assert detail['code'] == 'stale_project_revision'
    assert detail['actual_revision'] == project['revision'] + 1
    assert 'Reload' in detail['recovery']
