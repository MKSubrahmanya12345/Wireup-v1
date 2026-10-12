"""Manual, atomic routing through canonical mini-breadboard terminal strips."""

import copy
import re
import uuid

from .hardware import BOARD_CONFIG, fail, now, public


BREADBOARDS = frozenset(('breadboard', 'breadboard-mini'))
BUTTONS = frozenset(('pushbutton', 'pushbutton-6mm'))


def _internal_group(kind, pin):
    if kind in BREADBOARDS:
        hole = re.fullmatch(r'(\d+)([tb])\.([a-j])', pin)
        if hole:
            return 'strip', hole[1], hole[2]
        rail = re.fullmatch(r'([tb][pn])\.\d+', pin)
        if rail:
            return 'rail', rail[1]
    if kind in BUTTONS:
        terminal = re.fullmatch(r'([12])\.[lr]', pin)
        if terminal:
            return 'button', terminal[1]
    return None


def _nets(service, project):
    parts = {part['id']: part for part in project['components']}
    parents, endpoints, internal, wires = {}, {}, {}, []

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left, right):
        left, right = root(left), root(right)
        if left != right:
            parents[right] = left

    def add(value):
        endpoint = service._endpoint(project, value)
        key = endpoint['component'], endpoint['pin']
        parents.setdefault(key, key)
        endpoints[key] = endpoint
        group = _internal_group(parts[key[0]]['type'], key[1])
        if group is not None:
            internal_key = key[0], group
            previous = internal.setdefault(internal_key, key)
            union(key, previous)
        return key

    for wire in project['wires']:
        start, end = add(wire.get('from')), add(wire.get('to'))
        if start == end:
            fail(400, 'A wire must connect two distinct pins.')
        color = wire.get('color', '#22c55e')
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}|[a-zA-Z]{1,20}', color):
            fail(400, 'Invalid wire color.')
        union(start, end)
        wires.append((start, color))

    groups = {}
    for key, endpoint in endpoints.items():
        groups.setdefault(root(key), {'endpoints': []})['endpoints'].append(endpoint)
    for key, color in wires:
        groups[root(key)].setdefault('color', color)
    return list(groups.values())


def _strips(pins):
    groups = {}
    for pin in pins:
        hole = re.fullmatch(r'(\d+)([tb])\.([a-j])', pin)
        if hole and hole[3] in ('abcde' if hole[2] == 't' else 'fghij'):
            groups.setdefault((int(hole[1]), hole[2]), set()).add(pin)
    return [sorted(holes) for _, holes in sorted(groups.items()) if len(holes) == 5]


async def route_breadboard(service, project_id, args):
    """Replace explicit wires once; no component seating or firmware changes."""
    if not isinstance(args, dict) or set(args) - {'expected_revision'}:
        fail(400, 'Breadboard routing accepts only expected_revision.')
    with service.lock:
        project = service._load(project_id)
        service._check_revision(project, args)
        existing = next((part for part in project['components'] if part['type'] in BREADBOARDS), None)
        if existing:
            return {'project': public(project), 'changed': False, 'existing': True,
                    'reason': 'A breadboard already exists; its stored routing was not changed.',
                    'breadboard_id': existing['id']}
        external = [part for part in project['components']
                    if part['type'] not in BOARD_CONFIG and part['id'] != 'board']
        if not external or not project['wires']:
            return {'project': public(project), 'changed': False, 'existing': False,
                    'reason': 'Add external components and wires before routing through a breadboard.',
                    'breadboard_id': None}
        entry = service.catalog.components.get('breadboard-mini')
        if entry is None:
            fail(400, 'The canonical mini-breadboard catalog entry is unavailable.')
        nets = _nets(service, project)
        strips = _strips(entry['pins'])
        if any(len(net['endpoints']) > 5 for net in nets) or len(nets) > len(strips):
            fail(400, 'Mini-breadboard capacity exceeded; each net needs a distinct five-hole strip.')
        if len(project['components']) >= 200:
            fail(400, 'Project component limit reached.')
        if sum(len(net['endpoints']) for net in nets) > 500:
            fail(400, 'Project wire limit reached.')
        candidate = copy.deepcopy(project)
        ids = {part['id'] for part in project['components']}
        breadboard_id = 'breadboard'
        while breadboard_id in ids:
            breadboard_id = 'breadboard-' + uuid.uuid4().hex
        properties = copy.deepcopy(entry.get('defaultValues', {}))
        properties = service._properties('breadboard-mini', properties)
        candidate['components'].append({'id': breadboard_id, 'type': 'breadboard-mini',
                                        'x': 370, 'y': 300, 'rotation': 0, 'properties': properties})
        candidate['wires'] = []
        wire_ids = {wire['id'] for wire in project['wires']}
        for net, holes in zip(nets, strips):
            for endpoint, hole in zip(net['endpoints'], holes):
                start = service._endpoint(candidate, endpoint)
                end = service._endpoint(candidate, {'component': breadboard_id, 'pin': hole})
                wire_id = uuid.uuid4().hex
                while wire_id in wire_ids:
                    wire_id = uuid.uuid4().hex
                wire_ids.add(wire_id)
                candidate['wires'].append({'id': wire_id, 'from': start, 'to': end, 'color': net['color']})
        snapshot = {key: copy.deepcopy(project[key])
                    for key in ('name', 'board', 'components', 'wires', 'firmware')}
        candidate['_undo'] = (candidate['_undo'] + [snapshot])[-50:]
        candidate['revision'] += 1
        if candidate['compiler']:
            candidate['compiler']['stale'] = True
        candidate['updated_at'] = now()
        candidate['history'] = (candidate['history'] + [
            {'revision': candidate['revision'], 'operation': 'route_breadboard',
             'timestamp': candidate['updated_at']}])[-200:]
        service._save(candidate)
        return {'project': public(candidate), 'changed': True, 'existing': False,
                'reason': 'Stored wires now route through distinct mini-breadboard terminal strips; component seating is unchanged.',
                'breadboard_id': breadboard_id}
