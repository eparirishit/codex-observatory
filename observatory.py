#!/usr/bin/env python3
"""Read-only Codex log metrics. No dependencies, uploads, or disk database."""

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import tomllib
from urllib.parse import urlsplit

TOKEN_KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
              'output_tokens', 'reasoning_output_tokens', 'total_tokens')
MAX_LINE = 16 * 1024 * 1024
ASSETS = Path(__file__).parent / 'static'


def object_value(value):
    return value if isinstance(value, dict) else {}


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 2 ** 53 - 1 and math.isfinite(value):
        return value
    return None


def label(value, fallback='unavailable'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', value):
        return fallback
    if re.search(r'(sk-|Bearer|token=|secret|password)', value, re.I):
        return fallback
    return value


def identifier(value):
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def argument_fingerprint(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return identifier(json.dumps(value, sort_keys=True, separators=(',', ':')))
    return identifier(value)


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except ValueError:
        return None


def empty_usage():
    return dict.fromkeys(TOKEN_KEYS)


def usage_values(value):
    value = object_value(value)
    return {key: number(value.get(key)) for key in TOKEN_KEYS}


def add_usage(target, usage):
    for key in TOKEN_KEYS:
        if usage.get(key) is not None:
            target[key] = (target[key] or 0) + usage[key]


def seconds_between(start, end):
    if start and end:
        return max(0, (dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)).total_seconds())
    return None


def snapshot_limits(value, at):
    limits = object_value(value)
    if not limits:
        return None
    result = {'at': at, 'limit_id': label(limits.get('limit_id'), 'unspecified')}
    for key in ('primary', 'secondary'):
        window = object_value(limits.get(key))
        if window:
            used = number(window.get('used_percent'))
            result[key] = {'used_percent': used if used is not None and used <= 100 else None,
                           'window_minutes': number(window.get('window_minutes')),
                           'resets_at': number(window.get('resets_at'))}
    return result if 'primary' in result or 'secondary' in result else None


def read_config(root):
    result = {'status': 'unavailable', 'mcp_servers': [], 'plugin_count': None}
    try:
        with (root / 'config.toml').open('rb') as stream:
            config = tomllib.load(stream)
        result.update(status='measured', model=label(config.get('model')),
                      reasoning_effort=label(config.get('model_reasoning_effort')),
                      service_tier=label(config.get('service_tier')))
        result['mcp_servers'] = [{'name': label(name, 'redacted'),
                                 'enabled': settings.get('enabled', True) is not False}
                                for name, settings in object_value(config.get('mcp_servers')).items()
                                if isinstance(settings, dict)]
        result['plugin_count'] = len(object_value(config.get('plugins')))
    except (OSError, ValueError):
        pass
    return result


def parse_session(path):
    session = {'id': identifier(str(path)), 'parent_id': None, 'kind': 'main',
               'started_at': None, 'updated_at': None, 'models': [], 'turns': [],
               'calls': [], 'compactions': [], 'rate_limits': [], 'context': [],
               'usage_source': 'unavailable', 'usage': empty_usage(),
               'reported_thread_usage': empty_usage(), 'warnings': [],
               'invalid_lines': 0, 'unknown_events': 0, 'counter_resets': 0,
               'duplicate_usage_records': 0, 'historical_baseline': empty_usage()}
    turns = {}
    records = []
    fallback_records = []
    seen_records = set()
    seen_items = set()
    outer_calls = {}
    output_events = []
    current_turn = None
    current_model = 'unavailable'
    current_effort = 'unavailable'
    prior_total = None
    context_window = None
    inherited = False
    repeat_fingerprints = {}

    def turn_for(raw_id=None, at=None):
        nonlocal current_turn
        turn_id = identifier(raw_id) or current_turn or 'unassigned'
        if turn_id not in turns:
            turns[turn_id] = {'id': turn_id, 'number': len(turns) + 1,
                             'started_at': at, 'ended_at': None, 'status': 'unavailable',
                             'model': current_model, 'effort': current_effort,
                             'usage': empty_usage(), 'responses': 0, 'tool_calls': 0,
                             'repeated_calls': 0, 'failed_calls': 0, 'compactions': 0}
        return turns[turn_id]

    def add_call(name, category, at, turn_id, status='unavailable', duration_ms=None,
                 output_bytes=None, fingerprint=None, item_id=None):
        repeated = False
        if fingerprint:
            repeat_key = (turn_id, name, fingerprint)
            repeated = repeat_key in repeat_fingerprints
            repeat_fingerprints[repeat_key] = True
        call = {'at': at, 'turn_id': turn_id, 'name': label(name, 'redacted'),
                'category': category, 'status': status, 'duration_ms': duration_ms,
                'output_bytes': output_bytes, 'repeat_candidate': repeated,
                'id': item_id}
        session['calls'].append(call)
        return call

    with path.open('rb') as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                while line and not line.endswith(b'\n'):
                    line = stream.readline(MAX_LINE + 1)
                session['invalid_lines'] += 1
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                session['invalid_lines'] += 1
                continue
            if not isinstance(event, dict):
                session['invalid_lines'] += 1
                continue
            payload = object_value(event.get('payload'))
            kind = event.get('type')
            at = timestamp(event.get('timestamp'))
            session['started_at'] = session['started_at'] or at
            session['updated_at'] = at or session['updated_at']
            subtype = payload.get('type')
            if kind == 'session_meta':
                session['id'] = identifier(payload.get('id')) or session['id']
                session['started_at'] = timestamp(payload.get('timestamp')) or session['started_at']
                source = object_value(payload.get('source'))
                subagent = object_value(source.get('subagent'))
                spawn = object_value(subagent.get('thread_spawn'))
                session['parent_id'] = identifier(payload.get('parent_thread_id') or spawn.get('parent_thread_id'))
                if subagent or session['parent_id']:
                    session['kind'] = 'subagent'
                inherited = bool(payload.get('history_base'))
                session['originator'] = label(payload.get('originator'), 'redacted')
                session['log_version'] = label(payload.get('cli_version'))
            elif kind == 'turn_context':
                current_model = label(payload.get('model'))
                current_effort = label(payload.get('effort') or payload.get('reasoning_effort'))
                current_turn = identifier(payload.get('turn_id')) or current_turn
                turn = turn_for(payload.get('turn_id'), at)
                turn.update(model=current_model, effort=current_effort)
            elif kind == 'token_usage_record':
                response_key = identifier(payload.get('response_id'))
                if response_key and response_key in seen_records:
                    session['duplicate_usage_records'] += 1
                    continue
                if response_key:
                    seen_records.add(response_key)
                usage = usage_values(payload.get('usage'))
                if all(value is None for value in usage.values()):
                    continue
                turn = turn_for(payload.get('turn_id'), at)
                records.append({'id': response_key, 'at': at, 'turn_id': turn['id'], 'usage': usage,
                                'model': turn['model'], 'effort': turn['effort'], 'context_window': context_window})
                session['reported_thread_usage'] = usage_values(payload.get('thread_token_usage'))
            elif kind == 'event_msg':
                if subtype == 'task_started':
                    current_turn = identifier(payload.get('turn_id')) or current_turn
                    turn = turn_for(payload.get('turn_id'), at)
                    turn.update(started_at=timestamp(payload.get('started_at')) or at, status='running')
                    context_window = number(payload.get('model_context_window')) or context_window
                elif subtype in ('task_complete', 'task_aborted'):
                    turn = turn_for(payload.get('turn_id'), at)
                    turn.update(ended_at=timestamp(payload.get('completed_at')) or at,
                                status='failed' if payload.get('error') else 'aborted' if subtype == 'task_aborted' else 'completed')
                elif subtype == 'token_count':
                    info = object_value(payload.get('info'))
                    limits = snapshot_limits(payload.get('rate_limits'), at)
                    if limits and (not session['rate_limits'] or limits != session['rate_limits'][-1]):
                        session['rate_limits'].append(limits)
                    context_window = number(info.get('model_context_window')) or context_window
                    total = usage_values(info.get('total_token_usage'))
                    last = usage_values(info.get('last_token_usage'))
                    if total['total_tokens'] is None:
                        continue
                    turn = turn_for(at=at)
                    if prior_total is None:
                        if inherited or total != last:
                            session['historical_baseline'] = total
                        else:
                            fallback_records.append({'id': None, 'at': at, 'turn_id': turn['id'],
                                                     'usage': total, 'model': turn['model'], 'effort': turn['effort']})
                    elif total['total_tokens'] > prior_total['total_tokens']:
                        delta = {key: total[key] - prior_total[key] if total[key] is not None and prior_total[key] is not None and total[key] >= prior_total[key] else None for key in TOKEN_KEYS}
                        fallback_records.append({'id': None, 'at': at, 'turn_id': turn['id'],
                                                 'usage': delta, 'model': turn['model'], 'effort': turn['effort']})
                    elif total['total_tokens'] < prior_total['total_tokens']:
                        session['counter_resets'] += 1
                        session['warnings'].append('Cumulative counters decreased; the reset sample is excluded from fallback accounting.')
                    prior_total = total
                    if not records:
                        session['reported_thread_usage'] = total
                    if last['input_tokens'] is not None:
                        point = {'at': at, 'turn_id': turn['id'], 'input_tokens': last['input_tokens'],
                                 'context_window': context_window, 'source': 'last_token_usage'}
                        if not session['context'] or point['input_tokens'] != session['context'][-1]['input_tokens'] or point['turn_id'] != session['context'][-1]['turn_id']:
                            session['context'].append(point)
                elif subtype == 'item_completed':
                    item = object_value(payload.get('item'))
                    item_type = label(item.get('type'))
                    item_id = identifier(item.get('id'))
                    dedup_key = (item_type, item_id)
                    if item_id and dedup_key in seen_items:
                        continue
                    seen_items.add(dedup_key)
                    turn = turn_for(payload.get('turn_id'), at)
                    start_ms = number(payload.get('started_at_ms'))
                    end_ms = number(payload.get('completed_at_ms'))
                    duration = max(0, end_ms - start_ms) if start_ms is not None and end_ms is not None else None
                    status = item.get('status')
                    status = status if status in ('completed', 'failed', 'in_progress', 'cancelled') else 'unavailable'
                    if item_type == 'CommandExecution':
                        exit_code = item.get('exit_code')
                        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                            status = 'completed' if exit_code == 0 else 'failed'
                        output = item.get('aggregated_output')
                        output_size = len(output.encode()) if isinstance(output, str) else None
                        fingerprint = argument_fingerprint(item.get('command'))
                        add_call('shell.command', 'shell', at, turn['id'], status, duration, output_size, fingerprint, item_id)
                    elif item_type == 'McpToolCall':
                        name = label(item.get('server'), 'redacted') + '/' + label(item.get('tool'), 'redacted')
                        result = object_value(item.get('result'))
                        if result.get('isError') is True or item.get('error'):
                            status = 'failed'
                        add_call(name, 'mcp', at, turn['id'], status, duration,
                                 fingerprint=argument_fingerprint(item.get('arguments')), item_id=item_id)
                    elif item_type in ('FileChange', 'ImageView', 'Extension'):
                        add_call({'FileChange': 'file.change', 'ImageView': 'image.view', 'Extension': 'extension.' + label(item.get('kind'), 'unknown')}[item_type],
                                 'local' if item_type != 'Extension' else 'extension', at, turn['id'], status, duration, item_id=item_id)
                    elif item_type == 'SubAgentActivity':
                        add_call('subagent.' + label(item.get('kind'), 'activity'), 'subagent', at, turn['id'], item_id=item_id)
                elif subtype == 'thread_settings_applied':
                    settings = object_value(payload.get('thread_settings'))
                    current_model = label(settings.get('model'), current_model)
                    current_effort = label(settings.get('reasoning_effort'), current_effort)
            elif kind == 'response_item':
                metadata = object_value(payload.get('internal_chat_message_metadata_passthrough'))
                turn = turn_for(metadata.get('turn_id'), at) if subtype in ('function_call', 'custom_tool_call') else None
                if subtype in ('function_call', 'custom_tool_call'):
                    name = label(payload.get('name'), 'redacted')
                    namespace = label(payload.get('namespace'), '')
                    qualified = namespace + '/' + name if namespace else name
                    raw = payload.get('arguments') if subtype == 'function_call' else payload.get('input')
                    fingerprint = argument_fingerprint(raw)
                    call_id = identifier(payload.get('call_id'))
                    if call_id and call_id in outer_calls:
                        continue
                    category = 'wrapper' if name in ('exec', 'wait') and not namespace else 'agent_tool'
                    call = add_call(qualified, category, at, turn['id'], fingerprint=fingerprint, item_id=call_id)
                    if call_id:
                        outer_calls[call_id] = call
                elif subtype in ('function_call_output', 'custom_tool_call_output'):
                    output_events.append((identifier(payload.get('call_id')), at))
            elif kind == 'compacted':
                turn = turn_for(at=at)
                session['compactions'].append({'at': at, 'turn_id': turn['id'],
                                              'window_number': number(payload.get('window_number'))})
            elif kind not in ('world_state', 'inter_agent_communication_metadata'):
                session['unknown_events'] += 1

    for call_id, at in output_events:
        call = outer_calls.get(call_id)
        if call:
            call['status'] = 'returned'
            duration = seconds_between(call['at'], at)
            call['duration_ms'] = duration * 1000 if duration is not None else None
    selected_records = records or fallback_records
    session['usage_source'] = 'token_usage_record' if records else 'cumulative_delta' if fallback_records else 'unavailable'
    session['_records'] = selected_records
    if records:
        session['context'] = [{'at': record['at'], 'turn_id': record['turn_id'],
                               'input_tokens': record['usage']['input_tokens'], 'context_window': record['context_window'],
                               'source': 'token_usage_record'} for record in records if record['usage']['input_tokens'] is not None]
    session['turns'] = list(turns.values())
    if session['historical_baseline']['total_tokens'] is not None and not records:
        session['warnings'].append('First cumulative snapshot contains unattributed history; baseline is excluded from observed turn totals.')
    if session['invalid_lines']:
        session['warnings'].append('Malformed, oversized, or unfinished lines were skipped; refresh after active writes.')
    if session['unknown_events']:
        session['warnings'].append('Unknown event types were ignored; metrics may be incomplete for newer schemas.')
    recalculate_session(session)
    return session


def recalculate_session(session):
    session['usage'] = empty_usage()
    turns = {turn['id']: turn for turn in session['turns']}
    for turn in turns.values():
        turn.update(usage=empty_usage(), responses=0, tool_calls=0, repeated_calls=0, failed_calls=0, compactions=0)
        turn['duration_seconds'] = seconds_between(turn['started_at'], turn['ended_at'])
    for record in session['_records']:
        add_usage(session['usage'], record['usage'])
        add_usage(turns[record['turn_id']]['usage'], record['usage'])
        turns[record['turn_id']]['responses'] += 1
    for call in session['calls']:
        turn = turns[call['turn_id']]
        turn['tool_calls'] += 1
        turn['repeated_calls'] += int(call['repeat_candidate'])
        turn['failed_calls'] += int(call['status'] == 'failed')
    for point in session['compactions']:
        turns[point['turn_id']]['compactions'] += 1
    session['models'] = sorted({turn['model'] for turn in turns.values()})
    if 'codex-auto-review' in session['models']:
        session['kind'] = 'approval_subagent'


def merge_sessions(sessions):
    grouped = collections.defaultdict(list)
    for session in sessions:
        grouped[session['id']].append(session)
    result = []
    for group in grouped.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        group.sort(key=lambda session: session['updated_at'] or '')
        merged = dict(group[-1])
        merged['warnings'] = list(dict.fromkeys(warning for session in group for warning in session['warnings']))
        merged['warnings'].append(f'{len(group)} log segments combined for this session; duplicate response IDs excluded.')
        merged['started_at'] = min((session['started_at'] for session in group if session['started_at']), default=None)
        turn_map = {}
        for session in group:
            for turn in session['turns']:
                if turn['id'] not in turn_map:
                    turn_map[turn['id']] = dict(turn)
                else:
                    existing = turn_map[turn['id']]
                    existing.update({key: value for key, value in turn.items() if value is not None and key != 'started_at'})
        merged['turns'] = sorted(turn_map.values(), key=lambda turn: turn['started_at'] or '')
        for index, turn in enumerate(merged['turns'], 1):
            turn['number'] = index
        for field in ('_records', 'calls', 'context', 'compactions', 'rate_limits'):
            seen = set()
            merged[field] = []
            for session in group:
                for item in session[field]:
                    key = item.get('id') if field == '_records' else (item.get('category'), item.get('id')) if field == 'calls' and item.get('id') else json.dumps(item, sort_keys=True)
                    if key is not None and key in seen:
                        continue
                    if key is not None:
                        seen.add(key)
                    merged[field].append(item)
            merged[field].sort(key=lambda item: item.get('at') or '')
        for field in ('invalid_lines', 'unknown_events', 'counter_resets', 'duplicate_usage_records'):
            merged[field] = sum(session[field] for session in group)
        sources = {session['usage_source'] for session in group} - {'unavailable'}
        merged['usage_source'] = next(iter(sources)) if len(sources) == 1 else 'mixed' if sources else 'unavailable'
        recalculate_session(merged)
        result.append(merged)
    return sorted(result, key=lambda session: session['updated_at'] or '', reverse=True)


def summarize(sessions):
    totals = empty_usage()
    by_kind = {}
    by_model = {}
    tools = {}
    seen_responses = set()
    limits = []
    measured_sessions = 0
    duplicate_responses = 0
    for session in sessions:
        measured_sessions += int(session['usage_source'] != 'unavailable')
        kind_usage = by_kind.setdefault(session['kind'], {'sessions': 0, 'usage': empty_usage()})
        kind_usage['sessions'] += 1
        for record in session['_records']:
            if record['id'] and record['id'] in seen_responses:
                duplicate_responses += 1
                continue
            if record['id']:
                seen_responses.add(record['id'])
            add_usage(totals, record['usage'])
            add_usage(kind_usage['usage'], record['usage'])
            model = by_model.setdefault(record['model'], {'usage': empty_usage(), 'responses': 0})
            add_usage(model['usage'], record['usage'])
            model['responses'] += 1
        for call in session['calls']:
            tool = tools.setdefault(call['name'], {'name': call['name'], 'category': call['category'],
                                                  'calls': 0, 'failed': 0, 'repeat_candidates': 0,
                                                  'duration_ms': 0, 'timed_calls': 0, 'output_bytes': None})
            tool['calls'] += 1
            tool['failed'] += int(call['status'] == 'failed')
            tool['repeat_candidates'] += int(call['repeat_candidate'])
            if call['duration_ms'] is not None:
                tool['duration_ms'] += call['duration_ms']
                tool['timed_calls'] += 1
            if call['output_bytes'] is not None:
                tool['output_bytes'] = (tool['output_bytes'] or 0) + call['output_bytes']
        limits.extend(dict(snapshot, session_id=session['id']) for snapshot in session['rate_limits'])
    limits.sort(key=lambda snapshot: snapshot['at'] or '')
    input_tokens = totals['input_tokens']
    cached = totals['cached_input_tokens']
    cache_ratio = cached / input_tokens if cached is not None and input_tokens else None
    all_turns = [(session['id'], turn) for session in sessions for turn in session['turns']]
    largest = sorted(all_turns, key=lambda pair: pair[1]['usage']['total_tokens'] or 0, reverse=True)[:10]
    recommendations = []
    if largest and largest[0][1]['usage']['total_tokens']:
        session_id, turn = largest[0]
        recommendations.append({'title': 'Start with the largest measured turns', 'evidence': f"Turn {turn['number']} in session {session_id[:8]} recorded {turn['usage']['total_tokens']:,} tokens across {turn['responses']} responses.",
                                'action': 'Use the turn table to separate repeated context input from output. Scope the next task and compare before and after.', 'confidence': 'measured evidence; optimization hypothesis'})
    context_inputs = [point['input_tokens'] for session in sessions for point in session['context'] if point['input_tokens'] is not None]
    if context_inputs:
        recommendations.append({'title': 'Watch repeated context input', 'evidence': f"Largest recorded response input: {max(context_inputs):,} tokens. Overall cache-hit share: {cache_ratio:.1%}." if cache_ratio is not None else f"Largest recorded response input: {max(context_inputs):,} tokens.",
                                'action': 'For independent work, try a fresh chat with only relevant files. Input size is a context proxy, and cached input still counts in reported token totals.', 'confidence': 'measured evidence; optimization hypothesis'})
    repeated = sum(tool['repeat_candidates'] for tool in tools.values())
    failures = sum(tool['failed'] for tool in tools.values())
    if repeated or failures:
        recommendations.append({'title': 'Inspect repeated calls and failures', 'evidence': f'{repeated} repeated-call candidates and {failures} explicitly failed executions recorded.',
                                'action': 'Review the timeline before changing the workflow. Repeated polling and repeated successful commands may be intentional; equality alone does not prove a retry.', 'confidence': 'heuristic candidates; measured failures'})
    shell_bytes = sum(tool['output_bytes'] or 0 for tool in tools.values() if tool['category'] == 'shell')
    if shell_bytes:
        recommendations.append({'title': 'Keep command output focused', 'evidence': f'{shell_bytes:,} bytes of shell output recorded across these logs.',
                                'action': 'Request narrower searches and concise output when the full content is unnecessary. Byte length does not establish how many output tokens entered model context.', 'confidence': 'measured bytes; token impact unavailable'})
    approval = by_kind.get('approval_subagent')
    if approval:
        recommendations.append({'title': 'Separate approval activity', 'evidence': f"{approval['sessions']} approval subagent logs; {approval['usage']['total_tokens'] or 0:,} observed tokens.",
                                'action': 'Keep these separate when comparing coding chats. Local logs do not establish whether approval tokens consume subscription allowance. Preserve necessary safety controls.', 'confidence': 'measured tokens; quota treatment unavailable'})
    return {'usage': totals, 'cache_ratio': cache_ratio, 'by_kind': by_kind, 'by_model': by_model,
            'tools': sorted(tools.values(), key=lambda tool: tool['calls'], reverse=True),
            'rate_limits': limits, 'measured_sessions': measured_sessions, 'duplicate_responses': duplicate_responses,
            'largest_turns': [{'session_id': session_id, **turn} for session_id, turn in largest],
            'recommendations': recommendations, 'compactions': sum(len(session['compactions']) for session in sessions)}


class Observatory:
    def __init__(self, root):
        self.root = root.expanduser().resolve()
        self.cache = {}
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            paths = []
            compressed = 0
            unreadable = 0
            for name in ('sessions', 'archived_sessions'):
                directory = self.root / name
                if directory.is_symlink():
                    continue
                compressed += len(list(directory.rglob('*.jsonl.zst')))
                paths.extend(path for path in directory.rglob('*.jsonl') if not path.is_symlink())
            active = set(paths)
            self.cache = {path: value for path, value in self.cache.items() if path in active}
            for path in paths:
                try:
                    if not path.resolve().is_relative_to(self.root):
                        continue
                    stat = path.stat()
                    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    if path not in self.cache or self.cache[path][0] != signature:
                        self.cache[path] = (signature, parse_session(path))
                except (OSError, ValueError, RecursionError):
                    unreadable += 1
                    self.cache.pop(path, None)
            sessions = merge_sessions([value[1] for value in self.cache.values()])
            summary = summarize(sessions)
            public_sessions = [{key: value for key, value in session.items() if not key.startswith('_')} for session in sessions]
            return {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'privacy': 'Loopback only. Prompts, commands, arguments, outputs, paths, and secrets excluded. Memory storage.',
                    'coverage': {'sessions': len(sessions), 'log_files': len(self.cache), 'compressed_skipped': compressed, 'unreadable_files': unreadable,
                                 'invalid_lines': sum(session['invalid_lines'] for session in sessions)},
                    'config': read_config(self.root), 'summary': summary, 'sessions': public_sessions}


def make_handler(observatory, access_token, public_port=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            return

        def send_body(self, status, body, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = self.headers.get('Host')
            if host != f'127.0.0.1:{public_port or self.server.server_port}':
                self.send_body(403, b'Invalid host', 'text/plain')
                return
            if self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin'):
                self.send_body(403, b'Cross-site request denied', 'text/plain')
                return
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{host}':
                self.send_body(403, b'Invalid origin', 'text/plain')
                return
            route = urlsplit(self.path).path
            if route == '/healthz':
                self.send_body(200, b'healthy', 'text/plain')
                return
            if route == '/api/snapshot':
                if not secrets.compare_digest(self.headers.get('X-Observatory-Token', ''), access_token):
                    self.send_body(401, b'Access token required', 'text/plain')
                    return
                try:
                    body = json.dumps(observatory.snapshot(), allow_nan=False).encode()
                    self.send_body(200, body, 'application/json')
                except Exception:
                    self.send_body(500, b'Metrics could not be read. Check local log permissions.', 'text/plain')
                return
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/dashboard.css': ('dashboard.css', 'text/css; charset=utf-8'),
                     '/metrics.js': ('metrics.js', 'text/javascript; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if route not in files:
                self.send_body(404, b'Not found', 'text/plain')
                return
            name, content_type = files[route]
            self.send_body(200, (ASSETS / name).read_bytes(), content_type)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--container', action='store_true', help='Listen inside a container; publish only to host loopback')
    parser.add_argument('--public-port', type=int, help='Host loopback port when mapped by Docker')
    parser.add_argument('--link-file', type=Path, help='Save the current local access link in a private runtime file')
    parser.add_argument('--check', action='store_true', help='Print content-free metrics and exit')
    args = parser.parse_args()
    observatory = Observatory(args.codex_home)
    if args.check:
        snapshot = observatory.snapshot()
        print(json.dumps({'coverage': snapshot['coverage'], 'summary': snapshot['summary'], 'config': snapshot['config']}, indent=2))
        return
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    if args.public_port is not None and not 1 <= args.public_port <= 65535:
        parser.error('public-port must be between 1 and 65535')
    token = secrets.token_urlsafe(32)
    try:
        server = ThreadingHTTPServer(('0.0.0.0' if args.container else '127.0.0.1', args.port),
                                     make_handler(observatory, token, args.public_port))
    except OSError:
        parser.exit(1, 'Could not bind local port. Try --port 8766.\n')
    link = f'http://127.0.0.1:{args.public_port or server.server_port}/#token={token}'
    if args.link_file:
        descriptor = os.open(args.link_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(link + '\n')
        print('Codex Observatory started. Retrieve the access link with: python3 manage.py url', flush=True)
    else:
        print(f'Codex Observatory: {link}', flush=True)
    print('Read-only log access. No uploads or AI API calls. Stop with Control-C.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
