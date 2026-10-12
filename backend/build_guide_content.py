"""Grounded tutoring text; progress-dependent sections never enter version fingerprints."""

from collections import Counter
import re


TOKEN = re.compile(r'R"(?P<delimiter>[^\s()\\]{0,16})\([\s\S]*?(?:\)(?P=delimiter)"|$)|"(?:\\[\s\S]|[^"\\])*(?:"|$)|\'(?:\\[\s\S]|[^\'\\])*(?:\'|$)|//(?:[^\n]*\\\r?\n)*[^\n]*|/\*[\s\S]*?(?:\*/|$)')
BUTTON = re.compile(
    r'\s*const\s+int\s+(?P<input>[A-Za-z_]\w*)\s*=\s*(?P<input_pin>\d+)\s*;\s*'
    r'const\s+int\s+(?P<output>[A-Za-z_]\w*)\s*=\s*(?P<output_pin>\d+)\s*;\s*'
    r'void\s+setup\s*\(\s*\)\s*\{\s*'
    r'pinMode\s*\(\s*(?P=input)\s*,\s*INPUT_PULLUP\s*\)\s*;\s*'
    r'pinMode\s*\(\s*(?P=output)\s*,\s*OUTPUT\s*\)\s*;\s*\}\s*'
    r'void\s+loop\s*\(\s*\)\s*\{\s*'
    r'int\s+(?P<state>[A-Za-z_]\w*)\s*=\s*digitalRead\s*\(\s*(?P=input)\s*\)\s*;\s*'
    r'if\s*\(\s*(?P=state)\s*==\s*LOW\s*\)\s*\{\s*'
    r'digitalWrite\s*\(\s*(?P=output)\s*,\s*HIGH\s*\)\s*;\s*\}\s*else\s*\{\s*'
    r'digitalWrite\s*\(\s*(?P=output)\s*,\s*LOW\s*\)\s*;\s*\}\s*\}\s*')
API_MEANINGS = {
    'pinMode': 'Arduino pinMode requests a pin mode; INPUT_PULLUP enables the internal pull-up, and OUTPUT selects output mode.',
    'digitalRead': 'Arduino digitalRead reads a digital logic level, HIGH or LOW.',
    'digitalWrite': 'Arduino digitalWrite requests HIGH or LOW; its effect depends on the pin mode and hardware.',
    'analogRead': 'Arduino analogRead samples an analog input; interpretation depends on the board and reference.',
    'analogWrite': 'Arduino analogWrite requests a board-dependent output, often PWM rather than a steady analog voltage.',
    'delay': 'Arduino delay requests a pause in milliseconds; it blocks ordinary sketch execution during that call.',
}


def section(title, paragraphs=(), actions=()):
    return {'title': title, 'paragraphs': list(paragraphs), 'actions': list(actions)}


def source_facts(firmware):
    source = firmware['source']
    safe = True

    def mask(match):
        nonlocal safe
        text = match.group()
        comment = text.startswith(('//', '/*'))
        if text.startswith('/*') and not text.endswith('*/'):
            safe = False
        return ''.join('\n' if char == '\n' else (' ' if comment else '?') for char in text)

    code = TOKEN.sub(mask, source)
    match = BUTTON.fullmatch(code) if safe and firmware['filename'].endswith('.ino') else None
    pattern = match.groupdict() if match else None
    reserved = set(API_MEANINGS) | {'setup', 'loop', 'LOW', 'HIGH', 'INPUT_PULLUP', 'OUTPUT'}
    if pattern and (len({pattern['input'], pattern['output'], pattern['state']}) != 3
                    or {pattern['input'], pattern['output'], pattern['state']} & reserved
                    or pattern['input_pin'] == pattern['output_pin']
                    or not all(re.fullmatch(r'0|[1-9]\d*', pattern[key]) for key in ('input_pin', 'output_pin'))):
        pattern = None
    excerpts = []
    for found in re.finditer(r'(?<![\w.:])(' + '|'.join(API_MEANINGS) + r')\s*\([^(){};?]*\)\s*;', code):
        # A declaration or locally redefined API cannot establish Arduino call semantics.
        name = found[1]
        prefix = code[max(code.rfind(';', 0, found.start()), code.rfind('{', 0, found.start()), code.rfind('}', 0, found.start())) + 1:found.start()]
        if re.search(r'\b(?:void|int|long|bool|float|double|char)\s*$', prefix):
            continue
        line = code.count('\n', 0, found.start()) + 1
        end = code.count('\n', 0, found.end()) + 1
        reference = f'{firmware["filename"]}:L{line}' + (f'–L{end}' if end != line else '')
        excerpts.append(f'{reference}: {" ".join(found.group().split())} {API_MEANINGS[name]} This is a syntax excerpt, not guaranteed execution flow or resolution to the Arduino API.')
    declarations = []
    for found in re.finditer(r'(?m)^\s*const\s+int\s+([A-Za-z_]\w*)\s*=\s*(\d+)\s*;', code):
        line = code.count('\n', 0, found.start(1)) + 1
        declarations.append(f'{firmware["filename"]}:L{line}: const int {found[1]} = {found[2]}; This literal declaration alone does not prove pin usage.')
    functions = []
    for found in re.finditer(r'(?<![\w.:])void\s+(setup|loop)\s*\(\s*\)\s*\{', code):
        line = code.count('\n', 0, found.start()) + 1
        functions.append(f'{firmware["filename"]}:L{line}: void {found[1]}(). Arduino normally calls setup() once for initialization and loop() repeatedly; presence alone does not validate operation.')
    return {'pattern': pattern, 'excerpts': excerpts, 'declarations': declarations, 'functions': functions}


