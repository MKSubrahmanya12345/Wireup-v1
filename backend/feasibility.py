"""Capability-backed preflight; not a general electrical safety certification."""
import copy
import json
import re
import time
from uuid import uuid4
from fastapi import HTTPException

TTL = 1800
SIMULATED_PARTS = {'led', 'resistor', 'pushbutton', 'pushbutton-6mm'}
ANALOG_PARTS = {'ground', 'resistor', 'capacitor', 'inductor', 'source-dc-voltage', 'source-dc-current', 'source-sine-voltage', 'source-pulse-voltage', 'source-pwl-voltage', 'source-ac-voltage'}


def _fail(message, code=409):
    raise HTTPException(code, message)


def _report(record):
    return copy.deepcopy({key: value for key, value in record.items() if key not in ('expires',)})


def assess(service, project_id, plan):
    from backend.hardware import BOARD_CONFIG, BOARD_ALIASES
    if not isinstance(plan, dict) or len(json.dumps(plan)) > 20000:
        _fail('Invalid feasibility plan.', 400)
    operations = plan.get('operations', ['add_component'])
    plan = {**plan, 'operations': operations}
    executable = {'add_component','remove_component','modify_component','connect_wire','remove_wire','wire_circuit','generate_firmware','edit_firmware','compile_firmware','run_simulation','stop_simulation','read_simulation_results','read_compiler_errors','validate_circuit','check_connections','read_project','read_firmware','search_components'}
    if not isinstance(operations, list) or not operations or any(not isinstance(operation,str) or operation not in executable for operation in operations):
        _fail('Plan operations must use supported executable tool names, not prose descriptions.', 400)
    parts = plan.get('parts', [])
    if plan.get('requirements_text'):
        requested = str(plan['requirements_text']).lower()
        requested = re.sub(r'\b(?:no|without|not using|do not use|don\'t use)\s+(?:an?\s+)?(?:oled|lcd|display|screen)\b', '', requested)
        proposed_types = [str(part.get('type', '')) for part in parts if isinstance(part, dict)]
        proposed = ' '.join(kind + ' ' + str(service.catalog.components.get(BOARD_ALIASES.get(kind.removeprefix('wokwi-'), kind.removeprefix('wokwi-')), {}).get('name', '')) for kind in proposed_types).lower()
        unresolved = []
        for label, words in {'joystick': ('joystick',), 'servo': ('servo',), 'display': ('oled', 'lcd', 'display'), 'sensor': ('dht', 'ultrasonic', 'temperature sensor')}.items():
            present = any(re.search(r'\b' + re.escape(word) + r'\b', requested) for word in words)
            if label == 'display' and not any(re.search(r'\b' + word + r'\b', requested) for word in ('oled','lcd')):
                present = bool(re.search(r'\b(?:physical|external|i2c|graphical)\s+(?:display|screen)\b|\b(?:add|include|use|attach|connect)\s+(?:an?\s+)?display\b', requested))
            if present and not any(word in proposed for word in words):
                unresolved.append(label)
        if unresolved:
            plan = {**plan, 'unresolved_requirements': unresolved}
    if not isinstance(parts, list) or not 0 <= len(parts) <= 40:
        _fail('Plan must contain at most 40 part types.', 400)
    with service.lock:
        project = service._load(project_id)
        board = BOARD_ALIASES.get(plan.get('board', project['board']), plan.get('board', project['board']))
        board = 'none' if board == 'unselected' else board
        issues = []
        blocked = False
        def issue(code, title, message, reason, alternative=None):
            issues.append({'code': code, 'title': title, 'message': message, 'reason': reason, 'alternative': alternative})
        if board not in ('none', *BOARD_CONFIG):
            blocked = True
            issue('board_unknown', 'Board not supported', 'This board has no verified configuration in WireUp.', 'We cannot guess its pins or compiler target.')
        elif board != 'none' and BOARD_CONFIG[board]['simulation'] != 'browser':
            issue('board_runtime', 'Board cannot be simulated here yet', f'{BOARD_CONFIG[board]["name"]} can be placed, but its active runtime is unavailable.', BOARD_CONFIG[board].get('unavailable_reason', 'The emulator has not been verified.'))
        for missing in plan.get('unresolved_requirements', []):
            blocked = True
            issue('unresolved_'+missing, 'Requested feature is not accounted for', f'The plan has not identified the requested {missing} parts.', 'We must resolve every requested feature before changing your prototype.')
        requested_behavior = (str(plan.get('behavior', '')) + ' ' + str(plan.get('requirements_text', ''))).lower()
        if board == 'pi-pico-w' and any(word in requested_behavior for word in ('wifi', 'wi-fi', 'bluetooth', 'wireless', 'onboard led', 'built-in led')):
            issue('pico_w_radio_runtime', 'Pico W wireless behavior cannot be tested', 'The browser executes the RP2040 CPU and external pins, but not the CYW43 radio or onboard LED.', 'Wireless firmware and its onboard LED need real hardware or a compatible native simulator.')
        normalized = []
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get('type'), str):
                _fail('Every planned part needs a type.', 400)
            kind = part['type'].removeprefix('wokwi-')
            quantity = part.get('quantity', 1)
            if type(quantity) is not int or not 1 <= quantity <= 30:
                _fail('Part quantity must be between 1 and 30.', 400)
            entry = service.catalog.components.get(kind)
            normalized.append({**part, 'type': kind, 'quantity': quantity})
            if not entry:
                blocked = True
                issue(kind+'_unknown', 'Part not found', f'We do not have a verified {kind} part.', 'There is no catalog pin definition to wire safely.')
                continue
            if not entry.get('pins'):
                blocked = True
                issue(kind+'_pins', 'Pins are not verified', f'We can show {entry.get("name", kind)}, but cannot connect it accurately yet.', 'Its connectable pins are missing from the verified catalog.', 'Revise the design to a part with verified pins; do not silently substitute.')
            simulated = kind in (ANALOG_PARTS if board == 'none' else SIMULATED_PARTS) or kind == board or board in entry.get('simulation_boards', []) or (board == 'none' and entry.get('spice_model') in ('resistor','capacitor','inductor') and entry.get('simulation_supported') is True)
            if not simulated:
                issue(kind+'_runtime', 'Behavior is not simulated', f'We can place {entry.get("name",kind)}; the current runtime cannot verify its behavior.', 'A visible component is not the same as an implemented emulator model.')
            if kind == 'servo' or 'motor' in kind:
                issue(kind+'_power', 'Motor power must be checked', 'Use an appropriately rated regulated external supply and a shared ground; do not power a motor from a signal pin.', 'The exact motor current and safe supply need its datasheet; WireUp cannot physically verify them.')
        libraries = plan.get('libraries', [])
        if libraries:
            issue('libraries', 'Dependencies will be checked by compilation', 'This design requests firmware libraries; the compiler will report missing or incompatible dependencies.', 'A library request alone is not an incompatibility and does not require approval.')
        requested_operations = plan.get('operations', [])
        unavailable_simulation = board != 'none' and board in BOARD_CONFIG and BOARD_CONFIG[board]['simulation'] != 'browser'
        unavailable_compilation = board != 'none' and board in BOARD_CONFIG and not BOARD_CONFIG[board]['compile']
        if unavailable_simulation:
            plan = {**plan, 'operations': [operation for operation in requested_operations if operation not in ('run_simulation', 'stop_simulation', 'read_simulation_results')]}
        if unavailable_compilation:
            plan = {**plan, 'operations': [operation for operation in plan.get('operations', []) if operation not in ('compile_firmware', 'read_compiler_errors')]}
            issue('board_compile', 'Compilation is not available for this target', 'Generate board-appropriate source and instructions; run or install them on the actual board.', 'Raspberry Pi Linux applications do not use Arduino firmware compilation.')
        simulation_requested = 'run_simulation' in plan.get('operations', [])
        decision_issues = [item for item in issues if item['code'] not in ('libraries', 'board_compile') and not item['code'].endswith('_power') and (not item['code'].endswith('_runtime') or simulation_requested)]
        if blocked:
            decision_issues = issues
        status = 'blocked' if blocked else 'awaiting_approval' if decision_issues else 'approved'
        choices = [{'id':'revise','label':'Change the design','description':'Return to planning and choose supported parts.'},{'id':'cancel','label':'Cancel without changing the project'}]
        if not blocked:
            choices.insert(0, {'id':'hardware_only','label':'Proceed with the documented limitations','description':'Allow wiring/firmware, but do not claim unsupported simulation or physical testing.'})
        record = {'id': uuid4().hex, 'project_id': project_id, 'revision': project['revision'], 'status': status,
                  'summary': 'This check uses actual catalog pins and runtime support. Review limitations before any project changes.',
                  'issues': decision_issues, 'notices': [item for item in issues if item not in decision_issues], 'choices': choices if status != 'approved' else [], 'plan': {**plan,'board':board,'parts':normalized}, 'expires':time.time()+TTL}
        records = project.setdefault('_feasibility', {})
        def review_key(item):
            reviewed = item['plan']
            quantities = {}
            for part in reviewed.get('parts', []):
                quantities[part['type']] = quantities.get(part['type'], 0) + part.get('quantity', 1)
            return json.dumps({'board': reviewed['board'], 'parts': quantities,
                               'issues': sorted((issue['code'], issue.get('message', ''), issue.get('reason', '')) for issue in item['issues'])}, sort_keys=True)
        for previous in reversed(list(records.values())):
            if (previous['revision'] == project['revision'] and previous['expires'] > time.time()
                    and previous['status'] in ('approved', 'awaiting_approval', 'blocked')
                    and review_key(previous) == review_key(record)):
                previous['plan'] = record['plan']
                previous['notices'] = record['notices']
                service._save(project)
                return _report(previous)
            if (previous['expires'] > time.time() and previous['status'] == 'approved'
                    and previous.get('choice') == 'hardware_only'
                    and review_key(previous) == review_key(record)):
                record['status'] = 'approved'
                record['choice'] = 'hardware_only'
                break
        records[record['id']] = record
        project['_feasibility'] = dict(list(records.items())[-5:])
        service._save(project)
        return _report(record)


