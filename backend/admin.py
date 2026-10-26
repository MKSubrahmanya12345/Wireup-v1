"""Admin analytics for the invite-only beta control room.

The beta event log is intentionally the single source of truth for behaviour.
This module joins it with the canonical hardware project files so an operator can
answer: who is testing, what are they building, what does the agent do for them,
and where does the product fail.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from fastapi import APIRouter, HTTPException, Query, Request

from backend import beta

ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path(os.getenv('WIREUP_HARDWARE_DIR', ROOT / 'backend/data/hardware'))

router = APIRouter(prefix='/api/beta/admin', tags=['beta-admin'])

RUN_START = ('ai.run_started', 'ai_run_started')
RUN_END = ('ai.run_completed', 'ai.run_failed', 'ai_run_completed', 'ai_run_failed')
RUN_SUCCESS = {'success', 'completed', 'awaiting_answers', 'awaiting_approval', 'awaiting_revision', 'cancelled'}
PROJECT_CREATE = ('project.created', 'project_created')
PROJECT_DELETE = ('project.deleted', 'project_deleted')
TOOL_EVENTS = ('tool.completed', 'tool_call_completed')
SEARCH_EVENTS = ('search.performed',)
COMPILE_EVENTS = ('compile.completed',)
SIM_EVENTS = ('simulation.started',)
CHAT_EVENTS = ('chat.message_sent',)
COMMAND_EVENTS = ('command.completed',)
LOGIN_EVENTS = ('auth.login',)

EVENT_LABELS = {
    'auth.login': 'Signed in',
    'auth.logout': 'Signed out',
    'auth.invite_sent': 'Invite sent',
    'auth.access_requested': 'Access requested',
    'nav.page_view': 'Page view',
    'project.created': 'Project created',
    'project_created': 'Project created',
    'project.deleted': 'Project deleted',
    'project_deleted': 'Project deleted',
    'project.opened': 'Project opened',
    'project.changed': 'Project changed',
    'chat.message_sent': 'Chat message',
    'ai.run_started': 'AI run started',
    'ai_run_started': 'AI run started',
    'ai.run_completed': 'AI run completed',
    'ai.run_failed': 'AI run failed',
    'ai_run_completed': 'AI run completed',
    'ai_run_failed': 'AI run failed',
    'tool.completed': 'Agent tool call',
    'search.performed': 'Search',
    'search.result_opened': 'Search result opened',
    'compile.completed': 'Firmware compiled',
    'simulation.started': 'Simulation started',
    'simulation.stopped': 'Simulation stopped',
    'firmware.saved': 'Firmware saved',
    'command.completed': 'Manual command',
    'sample.opened': 'Example opened',
}


# ---------------------------------------------------------------------------
# small helpers


def _require_admin(request: Request):
    if not beta.enabled():
        host = request.client.host if request.client else ''
        local = host in ('localhost', 'testclient', 'testserver')
        try:
            local = local or bool(host and ipaddress.ip_address(host).is_loopback)
        except ValueError:
            pass
        if local:
            return {'id': None, 'email': 'local-admin', 'role': 'admin', 'workspace_id': None}
        raise HTTPException(403, 'Admin access is required.')
    actor = beta.identify(request)
    if not actor or actor.get('role') != 'admin':
        raise HTTPException(403, 'Admin access is required.')
    return actor


def _iso(value):
    if value is None or value == '':
        return None
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _day(value):
    if value is None or value == '':
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).date().isoformat()
        except ValueError:
            return value[:10] or None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _props(row):
    raw = row['properties'] if 'properties' in row.keys() else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {'value': value}
    except (TypeError, ValueError):
        return {}


def _row(row):
    return dict(row)


def _event(row, users=None, projects=None):
    item = _row(row)
    properties = _props(row)
    item['properties'] = properties
    item['occurred_at'] = _float(item.get('occurred_at'))
    item['occurred_at_iso'] = _iso(item.get('occurred_at'))
    item['label'] = EVENT_LABELS.get(item['name'], item['name'].replace('.', ' ').replace('_', ' ').title())
    if users is not None:
        owner = users.get(item.get('user_id'))
        item['email'] = owner['email'] if owner else (properties.get('email') if item.get('user_id') is None else None)
    if projects is not None and item.get('project_id'):
        item['project_name'] = projects.get(item['project_id'], {}).get('name')
    return item


def _avg(values):
    return round(mean(values), 1) if values else 0


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else 0


def _where_names(names):
    values = tuple(names)
    return '(' + ','.join('?' for _ in values) + ')', values


def _sql_names(names):
    return ','.join("'" + str(name).replace("'", "''") + "'" for name in names)


def _load_user_map(connection):
    return {row['id']: dict(row) for row in connection.execute('SELECT * FROM users')}


def _load_invites(connection):
    return {row['email']: dict(row) for row in connection.execute('SELECT * FROM invites')}


def _timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def _load_projects():
    projects = []
    if not PROJECT_DIR.is_dir():
        return projects
    for path in sorted(PROJECT_DIR.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get('id'):
            continue
        components = data.get('components') if isinstance(data.get('components'), list) else []
        wires = data.get('wires') if isinstance(data.get('wires'), list) else []
        firmware = data.get('firmware') if isinstance(data.get('firmware'), dict) else {}
        requirements = data.get('requirements') if isinstance(data.get('requirements'), dict) else {}
        compiler = data.get('compiler') if isinstance(data.get('compiler'), dict) else {}
        history = data.get('history') if isinstance(data.get('history'), list) else []
        projects.append({
            'id': data.get('id'),
            'name': data.get('name') or 'Untitled circuit',
            'board': data.get('board') or 'unselected',
            'workspace_id': data.get('workspace_id'),
            'revision': data.get('revision'),
            'created_at': data.get('created_at'),
            'updated_at': data.get('updated_at'),
            'components': len(components),
            'wires': len(wires),
            'component_types': list(dict.fromkeys(
                str(component.get('type')) for component in components if isinstance(component, dict) and component.get('type')
            )),
            'firmware_filename': firmware.get('filename'),
            'firmware_lines': len(str(firmware.get('source') or '').splitlines()),
            'firmware_source': str(firmware.get('source') or ''),
            'requirements_status': requirements.get('status') or 'unconfirmed',
            'requirements': requirements,
            'compiler_status': compiler.get('status') or 'not_compiled',
            'history': history[-40:],
            'updated_ts': _timestamp(data.get('updated_at')),
            'created_ts': _timestamp(data.get('created_at')),
        })
    return projects


def _project_activity(connection, since):
    rows = connection.execute(
        f'''SELECT project_id,
                   COUNT(*) AS events,
                   MAX(occurred_at) AS last_activity_at,
                   SUM(CASE WHEN name IN ({_sql_names(RUN_START)}) THEN 1 ELSE 0 END) AS runs,
                   SUM(CASE WHEN name IN ({_sql_names(RUN_END)}) THEN 1 ELSE 0 END) AS run_ends,
                   SUM(CASE WHEN name IN ({_sql_names(TOOL_EVENTS)}) THEN 1 ELSE 0 END) AS tool_calls,
                   SUM(CASE WHEN name IN ({_sql_names(SEARCH_EVENTS)}) THEN 1 ELSE 0 END) AS searches,
                   SUM(CASE WHEN name IN ({_sql_names(COMPILE_EVENTS)}) THEN 1 ELSE 0 END) AS compiles,
                   SUM(CASE WHEN name IN ({_sql_names(SIM_EVENTS)}) THEN 1 ELSE 0 END) AS simulations
            FROM events
            WHERE occurred_at>=? AND project_id IS NOT NULL
            GROUP BY project_id''',
        (since,),
    ).fetchall()
    return {row['project_id']: dict(row) for row in rows}


def _aggregate_events(connection, names, since, until=None):
    placeholders, values = _where_names(names)
    sql = f'SELECT * FROM events WHERE name IN {placeholders} AND occurred_at>=?'
    params = [*values, since]
    if until is not None:
        sql += ' AND occurred_at<?'
        params.append(until)
    sql += ' ORDER BY occurred_at DESC'
    return connection.execute(sql, params).fetchall()


def _sum_usage(properties):
    usage = properties.get('usage')
    if not isinstance(usage, dict):
        return 0
    return int(_float(usage.get('total_tokens')) or _float(usage.get('input_tokens')) + _float(usage.get('output_tokens')))


# ---------------------------------------------------------------------------
# overview


@router.get('/overview')
async def overview(request: Request, days: int = Query(14, ge=1, le=365)):
    _require_admin(request)
    now = time.time()
    since = now - days * 86400
    previous_since = since - days * 86400
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    with beta.db() as connection:
        users = _load_user_map(connection)
        invites = _load_invites(connection)
        projects = _load_projects()
        events = connection.execute(
            'SELECT * FROM events WHERE occurred_at>=? ORDER BY occurred_at DESC', (since,)
        ).fetchall()
        previous = connection.execute(
            'SELECT name, COUNT(*) AS count FROM events WHERE occurred_at>=? AND occurred_at<? GROUP BY name',
            (previous_since, since),
        ).fetchall()
        all_names = connection.execute(
            'SELECT name, COUNT(*) AS count FROM events GROUP BY name ORDER BY count DESC'
        ).fetchall()
        total_events = connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        total_runs = connection.execute(
            f"SELECT COUNT(*) FROM events WHERE name IN {_where_names(RUN_END)[0]}", _where_names(RUN_END)[1]
        ).fetchone()[0]
        access_requests = connection.execute(
            "SELECT COUNT(*) FROM events WHERE name='auth.access_requested'"
        ).fetchone()[0]
        recent_rows = connection.execute(
            'SELECT * FROM events ORDER BY occurred_at DESC LIMIT 30'
        ).fetchall()

    by_name = Counter(row['name'] for row in events)
    active_24h = sum(1 for user in users.values() if (user.get('last_seen_at') or 0) >= day_ago)
    active_7d = sum(1 for user in users.values() if (user.get('last_seen_at') or 0) >= week_ago)
    new_7d = sum(1 for user in users.values() if (user.get('created_at') or 0) >= week_ago)
    active_projects_7d = sum(1 for project in projects if project['updated_ts'] >= week_ago)
    run_ends = [row for row in events if row['name'] in RUN_END]
    run_errors = [row for row in run_ends if (_props(row).get('status') not in RUN_SUCCESS)]
    tool_calls = [row for row in events if row['name'] in TOOL_EVENTS]
    tool_errors = [row for row in tool_calls if not _props(row).get('success')]
    searches = [row for row in events if row['name'] in SEARCH_EVENTS]
    compiles = [row for row in events if row['name'] in COMPILE_EVENTS]
    compile_failures = [row for row in compiles if _props(row).get('status') not in ('simulation_ready', 'success')]
    simulations = [row for row in events if row['name'] in SIM_EVENTS]
    chats = [row for row in events if row['name'] in CHAT_EVENTS]

    # daily series ---------------------------------------------------------
    series = {}
    for offset in range(days):
        stamp = datetime.fromtimestamp(now - offset * 86400, tz=timezone.utc).date().isoformat()
        series[stamp] = {'date': stamp, 'events': 0, 'active_users': set(), 'new_users': 0,
                         'runs': 0, 'tool_calls': 0, 'searches': 0, 'compiles': 0,
                         'simulations': 0, 'chats': 0, 'projects': 0}
    for project in projects:
        stamp = _day(project.get('created_at'))
        if stamp in series:
            series[stamp]['projects'] += 1
    for user in users.values():
        stamp = _day(user.get('created_at'))
        if stamp in series:
            series[stamp]['new_users'] += 1
    for row in events:
        stamp = _day(row['occurred_at'])
        bucket = series.get(stamp)
        if not bucket:
            continue
        bucket['events'] += 1
        if row['user_id']:
            bucket['active_users'].add(row['user_id'])
        if row['name'] in RUN_START + RUN_END:
            bucket['runs'] += 1
        if row['name'] in TOOL_EVENTS:
            bucket['tool_calls'] += 1
        if row['name'] in SEARCH_EVENTS:
            bucket['searches'] += 1
        if row['name'] in COMPILE_EVENTS:
            bucket['compiles'] += 1
        if row['name'] in SIM_EVENTS:
            bucket['simulations'] += 1
        if row['name'] in CHAT_EVENTS:
            bucket['chats'] += 1
    day_series = []
    for stamp in sorted(series):
        bucket = series[stamp]
        bucket['active_users'] = len(bucket['active_users'])
        day_series.append(bucket)

    # user-based funnel ----------------------------------------------------
    # Use the event rows already loaded for the range, plus all-time project
    # ownership as a floor.
    project_owners = {project['workspace_id'] for project in projects if project.get('workspace_id')}
    range_user_sets = defaultdict(set)
    for row in events:
        if not row['user_id']:
            continue
        if row['name'] in PROJECT_CREATE:
            range_user_sets['project'].add(row['user_id'])
        if row['name'] in RUN_START:
            range_user_sets['run'].add(row['user_id'])
        if row['name'] in TOOL_EVENTS:
            range_user_sets['tool'].add(row['user_id'])
        if row['name'] in COMPILE_EVENTS:
            range_user_sets['compile'].add(row['user_id'])
        if row['name'] in SIM_EVENTS:
            range_user_sets['simulation'].add(row['user_id'])
    funnel = [
        {'stage': 'Invited', 'count': len(invites), 'hint': 'One-time links issued'},
        {'stage': 'Signed in', 'count': len(users), 'hint': 'Beta accounts created'},
        {'stage': 'Created a project', 'count': max(len(range_user_sets['project']), len(project_owners)), 'hint': 'Reached the canvas'},
        {'stage': 'Started an AI run', 'count': len(range_user_sets['run']), 'hint': 'Asked the agent'},
        {'stage': 'Used a tool', 'count': len(range_user_sets['tool']), 'hint': 'Agent took action'},
        {'stage': 'Compiled firmware', 'count': len(range_user_sets['compile']), 'hint': 'Reached real output'},
        {'stage': 'Ran simulation', 'count': len(range_user_sets['simulation']), 'hint': 'Ran the emulator'},
    ]

    # top tools ------------------------------------------------------------
    tools = defaultdict(lambda: {'name': '', 'calls': 0, 'errors': 0, 'durations': [], 'users': set(), 'last_at': 0})
    for row in tool_calls:
        properties = _props(row)
        name = properties.get('tool') or 'unknown'
        bucket = tools[name]
        bucket['name'] = name
        bucket['calls'] += 1
        bucket['errors'] += 0 if properties.get('success') else 1
        bucket['durations'].append(_float(properties.get('duration_ms')))
        if row['user_id']:
            bucket['users'].add(row['user_id'])
        bucket['last_at'] = max(bucket['last_at'], _float(row['occurred_at']))
    top_tools = sorted(tools.values(), key=lambda item: item['calls'], reverse=True)[:12]
    for item in top_tools:
        item['avg_ms'] = _avg(item.pop('durations'))
        item['users'] = len(item['users'])
        item['last_at'] = _iso(item['last_at'])

    # top searches ---------------------------------------------------------
    search_groups = defaultdict(lambda: {'query': '', 'scope': '', 'count': 0, 'users': set(),
                                         'results': [], 'zero': 0, 'last_at': 0})
    for row in searches:
        properties = _props(row)
        query = str(properties.get('query') or '').strip()
        if not query:
            continue
        scope = str(properties.get('scope') or 'catalog')
        key = (scope, query.lower())
        bucket = search_groups[key]
        bucket['query'] = query
        bucket['scope'] = scope
        bucket['count'] += 1
        bucket['results'].append(int(_float(properties.get('result_count'), 0)))
        bucket['zero'] += 1 if _float(properties.get('result_count'), 0) == 0 else 0
        if row['user_id']:
            bucket['users'].add(row['user_id'])
        bucket['last_at'] = max(bucket['last_at'], _float(row['occurred_at']))
    top_searches = sorted(search_groups.values(), key=lambda item: item['count'], reverse=True)[:12]
    for item in top_searches:
        item['users'] = len(item.pop('users'))
        item['avg_results'] = _avg(item.pop('results'))
        item['zero_results'] = item.pop('zero')
        item['last_at'] = _iso(item.pop('last_at'))

    # providers ------------------------------------------------------------
    provider_groups = defaultdict(lambda: {'provider': '', 'model': '', 'runs': 0, 'errors': 0,
                                           'durations': [], 'tokens': 0, 'tool_calls': 0})
    for row in run_ends:
        properties = _props(row)
        key = (str(properties.get('provider') or 'unknown'), str(properties.get('model') or 'unknown'))
        bucket = provider_groups[key]
        bucket['provider'] = key[0]
        bucket['model'] = key[1]
        bucket['runs'] += 1
        if properties.get('status') not in RUN_SUCCESS:
            bucket['errors'] += 1
        bucket['durations'].append(_float(properties.get('duration_ms')))
        bucket['tokens'] += _sum_usage(properties)
        bucket['tool_calls'] += int(_float(properties.get('tool_calls')))
    providers = sorted(provider_groups.values(), key=lambda item: item['runs'], reverse=True)
    for item in providers:
        item['avg_ms'] = _avg(item.pop('durations'))
        item['success_rate'] = _rate(item['runs'] - item['errors'], item['runs'])

    boards = Counter(project.get('board') or 'unselected' for project in projects)
    top_boards = [{'board': board, 'projects': count} for board, count in boards.most_common(12)]

    previous_counts = Counter(row['name'] for row in previous)
    project_names = {project['id']: project for project in projects}
    recent_events = [_event(row, users, project_names) for row in recent_rows]

    # Range totals with a prior-period comparison for the headline cards.
    def count(names):
        return sum(by_name.get(name, 0) for name in names)

    previous_totals = {
        'events': sum(previous_counts.values()),
        'runs': sum(previous_counts.get(name, 0) for name in RUN_END),
        'tool_calls': sum(previous_counts.get(name, 0) for name in TOOL_EVENTS),
        'searches': sum(previous_counts.get(name, 0) for name in SEARCH_EVENTS),
        'chats': sum(previous_counts.get(name, 0) for name in CHAT_EVENTS),
        'compiles': sum(previous_counts.get(name, 0) for name in COMPILE_EVENTS),
    }

    return {
        'generated_at': _iso(now),
        'range_days': days,
        'totals': {
            'users': len(users),
            'admins': sum(1 for user in users.values() if user.get('role') == 'admin'),
            'invites': len(invites),
            'accepted_invites': sum(1 for invite in invites.values() if invite.get('accepted_at')),
            'access_requests': access_requests,
            'active_24h': active_24h,
            'active_7d': active_7d,
            'new_users_7d': new_7d,
            'projects': len(projects),
            'active_projects_7d': active_projects_7d,
            'events': sum(by_name.values()),
            'events_total': total_events,
            'runs': len(run_ends),
            'run_errors': len(run_errors),
            'runs_total': total_runs,
            'tool_calls': len(tool_calls),
            'tool_errors': len(tool_errors),
            'searches': len(searches),
            'unique_searches': len(search_groups),
            'compiles': len(compiles),
            'compile_failures': len(compile_failures),
            'simulations': len(simulations),
            'chats': len(chats),
            'avg_run_ms': _avg([_float(_props(row).get('duration_ms')) for row in run_ends]),
            'avg_tool_ms': _avg([_float(_props(row).get('duration_ms')) for row in tool_calls]),
        },
        'previous': previous_totals,
        'series': day_series,
        'funnel': funnel,
        'top_tools': top_tools,
        'top_searches': top_searches,
        'providers': providers,
        'boards': top_boards,
        'recent_events': recent_events,
        'event_names': [{'name': row['name'], 'label': EVENT_LABELS.get(row['name'], row['name']), 'count': row['count']} for row in all_names],
    }


# ---------------------------------------------------------------------------
# testers


@router.get('/users')
async def users(request: Request, q: str = '', limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    _require_admin(request)
    with beta.db() as connection:
        user_map = _load_user_map(connection)
        invites = _load_invites(connection)
        projects = _load_projects()
        rows = connection.execute(
            '''SELECT user_id, COUNT(*) AS events, MAX(occurred_at) AS last_event_at,
                      SUM(CASE WHEN name IN ('ai.run_started','ai_run_started') THEN 1 ELSE 0 END) AS runs,
                      SUM(CASE WHEN name IN ('tool.completed','tool_call_completed') THEN 1 ELSE 0 END) AS tool_calls,
                      SUM(CASE WHEN name='search.performed' THEN 1 ELSE 0 END) AS searches,
                      SUM(CASE WHEN name='compile.completed' THEN 1 ELSE 0 END) AS compiles,
                      SUM(CASE WHEN name='simulation.started' THEN 1 ELSE 0 END) AS simulations,
                      SUM(CASE WHEN name='chat.message_sent' THEN 1 ELSE 0 END) AS messages,
                      SUM(CASE WHEN name='tool.completed' AND json_extract(properties, '$.success')=0 THEN 1 ELSE 0 END) AS tool_errors,
                      SUM(CASE WHEN name IN ('ai.run_failed','ai_run_failed') THEN 1 ELSE 0 END) AS run_errors
               FROM events WHERE user_id IS NOT NULL GROUP BY user_id'''
        ).fetchall()
        first_seen = {
            row['user_id']: row['first_at'] for row in connection.execute(
                'SELECT user_id, MIN(occurred_at) AS first_at FROM events WHERE user_id IS NOT NULL GROUP BY user_id'
            )
        }
        last_event = {row['user_id']: row['last_event_at'] for row in rows}
    event_map = {row['user_id']: dict(row) for row in rows}
    projects_by_workspace = defaultdict(list)
    for project in projects:
        projects_by_workspace[project.get('workspace_id')].append(project)

    needle = q.strip().lower()
    result = []
    for user in user_map.values():
        if needle and needle not in user['email'].lower():
            continue
        stats = event_map.get(user['id'], {})
        owned = projects_by_workspace.get(user.get('workspace_id'), [])
        invite = invites.get(user['email'], {})
        result.append({
            'id': user['id'],
            'email': user['email'],
            'role': user['role'],
            'workspace_id': user['workspace_id'],
            'created_at': _iso(user.get('created_at')),
            'created_ts': _float(user.get('created_at')),
            'last_seen_at': _iso(user.get('last_seen_at')),
            'last_seen_ts': _float(user.get('last_seen_at')),
            'last_event_at': _iso(last_event.get(user['id'])),
            'first_event_at': _iso(first_seen.get(user['id'])),
            'invited_at': _iso(invite.get('created_at')),
            'accepted_at': _iso(invite.get('accepted_at')),
            'projects': len(owned),
            'boards': sorted({project.get('board') for project in owned if project.get('board')}),
            'events': int(stats.get('events') or 0),
            'runs': int(stats.get('runs') or 0),
            'tool_calls': int(stats.get('tool_calls') or 0),
            'searches': int(stats.get('searches') or 0),
            'compiles': int(stats.get('compiles') or 0),
            'simulations': int(stats.get('simulations') or 0),
            'messages': int(stats.get('messages') or 0),
            'tool_errors': int(stats.get('tool_errors') or 0),
            'run_errors': int(stats.get('run_errors') or 0),
        })
    result.sort(key=lambda item: (item['last_seen_ts'] or 0, item['last_event_at'] or ''), reverse=True)
    total = len(result)
    return {'users': result[offset:offset + limit], 'total': total}


@router.get('/users/{user_id}')
async def user_detail(request: Request, user_id: str, days: int = Query(30, ge=1, le=365)):
    _require_admin(request)
    since = time.time() - days * 86400
    with beta.db() as connection:
        user_row = connection.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if user_row is None:
            raise HTTPException(404, 'Tester not found.')
        user = dict(user_row)
        invite_row = connection.execute('SELECT * FROM invites WHERE email=?', (user['email'],)).fetchone()
        invites = {user['email']: dict(invite_row)} if invite_row else {}
        projects = [project for project in _load_projects() if project.get('workspace_id') == user.get('workspace_id')]
        event_rows = connection.execute(
            'SELECT * FROM events WHERE user_id=? ORDER BY occurred_at DESC LIMIT 250', (user_id,)
        ).fetchall()
        session_rows = connection.execute(
            'SELECT created_at, expires_at FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 20', (user_id,)
        ).fetchall()
    project_names = {project['id']: project for project in projects}
    all_users = {user_id: user}
    event_items = [_event(row, all_users, project_names) for row in event_rows]
    tools = defaultdict(lambda: {'name': '', 'calls': 0, 'errors': 0, 'durations': []})
    searches = Counter()
    for row in event_rows:
        properties = _props(row)
        if row['name'] in TOOL_EVENTS:
            name = properties.get('tool') or 'unknown'
            tools[name]['name'] = name
            tools[name]['calls'] += 1
            tools[name]['errors'] += 0 if properties.get('success') else 1
            tools[name]['durations'].append(_float(properties.get('duration_ms')))
        if row['name'] in SEARCH_EVENTS and properties.get('query'):
            searches[str(properties['query'])] += 1
    tool_stats = []
    for item in tools.values():
        item['avg_ms'] = _avg(item.pop('durations'))
        tool_stats.append(item)
    tool_stats.sort(key=lambda item: item['calls'], reverse=True)
    days_series = defaultdict(lambda: {'date': '', 'events': 0, 'runs': 0, 'tool_calls': 0, 'searches': 0})
    for row in event_rows:
        stamp = _day(row['occurred_at'])
        if not stamp:
            continue
        bucket = days_series[stamp]
        bucket['date'] = stamp
        bucket['events'] += 1
        bucket['runs'] += 1 if row['name'] in RUN_START + RUN_END else 0
        bucket['tool_calls'] += 1 if row['name'] in TOOL_EVENTS else 0
        bucket['searches'] += 1 if row['name'] in SEARCH_EVENTS else 0
    project_items = []
    for project in projects:
        item = {key: project[key] for key in ('id', 'name', 'board', 'revision', 'created_at', 'updated_at',
                                              'components', 'wires', 'firmware_lines', 'requirements_status',
                                              'compiler_status', 'component_types')}
        project_items.append(item)
    return {
        'user': {
            **{key: user[key] for key in ('id', 'email', 'role', 'workspace_id')},
            'created_at': _iso(user.get('created_at')),
            'last_seen_at': _iso(user.get('last_seen_at')),
            'invite': {key: _iso(invite_row[key]) if key.endswith('_at') else invite_row[key]
                       for key in invite_row.keys()} if invite_row else None,
        },
        'projects': project_items,
        'events': event_items,
        'tools': tool_stats,
        'searches': [{'query': query, 'count': count} for query, count in searches.most_common(25)],
        'activity': sorted(days_series.values(), key=lambda item: item['date']),
        'auth_sessions': [{'created_at': _iso(row['created_at']), 'expires_at': _iso(row['expires_at']),
                           'active': _float(row['expires_at']) > time.time()} for row in session_rows],
        'range_days': days,
        'since': _iso(since),
    }


# ---------------------------------------------------------------------------
# projects


@router.get('/projects')
async def projects(request: Request, q: str = '', board: str = '', limit: int = Query(200, ge=1, le=1000),
                   offset: int = Query(0, ge=0)):
    _require_admin(request)
    since = time.time() - 365 * 86400
    with beta.db() as connection:
        users = _load_user_map(connection)
        workspaces = {user['workspace_id']: user for user in users.values()}
        project_rows = _load_projects()
        activity = _project_activity(connection, since)
    needle = q.strip().lower()
    result = []
    for project in project_rows:
        if board and project.get('board') != board:
            continue
        if needle and needle not in f"{project.get('name','')} {project.get('board','')} {project.get('id','')}".lower():
            continue
        owner = workspaces.get(project.get('workspace_id'))
        stats = activity.get(project['id'], {})
        item = {
            key: project[key] for key in ('id', 'name', 'board', 'workspace_id', 'revision', 'created_at',
                                          'updated_at', 'components', 'wires', 'component_types',
                                          'firmware_filename', 'firmware_lines', 'requirements_status',
                                          'compiler_status')
        }
        item.update({
            'owner_email': owner['email'] if owner else None,
            'owner_id': owner['id'] if owner else None,
            'events': int(stats.get('events') or 0),
            'runs': int(stats.get('runs') or 0),
            'tool_calls': int(stats.get('tool_calls') or 0),
            'searches': int(stats.get('searches') or 0),
            'compiles': int(stats.get('compiles') or 0),
            'simulations': int(stats.get('simulations') or 0),
            'last_activity_at': _iso(stats.get('last_activity_at')) or project.get('updated_at'),
            'last_activity_ts': _float(stats.get('last_activity_at')),
        })
        result.append(item)
    result.sort(key=lambda item: item['last_activity_ts'] or item.get('updated_at') or '', reverse=True)
    total = len(result)
    boards = Counter(project.get('board') for project in project_rows)
    return {'projects': result[offset:offset + limit], 'total': total,
            'boards': [{'board': name, 'projects': count} for name, count in boards.most_common()]}


@router.get('/projects/{project_id}')
async def project_detail(request: Request, project_id: str):
    _require_admin(request)
    with beta.db() as connection:
        users = _load_user_map(connection)
        workspaces = {user['workspace_id']: user for user in users.values()}
        project = next((item for item in _load_projects() if item['id'] == project_id), None)
        if project is None:
            raise HTTPException(404, 'Project not found.')
        event_rows = connection.execute(
            'SELECT * FROM events WHERE project_id=? ORDER BY occurred_at DESC LIMIT 150', (project_id,)
        ).fetchall()
    owner = workspaces.get(project.get('workspace_id'))
    project_names = {project['id']: project}
    detail = {key: project[key] for key in project.keys() if key != 'firmware_source'}
    detail['owner_email'] = owner['email'] if owner else None
    detail['owner_id'] = owner['id'] if owner else None
    detail['firmware_source'] = project.get('firmware_source', '')[:30000]
    detail['events'] = [_event(row, {owner['id']: owner} if owner else {}, project_names) for row in event_rows]
    return detail


# ---------------------------------------------------------------------------
# event stream


@router.get('/events')
async def event_stream(request: Request, user_id: str = '', project_id: str = '', name: str = '',
                       q: str = '', days: int = Query(30, ge=1, le=365),
                       limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    _require_admin(request)
    since = time.time() - days * 86400
    clauses = ['occurred_at>=?']
    params = [since]
    if user_id:
        clauses.append('user_id=?')
        params.append(user_id)
    if project_id:
        clauses.append('project_id=?')
        params.append(project_id)
    if name:
        clauses.append('name=?')
        params.append(name)
    if q.strip():
        clauses.append('(name LIKE ? OR properties LIKE ? OR project_id LIKE ?)')
        needle = f'%{q.strip()}%'
        params.extend([needle, needle, needle])
    where = ' AND '.join(clauses)
    with beta.db() as connection:
        users = _load_user_map(connection)
        projects = {project['id']: project for project in _load_projects()}
        total = connection.execute(f'SELECT COUNT(*) FROM events WHERE {where}', params).fetchone()[0]
        rows = connection.execute(
            f'SELECT * FROM events WHERE {where} ORDER BY occurred_at DESC LIMIT ? OFFSET ?',
            (*params, limit, offset),
        ).fetchall()
        names = connection.execute('SELECT name, COUNT(*) AS count FROM events GROUP BY name ORDER BY count DESC').fetchall()
    return {
        'events': [_event(row, users, projects) for row in rows],
        'total': total,
        'offset': offset,
        'limit': limit,
        'names': [{'name': row['name'], 'label': EVENT_LABELS.get(row['name'], row['name']), 'count': row['count']} for row in names],
    }


# ---------------------------------------------------------------------------
# AI runs


@router.get('/runs')
async def runs(request: Request, days: int = Query(30, ge=1, le=365), status: str = '',
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    _require_admin(request)
    since = time.time() - days * 86400
    with beta.db() as connection:
        users = _load_user_map(connection)
        projects = {project['id']: project for project in _load_projects()}
        rows = connection.execute(
            f'''SELECT * FROM events
                WHERE occurred_at>=? AND name IN {_where_names(RUN_START + RUN_END)[0]}
                ORDER BY occurred_at ASC''', (since, *_where_names(RUN_START + RUN_END)[1])
        ).fetchall()
    runs_by_id = {}
    for row in rows:
        properties = _props(row)
        run_id = row['run_id'] or properties.get('run_id') or row['id']
        item = runs_by_id.setdefault(run_id, {
            'run_id': run_id,
            'user_id': row['user_id'],
            'email': users.get(row['user_id'], {}).get('email'),
            'project_id': row['project_id'],
            'project_name': projects.get(row['project_id'], {}).get('name'),
            'chat_id': row['session_id'],
            'started_at': None,
            'completed_at': None,
            'status': 'incomplete',
            'reason': None,
            'provider': None,
            'model': None,
            'duration_ms': 0,
            'tool_calls': 0,
            'model_calls': 0,
            'tokens': 0,
            'prompt_preview': '',
            'prompt_length': 0,
        })
        if row['name'] in RUN_START:
            item['started_at'] = _iso(row['occurred_at'])
            item['provider'] = properties.get('provider') or item['provider']
            item['model'] = properties.get('model') or item['model']
            item['prompt_preview'] = str(properties.get('prompt_preview') or '')[:500]
            item['prompt_length'] = int(_float(properties.get('prompt_length')))
            if not item['project_id']:
                item['project_id'] = row['project_id']
            if not item['chat_id']:
                item['chat_id'] = row['session_id']
        else:
            item['completed_at'] = _iso(row['occurred_at'])
            item['status'] = str(properties.get('status') or 'success')
            item['reason'] = properties.get('reason')
            item['provider'] = properties.get('provider') or item['provider']
            item['model'] = properties.get('model') or item['model']
            item['duration_ms'] = int(_float(properties.get('duration_ms')))
            item['tool_calls'] = int(_float(properties.get('tool_calls')))
            item['model_calls'] = int(_float(properties.get('model_calls')))
            item['tokens'] = _sum_usage(properties)
            if not item['started_at'] and item['duration_ms']:
                item['started_at'] = _iso(_float(row['occurred_at']) - item['duration_ms'] / 1000)
    items = list(runs_by_id.values())
    # A start with no terminal event is useful too: show it as running/incomplete.
    for item in items:
        if item['status'] == 'incomplete' and item['started_at']:
            try:
                started = datetime.fromisoformat(item['started_at']).timestamp()
                if time.time() - started < 900:
                    item['status'] = 'running'
            except ValueError:
                pass
    if status:
        items = [item for item in items if item['status'] == status]
    items.sort(key=lambda item: item['started_at'] or '', reverse=True)
    total = len(items)
    summary = {
        'total': total,
        'success': sum(1 for item in items if item['status'] == 'success'),
        'errors': sum(1 for item in items if item['status'] not in ('success', 'running', 'cancelled')),
        'running': sum(1 for item in items if item['status'] == 'running'),
        'avg_ms': _avg([item['duration_ms'] for item in items if item['duration_ms']]),
        'tokens': sum(item['tokens'] for item in items),
    }
    return {'runs': items[offset:offset + limit], 'total': total, 'summary': summary}


# ---------------------------------------------------------------------------
# tools and searches


@router.get('/tools')
async def tools(request: Request, days: int = Query(30, ge=1, le=365), source: str = '',
                limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    _require_admin(request)
    since = time.time() - days * 86400
    with beta.db() as connection:
        users = _load_user_map(connection)
        projects = {project['id']: project for project in _load_projects()}
        rows = _aggregate_events(connection, TOOL_EVENTS, since)
        commands = _aggregate_events(connection, COMMAND_EVENTS, since)
    grouped = defaultdict(lambda: {'name': '', 'calls': 0, 'errors': 0, 'durations': [], 'users': set(),
                                   'last_at': 0, 'sources': Counter()})
    recent = []
    for row in rows:
        properties = _props(row)
        name = properties.get('tool') or 'unknown'
        if source and (row['source'] or 'unknown') != source:
            continue
        bucket = grouped[name]
        bucket['name'] = name
        bucket['calls'] += 1
        bucket['errors'] += 0 if properties.get('success') else 1
        bucket['durations'].append(_float(properties.get('duration_ms')))
        bucket['sources'][row['source'] or 'unknown'] += 1
        if row['user_id']:
            bucket['users'].add(row['user_id'])
        bucket['last_at'] = max(bucket['last_at'], _float(row['occurred_at']))
        recent.append(_event(row, users, projects))
    tool_rows = []
    for item in grouped.values():
        item['avg_ms'] = _avg(item.pop('durations'))
        item['users'] = len(item.pop('users'))
        item['last_at'] = _iso(item.pop('last_at'))
        item['sources'] = dict(item['sources'])
        item['success_rate'] = _rate(item['calls'] - item['errors'], item['calls'])
        tool_rows.append(item)
    tool_rows.sort(key=lambda item: item['calls'], reverse=True)
    command_groups = defaultdict(lambda: {'command': '', 'calls': 0, 'errors': 0, 'durations': [], 'users': set(), 'last_at': 0})
    command_recent = []
    for row in commands:
        properties = _props(row)
        name = properties.get('command') or 'unknown'
        bucket = command_groups[name]
        bucket['command'] = name
        bucket['calls'] += 1
        bucket['errors'] += 0 if properties.get('success') else 1
        bucket['durations'].append(_float(properties.get('duration_ms')))
        if row['user_id']:
            bucket['users'].add(row['user_id'])
        bucket['last_at'] = max(bucket['last_at'], _float(row['occurred_at']))
        command_recent.append(_event(row, users, projects))
    commands_out = []
    for item in command_groups.values():
        item['avg_ms'] = _avg(item.pop('durations'))
        item['users'] = len(item.pop('users'))
        item['last_at'] = _iso(item.pop('last_at'))
        item['success_rate'] = _rate(item['calls'] - item['errors'], item['calls'])
        commands_out.append(item)
    commands_out.sort(key=lambda item: item['calls'], reverse=True)
    recent = [item for item in recent if not source or item.get('source') == source]
    sources = Counter(row['source'] or 'unknown' for row in rows)
    return {
        'tools': tool_rows,
        'recent': recent[offset:offset + limit],
        'recent_total': len(recent),
        'manual_commands': commands_out,
        'manual_recent': command_recent[offset:offset + limit],
        'manual_total': len(command_recent),
        'sources': [{'source': name, 'calls': count} for name, count in sources.most_common()],
    }


@router.get('/searches')
async def searches(request: Request, days: int = Query(30, ge=1, le=365), scope: str = '',
                   limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    _require_admin(request)
    since = time.time() - days * 86400
    with beta.db() as connection:
        users = _load_user_map(connection)
        projects = {project['id']: project for project in _load_projects()}
        rows = _aggregate_events(connection, SEARCH_EVENTS, since)
    grouped = defaultdict(lambda: {'query': '', 'scope': '', 'count': 0, 'users': set(), 'results': [],
                                   'zero': 0, 'last_at': 0})
    recent = []
    scopes = Counter()
    for row in rows:
        properties = _props(row)
        query = str(properties.get('query') or '').strip()
        item_scope = str(properties.get('scope') or 'catalog')
        if not query:
            continue
        scopes[item_scope] += 1
        if scope and item_scope != scope:
            continue
        key = (item_scope, query.lower())
        bucket = grouped[key]
        bucket['query'] = query
        bucket['scope'] = item_scope
        bucket['count'] += 1
        bucket['results'].append(int(_float(properties.get('result_count'), 0)))
        bucket['zero'] += 1 if _float(properties.get('result_count'), 0) == 0 else 0
        if row['user_id']:
            bucket['users'].add(row['user_id'])
        bucket['last_at'] = max(bucket['last_at'], _float(row['occurred_at']))
        recent.append(_event(row, users, projects))
    out = []
    for item in grouped.values():
        item['users'] = len(item.pop('users'))
        item['avg_results'] = _avg(item.pop('results'))
        item['zero_results'] = item.pop('zero')
        item['last_at'] = _iso(item.pop('last_at'))
        out.append(item)
    out.sort(key=lambda item: item['count'], reverse=True)
    return {'queries': out[offset:offset + limit], 'total': len(out), 'recent': recent[offset:offset + limit],
            'scopes': [{'scope': name, 'count': count} for name, count in scopes.most_common()]}


# ---------------------------------------------------------------------------
# waitlist


@router.get('/waitlist')
async def waitlist(request: Request):
    _require_admin(request)
    with beta.db() as connection:
        invites = _load_invites(connection)
        rows = connection.execute(
            "SELECT * FROM events WHERE name='auth.access_requested' ORDER BY occurred_at DESC LIMIT 2000"
        ).fetchall()
    leads = {}
    for row in rows:
        properties = _props(row)
        email = str(properties.get('email') or '').strip().lower()
        if not email or email in invites:
            continue
        lead = leads.setdefault(email, {
            'email': email,
            'requests': 0,
            'first_requested_at': _iso(row['occurred_at']),
            'last_requested_at': _iso(row['occurred_at']),
            'delivered': False,
            'user_agent': properties.get('user_agent') or '',
        })
        lead['requests'] += 1
        lead['last_requested_at'] = _iso(row['occurred_at'])
        lead['delivered'] = bool(properties.get('delivered')) or lead['delivered']
    items = sorted(leads.values(), key=lambda item: item['last_requested_at'] or '', reverse=True)
    invited = sorted(
        ({'email': invite['email'], 'created_at': _iso(invite.get('created_at')),
          'accepted_at': _iso(invite.get('accepted_at')), 'invited_by': invite.get('invited_by')}
         for invite in invites.values()),
        key=lambda item: item['created_at'] or '', reverse=True,
    )
    return {'waitlist': items, 'total': len(items), 'invites': invited, 'invited_total': len(invited)}