def button_circuit(service, project, pattern):
    if not pattern or project['board'] not in ('arduino-uno', 'arduino-nano', 'arduino-mega'):
        return None
    parts = project['components']
    edges = {frozenset(((wire['from']['component'], wire['from']['pin']),
                        (wire['to']['component'], wire['to']['pin']))) for wire in project['wires']}

    def connected(left, right):
        return frozenset((left, right)) in edges

    matches = []
    for board in (part for part in parts if part['type'] == project['board']):
        pins = service.catalog.components.get(board['type'], {}).get('pins', [])
        if pattern['input_pin'] not in pins or pattern['output_pin'] not in pins:
            continue
        grounds = [pin for pin in pins if re.fullmatch(r'GND(?:\.\d+)?', pin)]
        for button in (part for part in parts if part['type'] == 'pushbutton'):
            button_pins = service.catalog.components.get('pushbutton', {}).get('pins', [])
            for input_terminal in ('1.l', '1.r'):
                for ground_terminal in ('2.l', '2.r'):
                    if input_terminal not in button_pins or ground_terminal not in button_pins:
                        continue
                    if not connected((board['id'], pattern['input_pin']), (button['id'], input_terminal)):
                        continue
                    if not any(connected((button['id'], ground_terminal), (board['id'], ground)) for ground in grounds):
                        continue
                    for resistor in (part for part in parts if part['type'] == 'resistor'):
                        for led in (part for part in parts if part['type'] == 'led'):
                            for first, second in (('1', '2'), ('2', '1')):
                                if (connected((board['id'], pattern['output_pin']), (resistor['id'], first))
                                        and connected((resistor['id'], second), (led['id'], 'A'))
                                        and any(connected((led['id'], 'C'), (board['id'], ground)) for ground in grounds)):
                                    matches.append({'board': board['id'], 'button': button['id'], 'resistor': resistor['id'], 'led': led['id']})
    if len(matches) != 1:
        return None
    result = matches[0]
    involved = set(result.values())
    # Extra wires touching this circuit can change its behavior.
    if sum(any(endpoint['component'] in involved for endpoint in (wire['from'], wire['to'])) for wire in project['wires']) != 5:
        return None
    return result


STAGE_NAMES = {
    'introduction': 'Project introduction', 'preparation': 'Component preparation',
    'wiring': 'Wiring', 'verification': 'Connection verification',
    'firmware': 'Firmware', 'simulation': 'Compilation and simulation',
    'testing': 'Testing', 'completion': 'Completion',
}
CHECK_LABELS = {
    'expectations': 'Understand the intended behavior', 'inventory': 'Identify the saved parts',
    'power_off': 'Check power and part compatibility', 'wiring_review': 'Review the saved connections',
    'inspection': 'Inspect connections and polarity', 'findings': 'Review connection findings',
    'source': 'Review the saved sketch', 'compile': 'Review the compiler result',
    'runtime_limits': 'Review simulation support', 'observed': 'Compare actual behavior',
    'troubleshooting': 'Review failed or untested cases', 'summary': 'Review remaining work',
}


