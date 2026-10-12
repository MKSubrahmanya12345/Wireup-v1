import asyncio
import copy
import json
from pathlib import Path

import pytest

from backend.build_guide import GENERATOR_VERSION, current_compile
from backend.build_guide_content import compiler_sections, compiler_state, human_names, source_facts, wire_action
from backend.tests.test_build_guide import client, get, request, service, url


@pytest.fixture
def button(service):
    # Copy the existing user project into isolated storage; never mutate its file.
    path = Path(__file__).resolve().parents[1] / 'data/hardware/14b148218b524bd3886696b661e73954.json'
    saved = copy.deepcopy(json.loads(path.read_text(encoding='utf-8')))
    saved.pop('_build_guide', None)
    service._save(saved)
    return saved


def text(step):
    return '\n'.join(step['instructions'] + [item['title'] for item in step['sections']] + [value for item in step['sections'] for key in ('paragraphs', 'actions') for value in item[key]])


def stage(guide, name):
    return next(step for step in guide['steps'] if step['id'] == name)


def test_actual_button_control_copy_has_grounded_eight_stage_tutoring(service, client, button):
    original_path = Path(__file__).resolve().parents[1] / 'data/hardware/14b148218b524bd3886696b661e73954.json'
    original = original_path.read_bytes()
    guide = get(client, button)
    assert guide['project_revision'] == 34
    assert len(guide['steps']) == 8
    assert len(guide['progress']['checks']) == 16
    for step in guide['steps']:
        assert all(set(item) == {'title', 'paragraphs', 'actions'} for item in step['sections'])
        assert all(isinstance(value, str) for item in step['sections'] for key in ('paragraphs', 'actions') for value in item[key])
        assert 1 <= len(step['sections']) <= 3
        assert {item['title'] for item in step['sections']} <= {"What you'll do", 'Steps', 'Check your work', "What's next"}
        actions = step['instructions'] or [action for item in step['sections'] if item['title'] == 'Steps' for action in item['actions']]
        assert 2 <= len(actions) <= 5
        assert step['pins'] == []
        assert all(isinstance(component, str) for component in step['components'])
        visible = text(step) + ' '.join(check['display_label'] for check in step['checklist'])
        for internal in ('button-1', 'led-1', 'r-led', 'wire-button', 'wire-led', 'arduino-uno'):
            assert internal not in visible
    intro = text(stage(guide, 'introduction'))
    assert 'Press Button to light LED; release it to turn the LED off' in intro
    assert len(intro.split()) < 70
    preparation = text(stage(guide, 'preparation'))
    for value in ('1 × Arduino Uno', '1 × Button', '1 × Resistor', '1 × LED', 'Saved value: 220', 'not saved inventory', 'A is anode', 'Input switch'):
        assert value in preparation
    wiring = stage(guide, 'wiring')
    assert not wiring['instructions']
    actions = [value for item in wiring['sections'] if item['title'] == 'Steps' for value in item['actions']]
    assert len(actions) == 5
    names, _ = human_names(service, button)
    assert set(actions) == {wire_action(wire, names) for wire in button['wires']}
    assert 'After each path' in text(wiring)
    assert 'Button path' in text(wiring) and 'LED path' in text(wiring)
    firmware = text(stage(guide, 'firmware'))
    for value in ('BUTTON_PIN = 2', 'LED_PIN = 13', 'INPUT_PULLUP', 'OUTPUT', 'released Button HIGH', 'pressed is LOW', 'active-low', 'no delay, serial output, or debounce logic'):
        assert value in firmware
    assert 'sketch.ino:L' not in firmware
    testing = text(stage(guide, 'testing'))
    assert 'Release Button: expect LED off' in testing
    assert 'Press and hold Button: expect LED on' in testing
    assert 'Released: input pin 2 HIGH, output pin 13 LOW' in testing
    assert 'Pressed: input LOW, output HIGH' in testing
    compiler = text(stage(guide, 'simulation'))
    assert 'Compiler: Outdated' in compiler
    assert 'project revision 10; the current revision is 34' in compiler
    assert 'Sketch uses' not in compiler and 'stdout:' not in compiler
    assert not stage(guide, 'completion')['components']
    assert original_path.read_bytes() == original


