"""Deterministic project instructions and independently versioned build progress."""

import copy
import hashlib
import json
import re

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


SCHEMA_VERSION = 1
GENERATOR_VERSION = 2
SIMULATION_SCOPE = 'Automatic simulation behavior verification is unavailable; browser acknowledgements and runtime availability are not proof of circuit behavior.'
COMPILE_SCOPE = 'Checks only the saved successful compiler result and its actual current artifact; does not compile, upload, or prove behavior.'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


def describe(service, project):
    components = sorted(project['components'], key=lambda part: part['id'])
    inventory = [{key: copy.deepcopy(part.get(key, {})) for key in ('id', 'type', 'properties')} for part in components]
    wires = sorted(project['wires'], key=lambda wire: wire.get('id', ''))
    connections = [{key: copy.deepcopy(wire.get(key)) for key in ('id', 'from', 'to', 'color')} for wire in wires]
    topology = {'board': project['board'], 'components': inventory,
                'wires': [{key: wire[key] for key in ('id', 'from', 'to')} for wire in connections]}
    firmware = project['firmware']
    source = firmware['source']
    source_dependency = {'board': project['board'], 'source': source, 'filename': firmware['filename']}
    component_labels = []
    for part in inventory:
        name = service.catalog.components.get(part['type'], {}).get('name', part['type'])
        properties = json.dumps(part['properties'], sort_keys=True, ensure_ascii=False)
        component_labels.append(f"Gather {part['id']}: {name} ({part['type']}), saved properties {properties}.")
    wiring = []
    power, signals = [], []
    pins = []
    for wire in connections:
        start, end = wire['from'], wire['to']
        text = (f"Connect wire {wire['id']}: {start['component']}.{start['pin']} to "
                f"{end['component']}.{end['pin']} (saved color {wire['color']}).")
        wiring.append(text)
        pins.extend([start, end])
        rail = any(re.fullmatch(r'GND(?:\.\d+)?|3V3(?:\.\d+)?|3\.3V|5V(?:\.\d+)?|VIN|VCC|VDD|VSS|V\+|VBUS|VSYS', endpoint['pin']) for endpoint in (start, end))
        (power if rail else signals).append(text)
    source_notes = [f"Review the actual saved {firmware['filename']} ({len(source.encode('utf-8'))} UTF-8 bytes), firmware revision {firmware['revision']}."]
    if firmware['filename'].endswith('.ino'):
        source_notes.extend([
            'For Arduino code, setup() is the initialization entry point and loop() is repeatedly called. Locate these functions in the saved source; their presence or operation has not been validated here.',
            'Read pinMode calls to understand intended input/output modes, and digitalRead/digitalWrite calls to find inputs and outputs. Trace named constants and library calls to their definitions rather than guessing their numeric pins.',
            'Review delay calls and timing variables to understand pauses, and Serial.begin/print calls for intended serial output. These descriptions explain APIs, not a verified execution trace of your program.',
            'Review include directives and library requirements for this exact board before compiling.',
        ])
    else:
        source_notes.append('This source format is not automatically explained. Use the language and board documentation to review its entry points, dependencies, inputs, and outputs.')
    source_notes.append('Read the source itself to understand pin assignments, timing, and expected behavior; this guide does not infer intent or guarantee source-to-wire compatibility.')
    safety = 'Disconnect physical power before wiring; verify real part pinouts, polarity, supply voltage, and current limits against datasheets. Saved pin labels are not a physical pin-number guarantee.'
    from backend.hardware import BOARD_CONFIG
    board = BOARD_CONFIG.get(project['board'], {})
    capability = [
        f"Compilation support for {project['board']}: " + ('listed as supported.' if board.get('compile') is True else 'unavailable or not confirmed.'),
        f"Simulation catalog status: {board.get('simulation', 'not confirmed')}.",
    ]
    if board.get('simulation_scope'):
        capability.append('Declared simulation scope: ' + board['simulation_scope'])
    if board.get('unavailable_reason'):
        capability.append(board['unavailable_reason'])
    support = [{'type': part['type'], 'pins': service.catalog.components.get(part['type'], {}).get('pins', []),
                'scope': service.catalog.components.get(part['type'], {}).get('simulation_scope'),
                'supported': service.catalog.components.get(part['type'], {}).get('simulation_supported')}
               for part in inventory]
    stages = [
        ('introduction', 'Project introduction and expected behavior', 'Understand the saved project and decide what behavior you will check.',
         [f"Project: {project['name']}. Board target: {project['board']}. The saved design has {len(inventory)} components and {len(connections)} wires.",
          'Read the saved source and identify the inputs, outputs, and changes you expect to observe. The project definitions alone do not establish the intended behavior.',
          'Generated instructions are not validated build results. Confirm your expected behavior from the source or your project requirements before assembly.'],
         'A stated observation goal lets you distinguish a working build from a program that merely compiles.', ['expectations'], {'topology': topology, 'firmware': source_dependency}),
        ('preparation', 'Component preparation', 'Collect and identify the exact saved parts before connecting them.',
         component_labels + ['Disconnect USB and any external supply. Match each physical model and revision with its datasheet, identify orientation and labeled pins, and confirm supply and logic-level compatibility.'] if inventory else ['No components are defined. Select a board and add the intended parts; do not invent missing inventory.'],
         'Matching references, models, configuration, and electrical ratings avoids incompatible substitutions.', ['inventory', 'power_off'], {'board': project['board'], 'components': inventory}),
        ('wiring', 'Wiring instructions', 'Reproduce every saved connection, one wire at a time, with power off.',
         ['Locate each component reference and its exact named pin in Wiring. Compare the name with physical documentation; simulator coordinates do not certify real header numbering.'] + wiring + (['No wires are saved. Review whether this is intentional; do not infer extra connections.'] if not wiring else []),
         'Following the actual endpoints preserves the project topology and makes each connection traceable.', ['wiring_review'], topology),
        ('verification', 'Connection verification', 'Check each connection and review limited automatic diagnostics before power is applied.',
         ['With physical power disconnected, compare both ends of every wire with the Wiring page. Check for loose leads, unintended contacts, reversed polarity, and orientation errors.',
          'Use Run connection checks and read every finding and the stated scope. The checker validates known endpoints and limited topologies; a pass is not electrical safety certification.',
          'If a warning or unsupported part is reported, consult its datasheet and resolve the uncertainty manually before powering.'],
         'A wire list and a software topology pass cannot replace inspection of a physical assembly.', ['inspection', 'findings'], topology),
        ('firmware', 'Firmware explanation', 'Review the actual saved source, target, dependencies, and pin usage.', source_notes,
         'Source review connects your intended behavior to code without treating comments or constants as validated wiring.', ['source'], source_dependency),
        ('simulation', 'Compilation and simulation', 'Compile for the actual target and observe only supported simulator behavior.',
         capability + ['Open Code and explicitly save any draft first. Use Compile firmware and review errors before proceeding; compilation does not upload or execute a physical build.',
          'Use Verify compilation evidence to inspect a real current compiler artifact. A missing, failed, stale, or mismatched artifact cannot pass.',
          'If Run simulation is enabled, start it explicitly and observe serial and pin output. Stop it before editing. Starting successfully does not verify intended behavior.',
          'Review runtime limitations and unsupported peripherals. Automatic simulation behavior verification is unavailable.'],
         'Real compiler evidence and limited simulator observations answer different questions from physical safety or functional success.', ['compile', 'runtime_limits'], {'topology': topology, 'firmware': source_dependency, 'support': support, 'board_support': board}),
        ('testing', 'Testing and troubleshooting', 'Compare actual observations with intended behavior and investigate failures safely.',
         ['State the input or condition being tested, the output you expect from your saved source, and the output actually observed. Repeat for initial conditions and changed inputs.',
          'Use Wiring to interact with inputs only when their runtime supports them. Review unsupported component warnings; do not assume every saved part is emulated.',
          'If compilation fails, read the first diagnostic and check the board target, source, and dependencies. If there is no output, check source logic and exact saved endpoints, then review simulator scope.',
          'For physical faults, disconnect power before changing wires. Recheck supply ratings, polarity, orientation, and connections against datasheets. Stop if a part overheats or supply safety is uncertain.',
          'Confirm only observations you actually made. Unsupported or untested cases remain unverified.'],
         'Testing actual cases exposes behavior failures that compilation and limited topology checks cannot prove absent.', ['observed', 'troubleshooting'], {'topology': topology, 'firmware': source_dependency, 'support': support}),
        ('completion', 'Completion summary', 'Review confirmations, outstanding work, outdated steps, and evidence scope.',
         ['Review the progress summary and return to outstanding or outdated checklist items. Review each automatic result and its limitations separately from your confirmations.',
          'Save your place and return later from this project’s Build Guide. Circuit or firmware changes can require new review.',
          'Completing this checklist means you confirmed the listed work; it is not electrical certification or a guarantee of physical or simulated behavior.'],
         'A clear completion record prevents unchecked assumptions from being presented as validated results.', ['summary'], {'topology': topology, 'firmware': source_dependency, 'support': support}),
    ]
    labels = {'expectations': 'I reviewed the saved project and identified the behavior I intend to check.',
              'inventory': 'I checked the exact saved parts and configuration.', 'power_off': 'I disconnected physical power and checked supply and logic compatibility.',
              'wiring_review': 'I reviewed the absence of saved wires without inventing connections.',
              'inspection': 'I inspected the actual wiring, orientation, and polarity with physical power off.', 'findings': 'I reviewed limited connection findings and unresolved warnings.',
              'source': 'I reviewed the actual saved source and its intended pin usage.', 'compile': 'I reviewed compiler results; this is my confirmation, not an automatic compiler pass.',
              'runtime_limits': 'I reviewed simulation compatibility, limitations, and available observations.',
              'observed': 'I manually observed and compared the intended behavior.', 'troubleshooting': 'I reviewed failed or untested cases and safe troubleshooting.',
              'summary': 'I reviewed outstanding work, outdated items, and evidence limitations.'}
    steps = []
    for stage, title, objective, instructions, why, checks, dependency in stages:
        checklist = [{'id': f'{stage}.{check}', 'label': labels[check]} for check in checks]
        if stage == 'wiring' and connections:
            checklist = [{'id': f"wiring.wire.{wire['id']}",
                          'label': f"I checked wire {wire['id']}: {wire['from']['component']}.{wire['from']['pin']} to {wire['to']['component']}.{wire['to']['pin']}."}
                         for wire in connections]
        steps.append({'id': stage, 'stage': stage, 'title': title, 'objective': objective,
                      'instructions': instructions, 'why': why, 'components': copy.deepcopy(inventory),
                      'pins': copy.deepcopy(pins) if stage in ('wiring', 'verification', 'firmware', 'testing') else [],
                      'warnings': [safety] + (['Select a board before compiling or testing.'] if project['board'] == 'unselected' else []),
                      'checklist': checklist,
                      'dependency_fingerprint': fingerprint({'generator': GENERATOR_VERSION, 'dependency': dependency})})
    from backend.build_guide_content import enhance_static
    return enhance_static(service, project, steps), topology, source_dependency


