"""Atomic additive wiring and limited, topology-only circuit diagnostics.

wire_circuit(service, project_id, args) accepts {batch: [wire, ...],
expected_revision?: int}. Each wire has from/to canonical endpoint objects,
optional id/color, and optional waypoints: [{x: number, y: number}, ...].
It returns {project, validation, wires, added, existing, changed}. Existing
connections are reused (either orientation); explicit conflicting metadata is
rejected. Duplicate connections within the batch are rejected. All errors leave
the stored project unchanged; a changed batch creates exactly one undo entry.

validate_circuit(service, project_id) returns {project_id, revision, valid,
nets, errors, warnings, scope} without writing. Findings include endpoint and
wire evidence. This is not simulation, ERC, or a general electrical safety check.
check_connections(service, project_id, args) returns {project, validation, before,
recheck, changed, repairs, proposed_repairs, status}. It is read-only by default.
With repair: true and expected_revision, recognized pin aliases and electrically
redundant self/duplicate wires are repaired only when the full recheck has no
errors. Duplicate metadata must match exactly. Other errors block every repair.
Runtime behavior matches ordinary HardwareService mutations: a connected browser
is not proof of a running simulation and does not itself block editing.
"""

import copy
import json
import re
import uuid

from fastapi import HTTPException

from .hardware import BOARD_CONFIG, fail, identifier, now, numeric, public


SCOPE = (
    'Checks canonical endpoints, duplicate/self wires, known board 3.3V/5V/GND '
    'wire-net shorts, and simple LED/positive-resistor/ground topologies only. '
    'GPIO drive state, current, resistor sizing, internal connectivity of other '
    'parts, and general electrical correctness are not verified.'
)


def _key(endpoint):
    return endpoint['component'], endpoint['pin']


def _connection(start, end):
    return tuple(sorted((_key(start), _key(end))))


def _rail(component, pin):
    if component['type'] == 'ground' and pin == 'GND':
        return 'GND'
    if component['type'] not in BOARD_CONFIG:
        return None
    base = pin.split('.')[0] if pin != '3.3V' else pin
    if base == 'GND':
        return 'GND'
    if base in ('3V3', '3.3V'):
        return '3.3V'
    if base == '5V':
        return '5V'
    return None


def _gpio(component, pin):
    kind = component['type']
    if kind in ('arduino-uno', 'arduino-nano', 'arduino-mega'):
        return bool(re.fullmatch(r'\d+(?:\.2)?|A\d+(?:\.2)?', pin))
    if kind in ('pi-pico', 'pi-pico-w'):
        return bool(re.fullmatch(r'GP\d+', pin))
    if kind.startswith('raspberry-pi-'):
        return bool(re.fullmatch(r'GPIO\d+', pin))
    # ESP32 input-only and flash pins are deliberately not treated as drivers.
    return False