@pytest.mark.parametrize('source', [
    '// digitalWrite(13, HIGH);\nvoid setup() {} void loop() {}',
    '/* pinMode(2, INPUT_PULLUP); digitalRead(2); */ void setup() {} void loop() {}',
    'void setup() {} void loop() { Serial.print("digitalWrite(13, HIGH);"); }',
    'void setup() {} void loop() { Serial.print(R"msg(digitalWrite(13, HIGH);)msg"); }',
    'void digitalWrite(int a, int b) {} void setup() {} void loop() {}',
    'const int LED_PIN = 13; void setup() {} void loop() { if (false) { digitalWrite(LED_PIN, HIGH); } }',
    '#if 0\nvoid setup() {} void loop() { digitalWrite(13, HIGH); }\n#endif',
    'print("digitalRead(2);")',
    'void setup() {} void loop() { Serial.print("unfinished digitalWrite(13, HIGH);',
    '// continued comment \\\n digitalWrite(13, HIGH);\nvoid setup() {} void loop() {}',
])
def test_unsupported_source_never_infers_flow_or_comment_string_calls(source):
    facts = source_facts({'filename': 'sketch.ino', 'source': source})
    assert facts['pattern'] is None
    if 'if (false)' in source or '#if' in source:
        assert facts['excerpts']
        assert all('not guaranteed execution flow' in entry for entry in facts['excerpts'])
    else:
        assert not facts['excerpts']


@pytest.mark.parametrize('change', ['extra_statement', 'extra_function', 'preprocessor', 'same_pin', 'shadowed_state', 'alternate_branch', 'unclosed_comment', 'unsupported_filename'])
def test_ambiguous_button_source_does_not_match_verified_pattern(button, change):
    firmware = copy.deepcopy(button['firmware'])
    if change == 'extra_statement':
        firmware['source'] = firmware['source'].replace('int buttonState', 'digitalWrite(LED_PIN, LOW); int buttonState')
    elif change == 'extra_function':
        firmware['source'] += '\nvoid digitalWrite(int pin, int level) {}'
    elif change == 'preprocessor':
        firmware['source'] = '#define LOW HIGH\n' + firmware['source']
    elif change == 'same_pin':
        firmware['source'] = firmware['source'].replace('LED_PIN = 13', 'LED_PIN = 2')
    elif change == 'shadowed_state':
        firmware['source'] = firmware['source'].replace('buttonState', 'BUTTON_PIN')
    elif change == 'alternate_branch':
        firmware['source'] = firmware['source'].replace('buttonState == LOW', 'buttonState == HIGH')
    elif change == 'unclosed_comment':
        firmware['source'] += '\n/* unfinished'
    else:
        firmware['filename'] = 'main.py'
    assert source_facts(firmware)['pattern'] is None


def test_missing_matching_circuit_keeps_sketch_intent_separate_from_hardware(service, client, button):
    button['wires'][0]['from']['pin'] = '3'
    service._save(button)
    guide = get(client, button)
    assert 'do not match a unique supported path' in text(stage(guide, 'firmware'))
    assert 'expect LED' not in text(stage(guide, 'testing'))
    assert 'purpose of unverified connections is unknown' in text(stage(guide, 'wiring'))


def test_unknown_objective_for_nonmatching_source(service, client):
    project = service.create_project('Press button for light', board='arduino-uno')
    asyncio.run(service.command(project['id'], 'edit_firmware', {'source': '// Press button to light LED\nvoid setup() {} void loop() {}'}))
    guide = get(client, project)
    introduction = text(stage(guide, 'introduction'))
    assert 'project title alone does not tell us what it should do' in introduction
    assert 'No objective is saved' in introduction
    assert 'Behavior is unknown' in ' '.join(stage(guide, 'introduction')['warnings'])
    assert 'expect LED' not in text(stage(guide, 'testing'))


