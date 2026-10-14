import pytest
from langchain_core.messages import AIMessageChunk
from backend.tests.test_targeted_followups import project, Model, call, collect, questions


@pytest.mark.parametrize('requirements', [None, {'board': 'arduino-uno', 'behavior': 'blink'}])
def test_small_addition_preserves_project_and_skips_setup(project, requirements):
    service, identity, _, calls = project
    before = service.get_project(identity)
    plan = {'board': 'arduino-uno', 'behavior': 'Add a second LED without changing the existing circuit', 'parts': [{'type': 'led', 'quantity': 2, 'purpose': 'Preserve the existing LED and add one unconnected LED'}, {'type': 'resistor', 'quantity': 1, 'purpose': 'Keep the existing current limiting resistor'}], 'libraries': [], 'operations': ['add_component']}
    model = Model([call('read_project'), call('assess_project_feasibility', {'plan': plan}), call('add_component', {'id': 'led2', 'type': 'led', 'x': 500, 'y': 200}), AIMessageChunk(content='Added led2. Existing wiring and firmware were preserved; the new LED is not connected.')])
    events = collect(project, model, 'Add one LED to the circuit.', requirements=requirements)
    assert events[-1]['status'] == 'success', [event.get('output') for event in events if event['type'] == 'step_end' and event['status'] == 'error']
    assert not any(event['type'] == 'questions' for event in events)
    after = service.get_project(identity)
    assert after['components'][:-1] == before['components']
    assert after['components'][-1]['id'] == 'led2'
    assert after['wires'] == before['wires']
    assert after['firmware'] == before['firmware']
    assert after['board'] == before['board']
    assert not any('ask_project_questions' in names for names in model.bound)
    assert all('remove_component' not in names and 'edit_firmware' not in names for names in model.bound)


@pytest.mark.parametrize('text', ['Start over and build a different project.', 'Change the board to ESP32.', 'Redesign this as a weather station.'])
def test_major_changes_reopen_setup_before_mutating(project, text):
    service, identity, _, calls = project
    before = service.get_project(identity)
    model = Model([call('ask_project_questions', questions())])
    events = collect(project, model, text, requirements={'board': 'arduino-uno', 'behavior': 'blink'})
    assert any(event['type'] == 'questions' for event in events), events
    assert events[-1]['status'] == 'awaiting_answers'
    assert service.get_project(identity) == before
    assert calls == []


def test_addition_cannot_remove_existing_component(project):
    service, identity, _, calls = project
    before = service.get_project(identity)
    model = Model([call('read_project'), call('remove_component', {'id': 'led1'}), AIMessageChunk(content='Unable to perform that unrelated removal.')])
    events = collect(project, model, 'Add one resistor.')
    assert service.get_project(identity) == before
    assert not any(name == 'remove_component' for name, _ in calls)
    assert any(event['type'] == 'step_end' and event['status'] == 'error' for event in events)
    assert not any(event['type'] == 'questions' for event in events)