def _record(service, project_id, assessment_id):
    project = service._load(project_id)
    record = project.get('_feasibility', {}).get(assessment_id)
    if not record or record['expires'] < time.time():
        _fail('This feasibility review expired. Assess the current project again.')
    if record['revision'] != project['revision']:
        _fail('The project changed after this review. Reassess before proceeding.')
    return project, record


def authorize(service, project_id, approval):
    with service.lock:
        project, record = _record(service, project_id, approval.get('assessment_id'))
        choice = approval.get('choice')
        if choice not in [item['id'] for item in record['choices']]:
            _fail('This option is not permitted by the review.', 400)
        if record['status'] == 'approved' and record.get('choice') == choice:
            return _report(record)
        if record['status'] not in ('awaiting_approval','blocked'):
            _fail('This review has already been answered.')
        record['status'] = 'approved' if choice == 'hardware_only' else choice
        record['choice'] = choice
        service._save(project)
        return _report(record)


def validate_approval(service, project_id, assessment_id):
    with service.lock:
        _, record = _record(service, project_id, assessment_id)
        if record['status'] != 'approved':
            _fail('Complete the feasibility review before changing the project.')
        return _report(record)


def check_operation(service, project_id, assessment_id, name, args):
    report = validate_approval(service, project_id, assessment_id)
    plan = report['plan']
    allowed = {part['type'] for part in plan['parts']} | {plan['board']}
    if name == 'add_component' and args.get('type','').removeprefix('wokwi-') not in allowed:
        _fail('This component is outside the reviewed plan. Reassess and ask permission first.')
    if name in ('modify_component', 'remove_component'):
        project = service.get_project(project_id)
        target = next((part for part in project['components'] if part['id'] == args.get('id')), None)
        if target and target['type'] not in allowed:
            _fail('This existing component is outside the reviewed plan. Reassess before changing it.')
    if name in ('connect_wire', 'wire_circuit'):
        project = service.get_project(project_id)
        proposed = args.get('batch', [args])
        for wire in proposed:
            for side in ('from', 'to'):
                endpoint = wire.get(side, {})
                target = next((part for part in project['components'] if part['id'] == endpoint.get('component')), None)
                if target and target['type'] not in allowed:
                    _fail('This wiring targets a part outside the reviewed plan. Reassess first.')
    if name == 'run_simulation' and any(issue['code'].endswith('_runtime') for issue in report['issues']):
        _fail('The reviewed plan includes unsupported simulation behavior. Do not claim this simulation verifies those parts.')
    return report


def advance(service, project_id, assessment_id, old_revision, new_revision):
    with service.lock:
        project = service._load(project_id)
        record = project.get('_feasibility', {}).get(assessment_id)
        if not record or record['revision'] != old_revision or project['revision'] != new_revision:
            _fail('Review revision no longer matches the project.')
        record['revision'] = new_revision
        service._save(project)