def test_progress_adaptive_summary_refreshes_patch_and_verify_without_version_churn(service, client, button):
    guide = get(client, button)
    assert '0 of 16 tasks confirmed by you' in text(stage(guide, 'completion'))
    response = client.patch(url(button) + '/progress', json=request(button, guide, current_step='completion', checks={'introduction.expectations': True}))
    assert response.status_code == 200
    updated = response.json()
    summary = text(stage(updated, 'completion'))
    assert '1 of 16 tasks confirmed by you' in summary
    assert 'Next stage: Component preparation' in summary
    assert 'Not tested here' in summary and 'no saved behavior results' in summary
    assert stage(updated, 'completion')['instructions'] == stage(updated, 'preparation')['instructions'][:3]
    assert len(stage(updated, 'completion')['sections']) == 3
    assert updated['version'] == guide['version']
    assert updated == get(client, button)
    verified = client.post(url(button) + '/verify', json=request(button, updated, checks=['compile', 'simulation'])).json()
    assert verified['progress']['evidence']['compile']['status'] == 'failed'
    assert verified['progress']['evidence']['simulation']['status'] == 'unavailable'
    assert 'Compiler: Outdated' in text(stage(verified, 'completion'))
    assert 'Not tested here' in text(stage(verified, 'completion'))
    assert verified['version'] == updated['version']
    assert verified == get(client, button)
    manual = client.patch(url(button) + '/progress', json=request(button, verified, checks={'testing.observed': True})).json()
    assert 'Manually confirmed task' in text(stage(manual, 'completion'))
    assert 'no saved behavior results' in text(stage(manual, 'completion'))
    assert manual['version'] == guide['version']


def test_connection_failure_prioritized_then_outdated_review(service, client, button):
    asyncio.run(service.command(button['id'], 'connect_wire', {'id': 'short', 'from': {'component': 'board', 'pin': '5V'}, 'to': {'component': 'board', 'pin': 'GND.1'}}))
    guide = get(client, button)
    verified = client.post(url(button) + '/verify', json=request(button, guide, checks=['topology'])).json()
    summary = text(stage(verified, 'completion'))
    assert 'Next stage: Connection verification' in summary
    assert 'Resolve failed connection findings first' in summary and 'Keep physical power disconnected' in summary
    assert 'power_short' not in summary and 'Evidence:' not in summary
    assert 'Different power or ground rails are connected together' in text(stage(verified, 'verification'))
    assert 'Arduino Uno pin 5V' in text(stage(verified, 'verification'))
    assert verified == get(client, button)
    asyncio.run(service.command(button['id'], 'remove_wire', {'id': 'short'}))
    outdated = get(client, button)
    assert 'Connection checks still need a current result' in text(stage(outdated, 'completion'))
    assert 'Connections: Outdated' in text(stage(outdated, 'verification'))
    assert 'Next stage: Project introduction' in text(stage(outdated, 'completion'))


@pytest.mark.parametrize('condition,expected', [('notrun', 'Not tested'), ('success', 'Current build passed'), ('stale', 'Outdated'), ('failed', 'Failed'), ('unsupported', 'Unsupported')])
def test_compiler_diagnostics_and_safe_actions(service, condition, expected):
    project = service.create_project(board='raspberry-pi-3' if condition == 'unsupported' else 'arduino-uno')
    if condition in ('success', 'stale', 'failed'):
        asyncio.run(service.command(project['id'], 'compile_firmware', {}))
    saved = service._load(project['id'])
    if condition == 'stale':
        saved['compiler']['stale'] = True
    if condition == 'failed':
        saved['compiler'].update(status='error', errors=["sketch.ino:4: error: 'LED_PIN' was not declared in this scope"], stderr='fatal error: Missing.h: No such file or directory\nerror: expected ; before }')
    item = compiler_sections(saved, current_compile(saved))
    content = '\n'.join(item['paragraphs'])
    assert f'Compiler: {expected}' in content
    assert len(item['actions']) == 1
    assert 'stderr:' not in content and 'stdout:' not in content
    if condition == 'failed':
        assert 'unknown name near line 4' in content
        assert 'missing header' not in content
        assert 'Correct the missing name' in item['actions'][0]


def test_static_prose_changes_version_not_semantic_confirmations(service, client, button, monkeypatch):
    import backend.build_guide_content as content
    # Seed the previous generator's unenhanced guide and confirmations.
    enhance = content.enhance_static
    monkeypatch.setattr(content, 'enhance_static', lambda service, project, steps: [dict(step, sections=[]) for step in steps])
    legacy = get(client, button)
    legacy = client.patch(url(button) + '/progress', json=request(button, legacy, checks={key: True for key in legacy['progress']['checks']})).json()
    monkeypatch.setattr(content, 'enhance_static', enhance)
    refreshed = get(client, button)
    assert GENERATOR_VERSION == 2
    assert refreshed['version'] != legacy['version']
    assert all(state['confirmed'] and not state['outdated'] for state in refreshed['progress']['checks'].values())
    assert not refreshed['progress']['outdated_steps']
    assert refreshed == get(client, button)