def human_names(service, project):
    parts = sorted(project['components'], key=lambda part: part['id'])
    totals = Counter(part['type'] for part in parts)
    seen = Counter()
    kinds, names = {}, {}
    for part in parts:
        kind = part['type']
        catalog_name = service.catalog.components.get(kind, {}).get('name')
        base = {'pushbutton': 'Button', 'led': 'LED', 'resistor': 'Resistor'}.get(kind, catalog_name or kind.replace('-', ' ').capitalize())
        kinds[kind] = base
        seen[kind] += 1
        names[part['id']] = f'{base} {seen[kind]}' if totals[kind] > 1 else base
    return names, kinds


def endpoint_name(endpoint, names):
    return f'{names.get(endpoint["component"], "Unknown component")} pin {endpoint["pin"]}'


def wire_action(wire, names):
    return f'Connect {endpoint_name(wire["from"], names)} to {endpoint_name(wire["to"], names)}.'


def enhance_static(service, project, steps):
    facts = source_facts(project['firmware'])
    pattern = facts['pattern']
    circuit = button_circuit(service, project, pattern)
    names, kinds = human_names(service, project)
    by_id = {step['id']: step for step in steps}
    board_name = kinds.get(project['board'], 'No board selected')
    quantities = [f'{count} × {kinds[kind]}' for kind, count in sorted(Counter(part['type'] for part in project['components']).items())]
    brief_parts = ', '.join(quantities) or 'No parts saved'
    objective = project.get('objective')
    goal = f'Saved objective: {objective.strip()}' if isinstance(objective, str) and objective.strip() else 'No objective is saved; the project title alone does not tell us what it should do.'
    if circuit:
        goal = f'Press {names[circuit["button"]]} to light {names[circuit["led"]]}; release it to turn the LED off.'
    elif pattern:
        goal = f'The sketch intends input pin {pattern["input_pin"]} LOW to set output pin {pattern["output_pin"]} HIGH, and otherwise LOW. The button and LED wiring could not be confirmed.'
    intro = by_id['introduction']
    intro.update(objective='Know what you are building.', why='A clear goal gives you something concrete to test.',
                 instructions=['Review the saved parts and board.', 'Confirm what the input should make the output do.'],
                 warnings=['This is expected behavior from the saved design, not a recorded test result.'] if circuit else ['Behavior is unknown for unsupported or ambiguous source; confirm the goal before building.'],
                 sections=[section("What you'll do", [goal, brief_parts + '.'])])
    prep_details = []
    for part in sorted(project['components'], key=lambda item: item['id']):
        kind = part['type']
        detail = 'Check its model and pinout against its datasheet.'
        if kind == project['board']:
            detail = 'Runs the sketch and provides the labeled connection pins.'
        elif circuit and part['id'] == circuit['button']:
            detail = 'Input switch; pressing connects the input to ground.'
        elif kind == 'led':
            detail = 'Light output; A is anode and C is cathode.'
        elif kind == 'resistor':
            value = part.get('properties', {}).get('value')
            detail = (f'Saved value: {value}; confirm the real resistance and rating. ' if value is not None else '')
            detail += 'Limits current in the LED path.' if circuit and part['id'] == circuit['resistor'] else 'Check its role in the saved connections.'
        prep_details.append(f'{names[part["id"]]}: {detail}')
    prep = by_id['preparation']
    prep.update(objective='Identify the parts before connecting them.', why='Correct pinouts and compatible parts prevent avoidable wiring mistakes.',
                instructions=['Disconnect USB and external power.', 'Gather the saved quantities and identify each part.', 'Check the real pinouts, orientation, and supply compatibility.'] if quantities else ['Select the intended board.', 'Add the parts needed for your goal.'],
                warnings=['Check voltage, current ratings, and physical pinouts in the datasheets; the saved plan does not validate them.'],
                sections=[section("What you'll do", quantities + prep_details),
                          section('Check your work', ['Suggested tools, not saved inventory: suitable leads, datasheets, and a continuity meter.'])])
    ordered = []
    groups = []
    if circuit:
        for label, ids, explanation in [
            ('Button path', {circuit['button']}, 'The button closes the input-to-ground path when pressed.'),
            ('LED path', {circuit['resistor'], circuit['led']}, 'The resistor sits in series with the LED; LED C returns to ground.')]:
            wires = sorted((wire for wire in project['wires'] if any(endpoint['component'] in ids for endpoint in (wire['from'], wire['to']))), key=lambda wire: wire['id'])
            actions = [wire_action(wire, names) for wire in wires]
            ordered.extend(actions)
            groups.append(section('Steps', [label + ': ' + explanation], actions))
        groups.append(section('Check your work', ['After each path, check both pin labels, LED A/C polarity, and unwanted contacts before continuing.']))
    else:
        ordered = [wire_action(wire, names) for wire in sorted(project['wires'], key=lambda wire: wire['id'])]
        groups = [section('Steps', ['Reproduce the saved endpoints; the purpose of unverified connections is unknown.'], ordered),
                  section('Check your work', ['Compare both ends of each wire with the real pinout; do not guess missing connections.'])]
    # Grouped actions are the wiring instructions; do not repeat them in the main list.
    by_id['wiring'].update(objective='Connect the saved pins, one wire at a time.', why='Exact endpoints matter more than wire color or position.',
                          instructions=[] if ordered else ['Review why no wires are saved.', 'Add connections only after confirming the design.'],
                          warnings=['Keep physical power disconnected while wiring.'], sections=groups)
    by_id['verification'].update(objective='Check the connections before power is applied.', why='A wrong pin or short can prevent the build from working safely.',
                                instructions=['Trace both ends of every saved connection.', 'Check polarity, loose leads, and unwanted contacts.', 'Run connection checks and review their findings.'],
                                warnings=['Automatic checks cover limited topologies, not physical safety or resistor sizing.'],
                                sections=[section('Check your work', ['For this design, trace the Button input-to-ground path and the LED series path.' if circuit else 'Match the exact pin labels in Wiring and inspect the real assembly separately.'])])
    firmware_paragraphs = ['Behavior is unknown for this source. Read the saved code, trace pin definitions, and check board/library requirements; comments and strings do not establish execution flow.']
    if pattern:
        firmware_paragraphs = [f'{pattern["input"]} = {pattern["input_pin"]}; {pattern["output"]} = {pattern["output_pin"]}. setup() selects INPUT_PULLUP for the input and OUTPUT for the output.',
                               'loop() reads the input: LOW writes HIGH to the output; otherwise it writes LOW. There is no delay, serial output, or debounce logic in this matched sketch.']
        if circuit:
            firmware_paragraphs.append('INPUT_PULLUP holds the released Button HIGH. Pressing connects it to ground, so pressed is LOW: this is active-low logic.')
        else:
            firmware_paragraphs.append('The sketch is recognized, but the Button and LED connections do not match a unique supported path.')
    by_id['firmware'].update(objective='Understand the input-to-output decision.', why='The pin assignments and logic should agree with your wiring.',
                            instructions=['Open the saved source and find setup() and loop().', 'Match the input and output pins to Wiring.', 'Save intentional changes before compiling.'],
                            warnings=[], sections=[section("What you'll do", firmware_paragraphs),
                                                   section('Check your work', ['Read the saved source for details; unsupported syntax is not an inferred execution trace.'] if not pattern else ['Check that the sketch pins match the connected board pins.'])])
    from backend.hardware import BOARD_CONFIG
    board = BOARD_CONFIG.get(project['board'], {})
    support = f'Target: {board_name}. Simulation support: {board.get("simulation", "not confirmed")}.'
    unsupported = sorted({kinds[part['type']] for part in project['components'] if service.catalog.components.get(part['type'], {}).get('simulation_supported') is not True})
    if unsupported:
        support += ' Simulation support is unconfirmed or unavailable for: ' + ', '.join(unsupported) + '.'
    by_id['simulation'].update(objective='Build the saved sketch for the selected board.', why='A current build checks the code, not the circuit’s behavior.',
                              instructions=['Save any draft in Code.', f'Compile for {board_name} and review the result.', 'Verify the current compiler artifact before starting a supported simulation.'],
                              warnings=['A successful compile does not upload or execute the sketch.'],
                              sections=[section("What you'll do", [support])])
    test_actions = ['Start a supported run after connection and compiler review.']
    if circuit:
        button_name, led_name = names[circuit['button']], names[circuit['led']]
        test_actions += [f'Release {button_name}: expect {led_name} off.', f'Press and hold {button_name}: expect {led_name} on.', f'Release {button_name} again and check that {led_name} turns off.', 'Record what actually happened, including failures.']
        test_check = f'Released: input pin {pattern["input_pin"]} HIGH, output pin {pattern["output_pin"]} LOW. Pressed: input LOW, output HIGH.'
        trouble = 'If the LED stays on or off, stop and check the Button ground path, output pin, LED polarity, and series resistor.'
    else:
        test_actions += ['State the input condition and expected output.', 'Try the initial and changed inputs.', 'Record the actual outputs and leave untested cases unconfirmed.']
        test_check = 'Define the expected behavior from reviewed source or requirements before claiming a pass.'
        trouble = 'If output differs, stop and check the saved logic, endpoints, and runtime support.'
    by_id['testing'].update(objective='Compare real observations with the intended behavior.', why='Starting a run or compiling successfully is not a behavior test.',
                           instructions=test_actions, warnings=['Stop if a part heats unexpectedly; disconnect power before changing wires.'],
                           sections=[section('Check your work', [test_check, trouble])])
    by_id['completion'].update(objective='Choose the next useful task.', why='Confirmed tasks and tested behavior are different things.', instructions=[], warnings=[], sections=[])
    wires_by_id = {wire['id']: wire for wire in project['wires']}
    for step in steps:
        step['components'] = quantities if step['id'] == 'preparation' else []
        step['pins'] = []
        for check in step['checklist']:
            if check['id'].startswith('wiring.wire.'):
                wire = wires_by_id.get(check['id'][len('wiring.wire.'):])
                check['display_label'] = wire_action(wire, names).replace('Connect ', 'Check ', 1) if wire else 'Check this connection'
            else:
                check['display_label'] = CHECK_LABELS.get(check['id'].split('.')[-1], 'Review this task')
    return steps