def _validate(hwservice, project):
    errors, warnings = [], []
    components = {part['id']: part for part in project['components']}
    endpoints, parents = {}, {}

    def add(endpoint):
        key = _key(endpoint)
        endpoints[key] = endpoint
        parents.setdefault(key, key)
        return key

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left, right):
        left, right = root(left), root(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    def finding(target, code, message, **evidence):
        target.append({'code': code, 'message': message, 'evidence': evidence})

    # Only known rails have implicit connectivity; resistors never merge nets.
    rails = {}
    for part in project['components']:
        entry = hwservice.catalog.components.get(part['type'], {})
        for pin in entry.get('pins', []):
            key = add({'component': part['id'], 'pin': pin})
            rail = _rail(part, pin)
            if rail:
                group = ('ground', rail) if rail == 'GND' else (part['id'], rail)
                if group in rails:
                    union(key, rails[group])
                else:
                    rails[group] = key

    wires, ids, connections = [], set(), set()
    for wire in project['wires']:
        wire_id = wire.get('id') if isinstance(wire, dict) else None
        try:
            if not isinstance(wire, dict):
                fail(400, 'Wire must be an object.')
            identifier(wire_id)
            start = hwservice._endpoint(project, wire.get('from'))
            end = hwservice._endpoint(project, wire.get('to'))
        except HTTPException as error:
            finding(errors, 'invalid_wire', str(error.detail), wire_id=wire_id,
                    wire=copy.deepcopy(wire))
            continue
        if wire_id in ids:
            finding(errors, 'duplicate_wire_id', 'Wire identifier is repeated.', wire_id=wire_id)
        ids.add(wire_id)
        connection = _connection(start, end)
        if start == end:
            finding(errors, 'self_wire', 'A wire connects a pin to itself.', wire_id=wire_id, endpoints=[start, end])
        if connection in connections:
            finding(errors, 'duplicate_connection', 'Wire connection is repeated.', wire_id=wire_id, endpoints=[start, end])
        connections.add(connection)
        union(add(start), add(end))
        wires.append({'id': wire_id, 'from': start, 'to': end})

    grouped = {}
    for key in sorted(endpoints):
        grouped.setdefault(root(key), []).append(endpoints[key])
    nets, net_by_pin = [], {}
    for members in grouped.values():
        net_id = f'net-{len(nets) + 1}'
        member_keys = {_key(endpoint) for endpoint in members}
        labels = sorted({rail for endpoint in members
                         if (rail := _rail(components[endpoint['component']], endpoint['pin']))})
        wire_ids = [wire['id'] for wire in wires if _key(wire['from']) in member_keys]
        net = {'id': net_id, 'endpoints': members, 'wire_ids': wire_ids, 'rails': labels}
        nets.append(net)
        for key in member_keys:
            net_by_pin[key] = net
        if len(labels) > 1:
            finding(errors, 'power_short', 'Distinct known power/ground rails share a wire net.',
                    net_id=net_id, rails=labels, endpoints=members, wire_ids=wire_ids)

    def drivers(net):
        return [endpoint for endpoint in net['endpoints']
                if _rail(components[endpoint['component']], endpoint['pin']) in ('3.3V', '5V')
                or _gpio(components[endpoint['component']], endpoint['pin'])]

    for part in project['components']:
        if part['type'] != 'led':
            continue
        anode = net_by_pin.get((part['id'], 'A'))
        cathode = net_by_pin.get((part['id'], 'C'))
        if not anode or not cathode:
            continue
        evidence = {'component_id': part['id'], 'net_ids': [anode['id'], cathode['id']],
                    'endpoints': anode['endpoints'] + cathode['endpoints'],
                    'wire_ids': list(dict.fromkeys(anode['wire_ids'] + cathode['wire_ids']))}
        if anode is cathode:
            finding(errors, 'led_bypassed', 'LED terminals share the same wire net.', **evidence)
            continue
        direct = drivers(anode) if 'GND' in cathode['rails'] else []
        if direct:
            hard_power = any(_rail(components[e['component']], e['pin']) in ('3.3V', '5V') for e in direct)
            finding(errors if hard_power else warnings, 'led_missing_series_resistor',
                    'LED is directly connected between a known supply and ground without a series resistor.'
                    if hard_power else 'LED is directly connected between a GPIO and ground without a series resistor; GPIO drive state is unknown.',
                    drivers=direct, **evidence)
            continue
        series = False
        for led_net, other_net in ((anode, cathode), (cathode, anode)):
            if len(led_net['endpoints']) != 2:
                continue
            resistor_pin = next((e for e in led_net['endpoints']
                                 if components[e['component']]['type'] == 'resistor'), None)
            if not resistor_pin:
                continue
            resistor = components[resistor_pin['component']]
            opposite = '2' if resistor_pin['pin'] == '1' else '1'
            outer = net_by_pin.get((resistor['id'], opposite))
            if not outer or outer is led_net:
                continue
            try:
                hwservice._properties('resistor', resistor.get('properties', {}))
            except HTTPException:
                continue
            if led_net is anode:
                series = bool(drivers(outer)) and 'GND' in other_net['rails']
            else:
                series = 'GND' in outer['rails'] and bool(drivers(other_net))
            if series:
                break
        if not series:
            finding(warnings, 'led_topology_unverified',
                    'A simple LED/positive series resistor/driver/ground path was not established.', **evidence)
    return {'project_id': project['id'], 'revision': project['revision'], 'valid': not errors,
            'nets': nets, 'errors': errors, 'warnings': warnings, 'scope': SCOPE}


async def validate_circuit(hwservice, project_id):
    """Return diagnostics for one locked, current project snapshot; never write."""
    with hwservice.lock:
        return _validate(hwservice, hwservice._load(project_id))


def _connection_report(hwservice, project):
    component_errors, ids = [], set()
    for index, part in enumerate(project['components']):
        try:
            if not isinstance(part, dict):
                fail(400, 'Component must be an object.')
            component_id = identifier(part.get('id'))
            kind = part.get('type')
            if not isinstance(kind, str) or kind not in hwservice.catalog.components:
                fail(400, 'Unknown component type.')
            if component_id in ids:
                fail(400, 'Component identifier is repeated.')
            ids.add(component_id)
        except HTTPException as error:
            component_errors.append({'code': 'invalid_component', 'message': str(error.detail),
                                     'evidence': {'component_index': index}})
    if component_errors:
        return {'project_id': project['id'], 'revision': project['revision'], 'valid': False,
                'nets': [], 'errors': component_errors, 'warnings': [],
                'scope': SCOPE + ' Net analysis and repair are skipped when component identities are invalid.'}
    report = _validate(hwservice, project)
    for index, wire in enumerate(project['wires']):
        if not isinstance(wire, dict):
            continue
        try:
            start = hwservice._endpoint(project, wire.get('from'))
            end = hwservice._endpoint(project, wire.get('to'))
        except HTTPException:
            continue
        if start != wire['from'] or end != wire['to']:
            report['errors'].append({
                'code': 'noncanonical_endpoint',
                'message': 'Wire uses a recognized pin alias instead of the canonical pin.',
                'evidence': {'wire_id': wire.get('id'), 'wire_index': index,
                             'endpoints': [start, end]},
            })
    connected = {_key(endpoint) for net in report['nets'] if net['wire_ids']
                 for endpoint in net['endpoints']}
    required = {'servo': ('GND', 'V+', 'PWM'),
                'potentiometer': ('GND', 'VCC', 'SIG')}
    for part in project['components']:
        for pin in required.get(part['type'], ()):
            if (part['id'], pin) not in connected:
                report['warnings'].append({
                    'code': 'unconnected_required_pin',
                    'message': f'{part["id"]} has no wire path at {pin}. Review whether this part is intentionally unused.',
                    'evidence': {'component_id': part['id'],
                                 'endpoints': [{'component': part['id'], 'pin': pin}]},
                })
    breadboards = [part['id'] for part in project['components'] if part['type'].startswith('breadboard')]
    if breadboards:
        report['warnings'].append({
            'code': 'breadboard_connectivity_unverified',
            'message': 'Breadboard internal strips and rails are not analyzed by this check. Inspect the breadboard connections separately; valid endpoint names do not prove a complete path.',
            'evidence': {'component_ids': breadboards},
        })
    if any(part['type'] == 'servo' for part in project['components']):
        report['warnings'].append({
            'code': 'servo_power_unverified',
            'message': 'Servo supply capacity, actual voltage, common-ground assembly, and mechanical clearance need manual verification. Saved wires alone do not establish them.',
            'evidence': {'component_ids': [part['id'] for part in project['components']
                                          if part['type'] in ('servo', 'power-supply')]},
        })
    report['valid'] = not report['errors']
    report['scope'] += (' Checks missing wire paths at servo and potentiometer pins, without inferring intended use. '
                        'Connection checking also flags recognized pin aliases. '
                        'Repair only canonicalizes aliases and removes self wires or duplicate '
                        'connections with matching metadata; it never infers intended wiring.')
    return report


async def check_connections(hwservice, project_id, args=None):
    """Inspect connections; explicitly requested repairs must pass a full recheck."""
    args = {} if args is None else args
    if not isinstance(args, dict) or set(args) - {'repair', 'expected_revision'}:
        fail(400, 'Connection checking accepts only repair and expected_revision.')
    repair = args.get('repair', False)
    if type(repair) is not bool:
        fail(400, 'Connection repair must be boolean.')
    revision = args.get('expected_revision')
    if revision is not None and (type(revision) is not int or revision < 0):
        fail(400, 'Expected revision must be a nonnegative integer.')
    if repair and revision is None:
        fail(400, 'Connection repair requires expected_revision.')
    with hwservice.lock:
        project = hwservice._load(project_id)
        hwservice._check_revision(project, args)
        before = _connection_report(hwservice, project)
        candidate = copy.deepcopy(project)
        actions = []
        if not any(item['code'] == 'invalid_component' for item in before['errors']):
            retained, connections = [], {}
            for index, wire in enumerate(candidate['wires']):
                if not isinstance(wire, dict):
                    retained.append(wire)
                    continue
                try:
                    identifier(wire.get('id'))
                    start = hwservice._endpoint(candidate, wire.get('from'))
                    end = hwservice._endpoint(candidate, wire.get('to'))
                except HTTPException:
                    retained.append(wire)
                    continue
                evidence = {'wire_id': wire['id'], 'wire_index': index}
                if start == end:
                    actions.append({'operation': 'remove_self_wire', **evidence})
                    continue
                connection = _connection(start, end)
                metadata = {key: value for key, value in wire.items()
                            if key not in ('id', 'from', 'to')}
                previous = connections.get(connection)
                if previous is not None and previous['metadata'] == metadata:
                    actions.append({'operation': 'remove_duplicate_wire', **evidence,
                                    'retained_wire_id': previous['id']})
                    continue
                connections.setdefault(connection, {'id': wire['id'], 'metadata': metadata})
                if start != wire['from'] or end != wire['to']:
                    actions.append({'operation': 'canonicalize_endpoints', **evidence,
                                    'from': start, 'to': end})
                    wire['from'], wire['to'] = start, end
                retained.append(wire)
            candidate['wires'] = retained
        after = _connection_report(hwservice, candidate) if repair else before
        changed = repair and bool(actions) and after['valid']
        if changed:
            snapshot = {key: copy.deepcopy(project[key])
                        for key in ('name', 'board', 'components', 'wires', 'firmware')}
            candidate['_undo'] = (candidate['_undo'] + [snapshot])[-50:]
            candidate['revision'] += 1
            if candidate['compiler']:
                candidate['compiler']['stale'] = True
            candidate['updated_at'] = now()
            candidate['history'] = (candidate['history'] + [
                {'revision': candidate['revision'], 'operation': 'check_connections',
                 'timestamp': candidate['updated_at']}])[-200:]
            hwservice._save(candidate)
            after['revision'] = candidate['revision']
        return {'project': public(candidate if changed else project),
                'validation': after if changed else before, 'before': before,
                'recheck': after if repair else None, 'changed': changed,
                'repairs': actions if changed else [],
                'proposed_repairs': actions,
                'status': 'repaired' if changed else 'needs_review' if not before['valid'] else 'checked'}


async def wire_circuit(hwservice, project_id, args):
    """Validate the complete additive candidate, then save it as one mutation."""
    if not isinstance(args, dict):
        fail(400, 'Command args must be an object.')
    try:
        encoded = json.dumps(args, allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        fail(400, 'Command args must contain finite JSON values.')
    if len(encoded) > 200000:
        fail(400, 'Command arguments are too large.')
    if set(args) - {'batch', 'expected_revision'}:
        fail(400, 'Unknown circuit wiring argument.')
    batch = args.get('batch')
    if not isinstance(batch, list) or not 1 <= len(batch) <= 500:
        fail(400, 'Wiring batch must contain 1 to 500 wires.')
    with hwservice.lock:
        project = hwservice._load(project_id)
        hwservice._check_revision(project, args)
        candidate = copy.deepcopy(project)
        connections = {}
        for wire in project['wires']:
            start = hwservice._endpoint(project, wire.get('from'))
            end = hwservice._endpoint(project, wire.get('to'))
            connections[_connection(start, end)] = wire
        ids = {wire['id'] for wire in project['wires']}
        seen, added, existing, resolved = set(), [], [], []
        for item in batch:
            if not isinstance(item, dict) or set(item) - {'id', 'from', 'to', 'color', 'waypoints'}:
                fail(400, 'Wire must be an object with id/from/to/color/waypoints fields only.')
            start = hwservice._endpoint(candidate, item.get('from'))
            end = hwservice._endpoint(candidate, item.get('to'))
            if start == end:
                fail(400, 'A wire must connect two distinct pins.')
            connection = _connection(start, end)
            if connection in seen:
                fail(409, 'Duplicate connection within wiring batch.')
            seen.add(connection)
            wire_id = identifier(item['id']) if 'id' in item else None
            color = item.get('color', '#22c55e')
            if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}|[a-zA-Z]{1,20}', color):
                fail(400, 'Invalid wire color.')
            waypoints = None
            if 'waypoints' in item:
                waypoints = item['waypoints']
                if not isinstance(waypoints, list) or len(waypoints) > 64:
                    fail(400, 'Wire waypoints must contain at most 64 points.')
                for point in waypoints:
                    if not isinstance(point, dict) or set(point) != {'x', 'y'}:
                        fail(400, 'Each waypoint must contain x and y.')
                    numeric(point['x'])
                    numeric(point['y'])
            previous = connections.get(connection)
            if previous:
                if (wire_id is not None and wire_id != previous['id']
                        or 'color' in item and color != previous.get('color')
                        or waypoints is not None and waypoints != previous.get('waypoints', [])):
                    fail(409, 'Existing connection has conflicting wire metadata.')
                existing.append(previous['id'])
                resolved.append(copy.deepcopy(previous))
                continue
            wire_id = wire_id or uuid.uuid4().hex
            if wire_id in ids:
                fail(409, 'Wire identifier already exists.')
            ids.add(wire_id)
            wire = {'id': wire_id, 'from': start, 'to': end, 'color': color}
            if waypoints is not None:
                wire['waypoints'] = copy.deepcopy(waypoints)
            candidate['wires'].append(wire)
            added.append(wire_id)
            resolved.append(copy.deepcopy(wire))
        if len(candidate['wires']) > 500:
            fail(400, 'Project wire limit reached.')
        validation = _validate(hwservice, candidate)
        if not validation['valid']:
            fail(400, {'message': 'Circuit wiring validation failed; no changes saved.', 'validation': validation})
        if added:
            snapshot = {key: copy.deepcopy(project[key])
                        for key in ('name', 'board', 'components', 'wires', 'firmware')}
            candidate['_undo'] = (candidate['_undo'] + [snapshot])[-50:]
            candidate['revision'] += 1
            if candidate['compiler']:
                candidate['compiler']['stale'] = True
            candidate['updated_at'] = now()
            candidate['history'] = (candidate['history'] + [
                {'revision': candidate['revision'], 'operation': 'wire_circuit',
                 'timestamp': candidate['updated_at']}])[-200:]
            hwservice._save(candidate)
            validation['revision'] = candidate['revision']
        return {'project': public(candidate), 'validation': validation, 'wires': resolved,
                'added': added, 'existing': existing, 'changed': bool(added)}