def test_dynamic_content_does_not_enter_saved_version_or_runtime_claims(service, client, button):
    guide = get(client, button)
    service.runtime.sessions[button['id']] = object()
    connected = get(client, button)
    assert connected['version'] == guide['version']
    assert connected['progress'] == guide['progress']
    assert connected == guide
    assert 'Not tested here' in text(stage(connected, 'testing'))
    assert 'no saved behavior results' in text(stage(connected, 'completion'))
    assert service._load(button['id'])['_build_guide']['version'] == guide['version']


def test_outdated_manual_testing_is_not_a_confirmation(service, client, button):
    guide = get(client, button)
    confirmed = client.patch(url(button) + '/progress', json=request(button, guide, checks={'testing.observed': True})).json()
    assert 'confirmed the task manually' in text(stage(confirmed, 'testing'))
    asyncio.run(service.command(button['id'], 'edit_firmware', {'source': button['firmware']['source'] + '\n// revised'}))
    updated = get(client, button)
    assert updated['progress']['checks']['testing.observed']['outdated']
    assert 'previous confirmation is outdated' in text(stage(updated, 'testing'))
    assert 'Outdated review:' in text(stage(updated, 'completion'))
    assert 'Testing' in text(stage(updated, 'completion'))
    assert 'Manually confirmed task' not in text(stage(updated, 'completion'))


def test_duplicate_part_names_are_deterministic_and_checklist_ids_stay_stable(service, client, button):
    extra = copy.deepcopy(next(part for part in button['components'] if part['type'] == 'led'))
    extra['id'] = 'aaa-hidden-led'
    button['components'].append(extra)
    service._save(button)
    guide = get(client, button)
    names, _ = human_names(service, button)
    assert names[extra['id']] == 'LED 1'
    assert names['led-1'] == 'LED 2'
    assert '2 × LED' in text(stage(guide, 'preparation'))
    assert 'LED 2 pin A' in text(stage(guide, 'wiring'))
    wiring = stage(guide, 'wiring')
    assert {check['id'] for check in wiring['checklist']} == {f'wiring.wire.{wire["id"]}' for wire in button['wires']}
    for check in wiring['checklist']:
        assert 'wire-' not in check['display_label']
        assert 'led-1' not in check['display_label']
        assert check['label'].startswith('I checked wire')
    assert guide == get(client, button)


@pytest.mark.parametrize('compiled,target', [(False, 'Compilation and simulation'), (True, 'Testing')])
def test_all_manual_checks_still_recommend_unverified_build_or_behavior(service, client, button, compiled, target):
    if compiled:
        asyncio.run(service.command(button['id'], 'compile_firmware', {}))
    guide = get(client, button)
    verified = client.post(url(button) + '/verify', json=request(button, guide, checks=['topology'])).json()
    complete = client.patch(url(button) + '/progress', json=request(button, verified, checks={key: True for key in guide['progress']['checks']})).json()
    assert '16 of 16 tasks confirmed by you' in text(stage(complete, 'completion'))
    assert f'Next stage: {target}' in text(stage(complete, 'completion'))
    assert 'no saved behavior results' in text(stage(complete, 'completion'))
    assert 2 <= len(stage(complete, 'completion')['instructions']) <= 3


@pytest.mark.parametrize('error,meaning,action', [
    ('fatal error: Missing.h: No such file or directory', 'missing header or library', 'required library'),
    ('sketch.ino:7: error: expected ; before }', 'syntax issue near line 7', 'preceding punctuation'),
])
def test_first_compiler_error_has_one_plain_explanation_without_raw_dump(service, error, meaning, action):
    project = service.create_project(board='arduino-uno')
    asyncio.run(service.command(project['id'], 'compile_firmware', {}))
    saved = service._load(project['id'])
    saved['compiler'].update(status='error', errors=[error, 'SECOND ERROR SHOULD NOT APPEAR'], stdout='RAW OUTPUT SHOULD NOT APPEAR')
    item = compiler_sections(saved, current_compile(saved))
    assert meaning in ' '.join(item['paragraphs'])
    assert action in item['actions'][0]
    assert len(item['paragraphs']) == 2 and len(item['actions']) == 1
    assert error not in ' '.join(item['paragraphs'])
    assert 'SHOULD NOT APPEAR' not in ' '.join(item['paragraphs'])
    assert compiler_state(saved, current_compile(saved)) == 'failed'