def evidence_dependencies(project, topology, source):
    return {'topology': fingerprint(topology),
            'compile': fingerprint({'source': source, 'project_revision': project['revision'],
                                    'firmware_revision': project['firmware']['revision'],
                                    'compiler': project.get('compiler'), 'artifacts': project.get('_artifacts', {})}),
            'simulation': fingerprint({'topology': topology, 'source': source})}


def synchronize(service, project):
    from backend.circuit_wiring import SCOPE
    steps, topology, source = describe(service, project)
    dependencies = evidence_dependencies(project, topology, source)
    for step in steps:
        if step['id'] in ('simulation', 'testing', 'completion'):
            step['dependency_fingerprint'] = fingerprint({'step': step['dependency_fingerprint'], 'compile': dependencies['compile']})
    saved = project.get('_build_guide')
    if saved is None:
        progress = {'revision': 1, 'current_step': steps[0]['id'],
                    'checks': {check['id']: {'confirmed': False, 'outdated': False} for step in steps for check in step['checklist']},
                    'outdated_steps': {}, 'evidence': {name: {'status': 'not_checked', 'scope': scope, 'details': {}, 'checked_at': None,
                                                          'dependency_fingerprint': dependencies[name]}
                        for name, scope in [('topology', SCOPE), ('compile', COMPILE_SCOPE), ('simulation', SIMULATION_SCOPE)]}}
        saved = {'schema_version': SCHEMA_VERSION, 'fingerprints': {}, 'progress': progress}
    else:
        saved = copy.deepcopy(saved)
    before = copy.deepcopy(saved)
    progress = saved['progress']
    step_ids = {step['id'] for step in steps}
    check_ids = {check['id'] for step in steps for check in step['checklist']}
    progress['checks'] = {key: value for key, value in progress['checks'].items() if key in check_ids}
    progress['outdated_steps'] = {key: value for key, value in progress['outdated_steps'].items() if key in step_ids}
    saved['fingerprints'] = {key: value for key, value in saved['fingerprints'].items() if key in step_ids}
    if progress['current_step'] not in step_ids:
        progress['current_step'] = steps[0]['id']
    for step in steps:
        step_id = step['id']
        previous = saved['fingerprints'].get(step_id)
        for check in step['checklist']:
            progress['checks'].setdefault(check['id'], {'confirmed': False, 'outdated': False})
        if previous is not None and previous != step['dependency_fingerprint']:
            progress['outdated_steps'][step_id] = 'Saved project dependencies changed; review this step again.'
            for check in step['checklist']:
                state = progress['checks'][check['id']]
                if state['confirmed'] or state['outdated']:
                    state.update(confirmed=False, outdated=True)
        saved['fingerprints'][step_id] = step['dependency_fingerprint']
    for name, dependency in dependencies.items():
        evidence = progress['evidence'][name]
        if evidence['dependency_fingerprint'] != dependency:
            if evidence['status'] != 'not_checked':
                evidence['status'] = 'outdated'
                evidence['outdated_reason'] = 'Saved verification dependencies changed; verify again.'
            evidence['dependency_fingerprint'] = dependency
    version = fingerprint({'schema_version': SCHEMA_VERSION, 'generator_version': GENERATOR_VERSION, 'steps': steps})
    saved['version'] = version
    if project.get('_build_guide') is not None and before != saved:
        progress['revision'] += 1
    if project.get('_build_guide') != saved:
        project['_build_guide'] = saved
        service._save(project)
    from backend.build_guide_content import refresh_dynamic
    guide = {'schema_version': SCHEMA_VERSION, 'version': version, 'project_id': project['id'],
             'project_revision': project['revision'], 'firmware_revision': project['firmware']['revision'],
             'steps': steps, 'progress': copy.deepcopy(progress)}
    return refresh_dynamic(service, project, guide, current_compile(project))


class ProgressRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    runtime_token: str = Field(max_length=200)
    version: str = Field(min_length=1, max_length=100)
    expected_progress_revision: StrictInt = Field(ge=1)
    current_step: str | None = Field(default=None, max_length=80)
    checks: dict[str, StrictBool] = Field(default_factory=dict, max_length=100)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    runtime_token: str = Field(max_length=200)
    version: str = Field(min_length=1, max_length=100)
    expected_progress_revision: StrictInt = Field(ge=1)
    checks: list[str] = Field(default_factory=lambda: ['topology', 'compile', 'simulation'], min_length=1, max_length=3)


def check_version(guide, payload):
    from backend.hardware import fail
    if payload.version != guide['version'] or payload.expected_progress_revision != guide['progress']['revision']:
        fail(409, 'Build guide version or progress revision conflict; read the current build guide.')


def current_compile(project):
    from backend.hardware import BOARD_CONFIG
    result = project.get('compiler') or {}
    artifact = result.get('artifact') or {}
    stored = project.get('_artifacts', {}).get(artifact.get('id'))
    board = BOARD_CONFIG.get(project['board'], {})
    payload_key = board.get('format')
    valid = (board.get('compile') is True and result.get('status') in ('compilation_complete', 'simulation_ready')
             and not result.get('stale') and not result.get('errors')
             and result.get('project_revision') == project['revision']
             and result.get('source_revision') == project['firmware']['revision']
             and result.get('source') == project['firmware']['source']
             and bool(artifact) and artifact == stored
             and artifact.get('board') == project['board'] and artifact.get('format') == payload_key
             and artifact.get('source_revision') == project['firmware']['revision']
             and artifact.get('project_revision') == project['revision']
             and artifact.get('source') == project['firmware']['source']
             and isinstance(artifact.get(payload_key), str) and bool(artifact.get(payload_key)))
    return {'status': 'passed' if valid else 'failed', 'scope': COMPILE_SCOPE,
            'details': {'message': 'Current successful saved artifact verified.' if valid else 'No actual current successful artifact matches this project and firmware.',
                        'compiler_status': result.get('status', 'not_compiled'), 'artifact_id': artifact.get('id')},
            'artifact_id': artifact.get('id'), 'project_revision': project['revision'],
            'firmware_revision': project['firmware']['revision']}