def compiler_state(project, current_result):
    from backend.hardware import BOARD_CONFIG
    compiler = project.get('compiler') or {}
    if BOARD_CONFIG.get(project['board'], {}).get('compile') is not True:
        return 'unsupported'
    if not compiler:
        return 'not_tested'
    if (compiler.get('stale') or compiler.get('project_revision') != project['revision']
            or compiler.get('source_revision') != project['firmware']['revision']
            or compiler.get('source') != project['firmware']['source']):
        return 'stale'
    return 'current_pass' if current_result['status'] == 'passed' else 'failed'


def compiler_sections(project, current_result):
    state = compiler_state(project, current_result)
    message, action = {
        'unsupported': ('Compiler: Unsupported for this board.', 'Use the board’s documented external build workflow or select the intended supported board.'),
        'not_tested': ('Compiler: Not tested — no saved build result.', 'Save the sketch and compile for the selected board.'),
        'stale': ('Compiler: Outdated — recompile the current project.', 'Save any draft, recompile, and verify the new artifact.'),
        'current_pass': ('Compiler: Current build passed; behavior still needs testing.', 'Start a supported run and compare the expected input/output cases.'),
        'failed': ('Compiler: Failed — no current successful artifact.', 'Open compiler diagnostics, fix the first error, save, and recompile.'),
    }[state]
    paragraphs = [message]
    if state == 'stale':
        compiler = project.get('compiler') or {}
        if compiler.get('project_revision') != project['revision']:
            paragraphs.append(f'The saved build is from project revision {compiler.get("project_revision", "unknown")}; the current revision is {project["revision"]}.')
        else:
            paragraphs.append('The saved result is marked stale or no longer matches the saved source.')
    if state == 'failed':
        compiler = project.get('compiler') or {}
        errors = compiler.get('errors') or []
        errors = errors if isinstance(errors, list) else [errors]
        stderr = compiler.get('stderr') or ''
        first = next((str(error) for error in errors if str(error).strip()), next((line for line in str(stderr).splitlines() if 'error' in line.lower()), ''))
        lowered = first.lower()
        location = re.search(r'([^\s:]+\.(?:ino|cpp|c|h)):(\d+)(?::\d+)?', first)
        at_line = f' near line {location[2]}' if location else ''
        if 'not declared' in lowered:
            paragraphs.append('The first error is an unknown name' + at_line + '; check its spelling and definition.')
            action = 'Correct the missing name in the saved source, then recompile.'
        elif 'no such file' in lowered or 'file not found' in lowered:
            paragraphs.append('The first error is a missing header or library; check the include and board-compatible dependency.')
            action = 'Check the include and required library in the build environment, then recompile.'
        elif 'expected' in lowered:
            paragraphs.append('The first error is a syntax issue' + at_line + '; check punctuation and braces just before it.')
            action = 'Correct the cited statement and preceding punctuation, then recompile.'
        elif not first:
            paragraphs.append('No usable error text is saved; check the artifact and detailed compiler result.')
    return section('Check your work', paragraphs, [action])


def connection_section(project, guide, names):
    evidence = guide['progress']['evidence']['topology']
    status = evidence['status']
    paragraphs = [{
        'not_checked': 'Connections: Not checked yet.', 'outdated': 'Connections: Outdated — run checks again.',
        'failed': 'Connections: Failed — fix the findings before power is applied.',
        'passed': 'Connections: No errors found by the limited checker.',
    }.get(status, 'Connections: Review the latest findings.')]
    findings = evidence.get('details', {})
    if status in ('failed', 'passed'):
        entries = findings.get('errors', []) + findings.get('warnings', [])
        if entries:
            finding = entries[0]
            detail = finding.get('evidence', {})
            endpoints = detail.get('endpoints', [])
            description = {
                'power_short': 'Different power or ground rails are connected together.',
                'led_bypassed': 'The LED terminals share a net, bypassing the LED.',
                'led_missing_series_resistor': 'The LED path has no confirmed series resistor.',
                'led_topology_unverified': 'The LED series path could not be confirmed.',
                'self_wire': 'A connection returns to the same pin.',
                'duplicate_connection': 'The same connection is saved more than once.',
                'duplicate_wire_id': 'The saved wire records conflict; review the connections.',
                'invalid_wire': 'A saved connection has an invalid endpoint.',
            }.get(finding.get('code'), 'A connection needs review; open the detailed findings.')
            if endpoints:
                description += ' Check ' + ' and '.join(endpoint_name(endpoint, names) for endpoint in endpoints[:2]) + '.'
            paragraphs.append(description)
    return section('Check your work', paragraphs)