def create_router(service):
    from backend.hardware import fail, now
    api = APIRouter()

    @api.get('/projects/{project_id}/build-guide')
    async def get_guide(project_id: str):
        with service.lock:
            return synchronize(service, service._load(project_id))

    @api.patch('/projects/{project_id}/build-guide/progress')
    async def patch_progress(project_id: str, payload: ProgressRequest):
        with service.lock:
            project = service._load(project_id)
            service.authorize_runtime(project, payload.runtime_token)
            guide = synchronize(service, project)
            check_version(guide, payload)
            if payload.current_step is not None and payload.current_step not in {step['id'] for step in guide['steps']}:
                fail(400, 'Unknown build guide step.')
            progress = project['_build_guide']['progress']
            if any(check not in progress['checks'] for check in payload.checks):
                fail(400, 'Unknown build guide checklist ID; automatic statuses cannot be set by clients.')
            before = copy.deepcopy(progress)
            if payload.current_step is not None:
                progress['current_step'] = payload.current_step
            for check, confirmed in payload.checks.items():
                state = progress['checks'][check]
                if state['confirmed'] != confirmed or state['outdated']:
                    progress['checks'][check] = {'confirmed': confirmed, 'outdated': False}
                    if confirmed:
                        progress['checks'][check]['confirmed_at'] = now()
            for step in guide['steps']:
                if all(progress['checks'][check['id']]['confirmed'] and not progress['checks'][check['id']]['outdated'] for check in step['checklist']):
                    progress['outdated_steps'].pop(step['id'], None)
            if progress != before:
                progress['revision'] += 1
                service._save(project)
            return synchronize(service, project)

    @api.post('/projects/{project_id}/build-guide/verify')
    async def verify(project_id: str, payload: VerifyRequest):
        from backend.circuit_wiring import _validate
        with service.lock:
            project = service._load(project_id)
            service.authorize_runtime(project, payload.runtime_token)
            guide = synchronize(service, project)
            check_version(guide, payload)
            if len(set(payload.checks)) != len(payload.checks) or any(name not in ('topology', 'compile', 'simulation') for name in payload.checks):
                fail(400, 'Verification checks must be unique topology, compile, or simulation names.')
            progress = project['_build_guide']['progress']
            for name in payload.checks:
                if name == 'topology':
                    validation = _validate(service, project)
                    evidence = {'status': 'passed' if validation['valid'] else 'failed', 'scope': validation['scope'], 'details': validation}
                elif name == 'compile':
                    evidence = current_compile(project)
                else:
                    evidence = {'status': 'unavailable', 'scope': SIMULATION_SCOPE,
                                'details': {'message': 'Observe and confirm behavior manually in the workspace; no automatic behavior verifier is available.'}}
                evidence.update(checked_at=now(), dependency_fingerprint=progress['evidence'][name]['dependency_fingerprint'])
                progress['evidence'][name] = evidence
            progress['revision'] += 1
            service._save(project)
            return synchronize(service, project)

    return api