def refresh_dynamic(service, project, guide, current_result):
    progress = guide['progress']
    steps = guide['steps']
    checks = progress['checks']
    by_id = {step['id']: step for step in steps}
    names, _ = human_names(service, project)
    confirmed = sum(state['confirmed'] and not state['outdated'] for state in checks.values())
    earliest = next((step['id'] for step in steps if step['id'] in progress['outdated_steps'] or any(not checks[check['id']]['confirmed'] or checks[check['id']]['outdated'] for check in step['checklist'])), None)
    topology = progress['evidence']['topology']['status']
    state = compiler_state(project, current_result)
    target = 'verification' if topology == 'failed' else earliest
    if target is None:
        target = 'simulation' if state != 'current_pass' else 'verification' if topology in ('not_checked', 'outdated') else 'testing'
    compiler = compiler_sections(project, current_result)
    by_id['verification']['sections'].append(connection_section(project, guide, names))
    by_id['simulation']['sections'].append(compiler)
    by_id['simulation']['sections'].append(section("What's next", ['Simulation: Not tested here — no saved behavior results. Start a supported run explicitly.']))
    observed = checks['testing.observed']
    runtime = 'Behavior: Not tested here — no saved behavior results.'
    if observed['outdated']:
        runtime += ' Your previous confirmation is outdated; repeat the observations.'
    elif observed['confirmed']:
        runtime += ' You confirmed the task manually; that is not a saved test result.'
    by_id['testing']['sections'].append(section("What's next", [runtime]))
    gaps = []
    if topology == 'failed':
        gaps.append('Resolve failed connection findings first.')
    elif topology in ('not_checked', 'outdated'):
        gaps.append('Connection checks still need a current result.')
    elif progress['evidence']['topology'].get('details', {}).get('warnings'):
        gaps.append('Review the remaining connection warnings.')
    if state != 'current_pass':
        gaps.append(compiler['paragraphs'][0])
    gaps.append('Behavior: Not tested here — no saved behavior results.' if not observed['confirmed'] or observed['outdated'] else 'Behavior: Manually confirmed task; no saved behavior results.')
    if progress['outdated_steps']:
        gaps.append('Outdated review: ' + ', '.join(STAGE_NAMES[step['id']] for step in steps if step['id'] in progress['outdated_steps']) + '.')
    target_actions = by_id[target]['instructions'][:3]
    if target == 'wiring':
        target_actions = [action for item in by_id[target]['sections'] if item['title'] == 'Steps' for action in item['actions']][:3] or by_id[target]['instructions'][:3]
    if target == 'simulation':
        target_actions = [compiler['actions'][0], 'Review the new compiler result before testing.']
    if target == 'verification' and topology == 'failed':
        target_actions = ['Keep physical power disconnected.', 'Open the connection findings and correct the cited pins.', 'Run connection checks again.']
    next_action = f'Continue with {STAGE_NAMES[target]}.'
    summary = by_id['completion']
    summary['instructions'] = target_actions
    summary['sections'] = [section("What you'll do", [f'{confirmed} of {len(checks)} tasks confirmed by you — not automatic passes.', f'Next stage: {STAGE_NAMES[target]}.']),
                           section('Check your work', gaps), section("What's next", actions=[next_action])]
    return guide
