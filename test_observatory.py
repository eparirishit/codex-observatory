import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from observatory import Observatory, make_handler, number, parse_session, summarize


def event(kind, payload, second=0):
    return {'timestamp': f'2026-10-08T12:00:{second:02d}Z', 'type': kind, 'payload': payload}


def usage(input_tokens=100, cached=60, output=20, reasoning=5):
    return {'input_tokens': input_tokens, 'cached_input_tokens': cached,
            'output_tokens': output, 'reasoning_output_tokens': reasoning,
            'total_tokens': input_tokens + output}


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = self.root / 'sessions'
        self.logs.mkdir()
        self.path = self.logs / 'test.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, events, tail=''):
        self.path.write_text(''.join(json.dumps(item) + '\n' for item in events) + tail)

    def base(self):
        return [event('session_meta', {'id': 'session-one', 'base_instructions': 'PRIVATE_PROMPT'}),
                event('event_msg', {'type': 'task_started', 'turn_id': 'turn-one', 'model_context_window': 1000}, 1),
                event('turn_context', {'turn_id': 'turn-one', 'model': 'gpt-6.1-sol', 'effort': 'high'}, 2)]

    def test_response_records_and_snapshots_are_not_double_counted(self):
        record = {'response_id': 'resp-one', 'turn_id': 'turn-one', 'usage': usage(), 'thread_token_usage': usage()}
        events = self.base() + [event('token_usage_record', record, 3), event('token_usage_record', record, 4),
                               event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': usage(), 'last_token_usage': usage()}}, 5)]
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual(session['usage']['total_tokens'], 120)
        self.assertEqual(session['usage']['reasoning_output_tokens'], 5)
        self.assertEqual(session['turns'][0]['responses'], 1)
        self.assertEqual(session['duplicate_usage_records'], 1)
        self.assertEqual(session['usage_source'], 'token_usage_record')
        self.assertEqual(summarize([session, session])['usage']['total_tokens'], 120)

    def test_response_analytics_metadata_is_hashed_and_historical(self):
        events = self.base()
        events[0]['payload']['cwd'] = '/PRIVATE_PROJECT'
        events[2]['payload'].update(cwd='/PRIVATE_PROJECT', service_tier='fast')
        events += [event('token_usage_record', {'response_id': 'PRIVATE_RESPONSE', 'turn_id': 'turn-one', 'usage': usage()}, 3),
                   event('turn_context', {'turn_id': 'turn-two', 'model': 'other', 'effort': 'low', 'cwd': '/PRIVATE_OTHER', 'service_tier': 'default'}, 4),
                   event('token_usage_record', {'response_id': 'PRIVATE_SECOND', 'turn_id': 'turn-two', 'usage': usage()}, 5)]
        self.write(events)
        snapshot = Observatory(self.root).snapshot()
        session = snapshot['sessions'][0]
        first, second = session['responses']
        self.assertEqual(first['service_tier'], 'fast')
        self.assertEqual(second['service_tier'], 'default')
        self.assertNotEqual(first['project_id'], second['project_id'])
        self.assertEqual(first['project_id'], session['turns'][0]['project_id'])
        self.assertNotIn('PRIVATE_', json.dumps(snapshot))
        self.assertNotIn('_fingerprint', json.dumps(snapshot))

    def test_missing_tier_does_not_inherit_fast_pricing(self):
        events = self.base()
        events[2]['payload']['service_tier'] = 'fast'
        events += [event('token_usage_record', {'response_id': 'fast', 'turn_id': 'turn-one', 'usage': usage()}, 3),
                   event('turn_context', {'turn_id': 'turn-two', 'model': 'example', 'service_tier': None}, 4),
                   event('token_usage_record', {'response_id': 'unknown', 'turn_id': 'turn-two', 'usage': usage()}, 5),
                   event('turn_context', {'turn_id': 'turn-three', 'model': 'example'}, 6),
                   event('token_usage_record', {'response_id': 'missing', 'turn_id': 'turn-three', 'usage': usage()}, 7)]
        self.write(events)
        self.assertEqual([record['service_tier'] for record in parse_session(self.path)['_records']], ['fast', 'unavailable', 'unavailable'])

    def test_service_tier_only_exports_known_enum_values(self):
        events = self.base()
        events[2]['payload']['service_tier'] = '/PRIVATE_TIER'
        events += [event('token_usage_record', {'response_id': 'one', 'turn_id': 'turn-one', 'usage': usage(), 'service_tier': '/PRIVATE_OVERRIDE'}, 3)]
        self.write(events)
        snapshot = Observatory(self.root).snapshot()
        self.assertEqual(snapshot['sessions'][0]['responses'][0]['service_tier'], 'unavailable')
        self.assertNotIn('PRIVATE_', json.dumps(snapshot))

    def test_fallback_deltas_repeated_snapshots_and_reset(self):
        events = self.base()
        for second, metrics in enumerate([usage(), usage(), usage(200, 120, 40, 10), usage(50, 30, 10, 2), usage(75, 40, 15, 3)], 3):
            events.append(event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': metrics, 'last_token_usage': metrics}}, second))
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual(session['usage']['total_tokens'], 270)
        self.assertEqual(session['counter_resets'], 1)
        self.assertEqual(session['usage_source'], 'cumulative_delta')

    def test_inherited_baseline_and_missing_fields(self):
        self.write(self.base() + [event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': usage(1000), 'last_token_usage': usage()}}, 3),
                                  event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 1100, 'total_tokens': 1120}}}, 4)])
        session = parse_session(self.path)
        self.assertEqual(session['historical_baseline']['total_tokens'], 1020)
        self.assertEqual(session['usage']['total_tokens'], 100)
        self.assertIsNone(session['usage']['output_tokens'])
        self.assertIsNone(session['usage']['reasoning_output_tokens'])

    def test_turn_model_switch_and_subagent_relationship(self):
        events = self.base()
        events[0]['payload'].update(parent_thread_id='parent-secret', source={'subagent': {'thread_spawn': {'parent_thread_id': 'parent-secret'}}})
        events += [event('token_usage_record', {'response_id': 'response-one', 'turn_id': 'turn-one', 'usage': usage()}, 3),
                   event('event_msg', {'type': 'task_started', 'turn_id': 'turn-two'}, 4),
                   event('turn_context', {'turn_id': 'turn-two', 'model': 'gpt-6-astra', 'effort': 'low'}, 5),
                   event('token_usage_record', {'response_id': 'response-two', 'turn_id': 'turn-two', 'usage': usage(300)}, 6)]
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual(session['kind'], 'subagent')
        self.assertNotEqual(session['parent_id'], 'parent-secret')
        self.assertEqual([turn['model'] for turn in session['turns']], ['gpt-6.1-sol', 'gpt-6-astra'])
        self.assertEqual(session['turns'][1]['usage']['total_tokens'], 320)

    def test_inner_mcp_failure_compaction_and_redaction(self):
        secret = 'sk-PRIVATE_SECRET'
        events = self.base() + [
            event('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'input': secret, 'call_id': 'call-one'}, 3),
            event('response_item', {'type': 'custom_tool_call_output', 'output': secret, 'call_id': 'call-one'}, 4),
            event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one', 'started_at_ms': 1, 'completed_at_ms': 50,
                                'item': {'type': 'McpToolCall', 'id': 'mcp-one', 'server': 'context7', 'tool': 'query-docs', 'arguments': {'prompt': secret}, 'result': {'isError': True, 'content': secret}, 'status': 'completed'}}, 5),
            event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one', 'item': {'type': 'CommandExecution', 'id': 'shell-one', 'command': secret, 'aggregated_output': secret, 'exit_code': 1}}, 6),
            event('response_item', {'type': 'compaction', 'encrypted_content': secret}, 7),
            event('event_msg', {'type': 'item_completed', 'item': {'type': 'ContextCompaction', 'id': 'compact-one'}}, 8),
            event('compacted', {'message': secret, 'replacement_history': [secret], 'window_number': 1}, 9),
            event('event_msg', {'type': 'token_count', 'rate_limits': {'limit_id': 'codex', 'primary': {'used_percent': 80, 'window_minutes': 300, 'resets_at': 1791500000}, 'credits': {'balance': secret}}}, 10)
        ]
        self.write(events, '{unfinished')
        (self.root / 'config.toml').write_text('model="gpt-6.1-sol"\n[mcp_servers.context7]\ncommand="' + secret + '"\n[mcp_servers.context7.env]\nAPI_KEY="' + secret + '"\n')
        snapshot = Observatory(self.root).snapshot()
        serialized = json.dumps(snapshot)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('PRIVATE_PROMPT', serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('session-one', serialized)
        session = snapshot['sessions'][0]
        self.assertEqual(session['invalid_lines'], 1)
        self.assertEqual(len(session['compactions']), 1)
        self.assertEqual(session['calls'][1]['name'], 'context7/query-docs')
        self.assertEqual(session['calls'][1]['status'], 'failed')
        self.assertEqual(session['calls'][0]['status'], 'returned')
        self.assertEqual(session['rate_limits'][0]['primary']['used_percent'], 80)

    def test_successful_polling_is_not_a_repeat_candidate(self):
        events = self.base()
        for second in (3, 4):
            events.append(event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one', 'item': {'type': 'CommandExecution', 'id': f'shell-{second}', 'command': ['pwd'], 'exit_code': 0}}, second))
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual([call['repeat_candidate'] for call in session['calls']], [False, False])
        self.assertEqual(session['turns'][0]['repeated_calls'], 0)

    def test_mcp_arguments_order_and_unavailable_output_bytes(self):
        events = self.base()
        for second, arguments in [(3, {'first': 1, 'second': 2}), (4, {'second': 2, 'first': 1})]:
            events.append(event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one', 'item': {'type': 'McpToolCall', 'id': f'mcp-{second}', 'server': 'example', 'tool': 'read', 'arguments': arguments, 'status': 'failed' if second == 3 else 'completed'}}, second))
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual([call['repeat_candidate'] for call in session['calls']], [False, True])
        self.assertIsNone(summarize([session])['tools'][0]['output_bytes'])

    def test_repeats_require_failure_and_do_not_cross_turns_or_overlap(self):
        events = self.base()
        for second, status, duration in ((3, 'failed', 100), (4, 'completed', 2000), (5, 'completed', 100), (6, 'failed', 100), (7, 'completed', 100), (8, 'completed', 100)):
            events.append(event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one',
                          'started_at_ms': 0, 'completed_at_ms': duration,
                          'item': {'type': 'CommandExecution', 'id': f'call-{second}', 'command': ['pwd'], 'status': status}}, second))
        events += [event('event_msg', {'type': 'task_started', 'turn_id': 'turn-two'}, 9),
                   event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-two', 'item': {'type': 'CommandExecution', 'id': 'new-turn', 'command': ['pwd'], 'exit_code': 0}}, 10)]
        self.write(events)
        snapshot = Observatory(self.root).snapshot()
        self.assertEqual([call['repeat_candidate'] for call in snapshot['sessions'][0]['calls']], [False, False, False, False, True, False, False])
        self.assertNotIn('_fingerprint', json.dumps(snapshot))

    def test_failed_repeat_is_recalculated_across_segments(self):
        self.write(self.base() + [event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one',
                   'item': {'type': 'CommandExecution', 'id': 'failed', 'command': ['pwd'], 'exit_code': 1}}, 3)])
        events = self.base() + [event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one',
                  'item': {'type': 'CommandExecution', 'id': 'repeated', 'command': ['pwd'], 'exit_code': 0}}, 5)]
        (self.logs / 'later.jsonl').write_text(''.join(json.dumps(item) + '\n' for item in events))
        reader = Observatory(self.root)
        for _ in range(2):
            snapshot = reader.snapshot()
            self.assertEqual([call['repeat_candidate'] for call in snapshot['sessions'][0]['calls']], [False, True])
            self.assertNotIn('_fingerprint', json.dumps(snapshot))

    def test_recommendations_require_comparable_outliers(self):
        events = self.base()
        for index, total in enumerate((100, 100, 100, 100, 100), 1):
            events += [event('turn_context', {'turn_id': f'turn-{index}', 'model': 'example'}, index),
                       event('token_usage_record', {'response_id': f'resp-{index}', 'turn_id': f'turn-{index}', 'usage': usage(total, 90, 0)}, index)]
        self.write(events)
        self.assertEqual(summarize([parse_session(self.path)])['recommendations'], [])
        events[-1]['payload']['usage'] = usage(500, 450, 0)
        self.write(events)
        recommendations = summarize([parse_session(self.path)])['recommendations']
        self.assertEqual(len(recommendations), 1)
        self.assertIn('median', recommendations[0]['evidence'])
        events[-2]['payload']['model'] = 'different-model'
        self.write(events)
        self.assertEqual(summarize([parse_session(self.path)])['recommendations'], [])

    def test_cache_collapse_requires_sustained_valid_evidence(self):
        events = self.base()
        for index, cached in enumerate((9500, 9500, 9500, 1000, 1000, 1000), 3):
            events.append(event('token_usage_record', {'response_id': f'resp-{index}', 'turn_id': 'turn-one', 'usage': usage(10000, cached, 0)}, index))
        self.write(events[:-1])
        self.assertEqual(summarize([parse_session(self.path)])['recommendations'], [])
        self.write(events)
        recommendations = summarize([parse_session(self.path)])['recommendations']
        self.assertEqual(len(recommendations), 1)
        self.assertIn('cache', recommendations[0]['title'].lower())
        events[-1]['payload']['usage']['cached_input_tokens'] = None
        self.write(events)
        self.assertEqual(summarize([parse_session(self.path)])['recommendations'], [])

    def test_duplicate_responses_do_not_supply_comparable_turns(self):
        sessions = []
        for index in range(5):
            events = self.base()
            events[0]['payload']['id'] = f'session-{index}'
            events.append(event('token_usage_record', {'response_id': 'shared' if index < 4 else 'unique', 'turn_id': 'turn-one',
                          'usage': usage(100 if index < 4 else 500, 0, 0)}, 3))
            self.write(events)
            sessions.append(parse_session(self.path))
        summary = summarize(sessions)
        self.assertEqual(summary['usage']['total_tokens'], 600)
        self.assertEqual(summary['recommendations'], [])

    def test_mixed_model_turns_do_not_supply_comparable_outliers(self):
        events = self.base()
        for index in range(5):
            if index == 4:
                events += [event('turn_context', {'turn_id': f'turn-{index}', 'model': 'other'}, 3),
                           event('token_usage_record', {'response_id': 'other-response', 'turn_id': f'turn-{index}', 'usage': usage(400, 0, 0)}, 4)]
            events += [event('turn_context', {'turn_id': f'turn-{index}', 'model': 'same'}, 5),
                       event('token_usage_record', {'response_id': f'response-{index}', 'turn_id': f'turn-{index}', 'usage': usage(100, 0, 0)}, 6)]
        self.write(events)
        self.assertEqual(summarize([parse_session(self.path)])['recommendations'], [])

    def test_lifecycle_updates_keep_valid_repeat_candidates(self):
        events = self.base()
        for second, call_id, status, duration in ((3, 'first', 'failed', 100), (4, 'second', 'in_progress', None), (10, 'second', 'completed', 6000)):
            payload = {'type': 'item_completed', 'turn_id': 'turn-one', 'item': {'type': 'McpToolCall', 'id': call_id,
                       'server': 'example', 'tool': 'read', 'arguments': {'key': 1}, 'status': status}}
            if duration is not None:
                payload.update(started_at_ms=0, completed_at_ms=duration)
            events.append(event('event_msg', payload, second))
        self.write(events)
        self.assertEqual([call['repeat_candidate'] for call in parse_session(self.path)['calls']], [False, True])

    def test_refresh_after_append_and_unavailable_is_not_zero(self):
        self.write(self.base())
        reader = Observatory(self.root)
        self.assertIsNone(reader.snapshot()['summary']['usage']['total_tokens'])
        with self.path.open('a') as stream:
            stream.write(json.dumps(event('token_usage_record', {'response_id': 'new', 'turn_id': 'turn-one', 'usage': usage()}, 3)) + '\n')
        self.assertEqual(reader.snapshot()['summary']['usage']['total_tokens'], 120)
        self.path.unlink()
        self.assertEqual(reader.snapshot()['coverage']['sessions'], 0)

    def test_archives_and_non_object_lines(self):
        self.write(self.base(), 'null\n[]\n')
        (self.logs / 'archive.jsonl.zst').write_bytes(b'compressed')
        snapshot = Observatory(self.root).snapshot()
        self.assertEqual(snapshot['coverage']['compressed_skipped'], 1)
        self.assertEqual(snapshot['coverage']['invalid_lines'], 2)

    def test_compressed_logs_stream_frames_and_merge_with_readable_logs(self):
        self.write(self.base())
        record = event('token_usage_record', {'response_id': 'compressed-response', 'turn_id': 'turn-one', 'usage': usage()}, 3)
        frames = [self.path.read_bytes(), (json.dumps(record) + '\n' + 'x' * 1025 + '\n{unfinished').encode()]
        compressed = b''.join(subprocess.run(['zstd', '-q', '-c'], input=frame, capture_output=True, check=True).stdout for frame in frames)
        archives = self.root / 'archived_sessions'
        archives.mkdir()
        archive = archives / 'history.jsonl.zst'
        archive.write_bytes(compressed)
        reader = Observatory(self.root)
        with patch('observatory.MAX_LINE', 1024):
            first = reader.snapshot()
        self.assertEqual(first['coverage']['log_files'], 2)
        self.assertEqual(first['coverage']['sessions'], 1)
        self.assertEqual(first['coverage']['compressed_skipped'], 0)
        self.assertEqual(first['coverage']['invalid_lines'], 2)
        self.assertEqual(first['summary']['usage']['total_tokens'], 120)
        self.assertNotIn('PRIVATE_PROMPT', json.dumps(first))
        self.assertEqual(reader.snapshot()['summary']['usage']['total_tokens'], 120)
        self.assertEqual(archive.read_bytes(), compressed)
        self.write(self.base() + [record])
        self.assertEqual(reader.snapshot()['summary']['usage']['total_tokens'], 120)

    def test_failed_compressed_reads_do_not_publish_partial_metrics(self):
        self.write(self.base())
        compressed = subprocess.run(['zstd', '-q', '-c'], input=self.path.read_bytes(), capture_output=True, check=True).stdout
        archive = self.logs / 'history.jsonl.zst'
        archive.write_bytes(compressed[:-1])
        snapshot = Observatory(self.root).snapshot()
        self.assertEqual(snapshot['coverage']['log_files'], 1)
        self.assertEqual(snapshot['coverage']['unreadable_files'], 1)
        self.assertEqual(snapshot['coverage']['compressed_skipped'], 1)
        archive.write_bytes(compressed)
        with patch('observatory.subprocess.Popen', side_effect=FileNotFoundError):
            snapshot = Observatory(self.root).snapshot()
        self.assertEqual(snapshot['coverage']['log_files'], 1)
        self.assertEqual(snapshot['coverage']['compressed_skipped'], 1)

    def test_collaboration_executions_preserve_outcomes_and_redaction(self):
        events = self.base()
        for second, status in ((3, 'completed'), (4, 'failed')):
            events.append(event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one',
                          'started_at_ms': 100, 'completed_at_ms': 350,
                          'item': {'type': 'CollabAgentToolCall', 'id': f'collab-{second}',
                                   'tool': 'spawn_agent', 'status': status, 'prompt': 'PRIVATE_PROMPT',
                                   'sender_thread_id': 'PRIVATE_SENDER', 'receiver_thread_ids': ['PRIVATE_RECEIVER'],
                                   'agents_states': {'PRIVATE_RECEIVER': {'message': 'PRIVATE_RESULT'}}}}, second))
        self.write(events)
        snapshot = Observatory(self.root).snapshot()
        calls = snapshot['sessions'][0]['calls']
        self.assertEqual([call['status'] for call in calls], ['completed', 'failed'])
        self.assertEqual([call['duration_ms'] for call in calls], [250, 250])
        self.assertEqual(calls[0]['name'], 'subagent.spawn_agent')
        self.assertEqual(calls[0]['category'], 'subagent')
        self.assertEqual(snapshot['summary']['tools'][0]['failed'], 1)
        self.assertNotIn('PRIVATE_', json.dumps(snapshot))

    def test_turn_abort_events_end_running_turns(self):
        for subtype in ('task_aborted', 'turn_aborted'):
            with self.subTest(subtype=subtype):
                self.write(self.base() + [event('event_msg', {'type': subtype, 'turn_id': 'turn-one'}, 4)])
                turn = parse_session(self.path)['turns'][0]
                self.assertEqual(turn['status'], 'aborted')
                self.assertEqual(turn['duration_seconds'], 3)

    def test_unknown_nested_events_are_visible_without_warning_on_known_messages(self):
        self.write(self.base() + [
            event('response_item', {'type': 'message', 'content': 'PRIVATE_PROMPT'}, 3),
            event('event_msg', {'type': 'item_completed', 'item': {'type': 'AgentMessage', 'id': 'message'}}, 4),
            event('event_msg', {'type': 'future_event', 'content': 'PRIVATE_EVENT'}, 5),
            event('response_item', {'type': 'future_tool_call', 'arguments': 'PRIVATE_ARGUMENTS'}, 6),
            event('event_msg', {'type': 'item_completed', 'item': {'type': 'FutureToolCall', 'id': 'future'}}, 7)])
        snapshot = Observatory(self.root).snapshot()
        self.assertEqual(snapshot['sessions'][0]['unknown_events'], 3)
        self.assertEqual(snapshot['coverage']['unknown_events'], 3)
        self.assertEqual(len(snapshot['sessions'][0]['warnings']), 1)
        self.assertNotIn('PRIVATE_', json.dumps(snapshot))

    def test_session_segments_merge_without_duplicate_rows_or_tokens(self):
        self.write(self.base() + [event('token_usage_record', {'response_id': 'response-one', 'turn_id': 'turn-one', 'usage': usage()}, 3)])
        (self.logs / 'copy.jsonl').write_bytes(self.path.read_bytes())
        reader = Observatory(self.root)
        first = reader.snapshot()
        second = reader.snapshot()
        self.assertEqual(first['coverage']['log_files'], 2)
        self.assertEqual(first['coverage']['sessions'], 1)
        self.assertEqual(first['summary']['usage']['total_tokens'], 120)
        self.assertEqual(second['sessions'][0]['turns'][0]['responses'], 1)

    def test_call_completion_survives_overlapping_segments_and_stale_copies(self):
        pending = self.base() + [event('response_item', {'type': 'function_call', 'name': 'exec', 'call_id': 'call-one', 'arguments': '{}'}, 3)]
        self.write(pending)
        finished = pending + [event('response_item', {'type': 'function_call_output', 'call_id': 'call-one', 'output': 'PRIVATE_OUTPUT'}, 5),
                              event('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one'}, 6)]
        (self.logs / 'finished.jsonl').write_text(''.join(json.dumps(item) + '\n' for item in finished))
        stale = pending + [event('response_item', {'type': 'message'}, 9)]
        (self.logs / 'stale.jsonl').write_text(''.join(json.dumps(item) + '\n' for item in stale))
        reader = Observatory(self.root)
        for _ in range(2):
            snapshot = reader.snapshot()
            session = snapshot['sessions'][0]
            self.assertEqual(len(session['calls']), 1)
            self.assertEqual(session['calls'][0]['status'], 'returned')
            self.assertEqual(session['calls'][0]['duration_ms'], 2000)
            self.assertEqual(session['calls'][0]['ended_at'], '2026-10-08T12:00:05+00:00')
            self.assertEqual(session['turns'][0]['status'], 'completed')
            self.assertNotIn('PRIVATE_OUTPUT', json.dumps(snapshot))

    def test_output_in_separate_segment_completes_call(self):
        self.write(self.base() + [event('response_item', {'type': 'function_call', 'name': 'exec', 'call_id': 'call-one', 'arguments': '{}'}, 3)])
        output = self.base() + [event('response_item', {'type': 'function_call_output', 'call_id': 'call-one', 'output': 'PRIVATE_OUTPUT'}, 5)]
        (self.logs / 'output.jsonl').write_text(''.join(json.dumps(item) + '\n' for item in output))
        call = Observatory(self.root).snapshot()['sessions'][0]['calls'][0]
        self.assertEqual(call['status'], 'returned')
        self.assertEqual(call['duration_ms'], 2000)

    def test_merged_call_duration_uses_earliest_start(self):
        for name, start in (('first.jsonl', 3), ('later.jsonl', 4)):
            events = self.base() + [event('response_item', {'type': 'function_call', 'name': 'exec', 'call_id': 'call-one'}, start),
                                    event('response_item', {'type': 'function_call_output', 'call_id': 'call-one'}, 5)]
            if start == 4:
                events.append(event('response_item', {'type': 'message'}, 6))
            (self.logs / name).write_text(''.join(json.dumps(item) + '\n' for item in events))
        call = Observatory(self.root).snapshot()['sessions'][0]['calls'][0]
        self.assertEqual(call['duration_ms'], 2000)

    def test_execution_updates_preserve_terminal_state_and_timing(self):
        events = self.base()
        for second, status in ((3, 'in_progress'), (5, 'failed'), (6, 'in_progress')):
            item = {'type': 'McpToolCall', 'id': 'mcp-one', 'server': 'example', 'tool': 'read', 'arguments': {'key': 'value'}, 'status': status}
            payload = {'type': 'item_completed', 'turn_id': 'turn-one', 'item': item}
            if status == 'failed':
                payload.update(started_at_ms=100, completed_at_ms=350)
            events.append(event('event_msg', payload, second))
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual(len(session['calls']), 1)
        self.assertEqual(session['calls'][0]['status'], 'failed')
        self.assertEqual(session['calls'][0]['duration_ms'], 250)
        self.assertEqual(session['turns'][0]['failed_calls'], 1)
        self.assertEqual(session['turns'][0]['repeated_calls'], 0)

    def test_replayed_turn_start_does_not_replace_terminal_state(self):
        self.write(self.base() + [event('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one'}, 5),
                                  event('event_msg', {'type': 'task_started', 'turn_id': 'turn-one'}, 6),
                                  event('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one'}, 4)])
        turn = parse_session(self.path)['turns'][0]
        self.assertEqual(turn['status'], 'completed')
        self.assertEqual(turn['duration_seconds'], 4)

    def test_invalid_numeric_and_item_types_do_not_break_other_metrics(self):
        self.assertIsNone(number(10 ** 400))
        self.assertIsNone(number(True))
        self.assertIsNone(number(float('nan')))
        self.write(self.base() + [event('event_msg', {'type': 'item_completed', 'item': {'type': ['unexpected']}}, 3),
                                  event('token_usage_record', {'response_id': 'valid', 'turn_id': 'turn-one', 'usage': usage()}, 4)])
        self.assertEqual(Observatory(self.root).snapshot()['summary']['usage']['total_tokens'], 120)


class ServerTests(unittest.TestCase):
    def test_container_public_port_and_health(self):
        with tempfile.TemporaryDirectory() as folder:
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Observatory(Path(folder)), 'test-token', public_port=9876))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(Request(address + '/healthz', headers={'Host': '127.0.0.1:9876'})) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(address + '/healthz')
                self.assertEqual(caught.exception.code, 403)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(address + '/api/snapshot', headers={'Host': '127.0.0.1:9876'}))
                self.assertEqual(caught.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_loopback_auth_host_origin_and_static_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Observatory(Path(folder)), 'test-token'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(address + '/') as response:
                    self.assertIn(b'Codex Observatory', response.read())
                    self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
                    self.assertIn("font-src 'self'", response.headers['Content-Security-Policy'])
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                with urlopen(address + '/fonts/geist.woff2') as response:
                    self.assertEqual(response.headers['Content-Type'], 'font/woff2')
                    self.assertEqual(response.read(4), b'wOF2')
                for path, headers, expected in [('/api/snapshot', {}, 401),
                                                ('/api/snapshot', {'X-Observatory-Token': 'wrong'}, 401),
                                                ('/api/snapshot', {'X-Observatory-Token': 'test-token', 'Origin': 'https://evil.example'}, 403),
                                                ('/api/snapshot', {'Host': 'evil.example', 'X-Observatory-Token': 'test-token'}, 403),
                                                ('/api/snapshot', {'Sec-Fetch-Site': 'cross-site', 'X-Observatory-Token': 'test-token'}, 403),
                                                ('/../observatory.py', {}, 404)]:
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(Request(address + path, headers=headers))
                    self.assertEqual(caught.exception.code, expected)
                with urlopen(Request(address + '/api/snapshot', headers={'X-Observatory-Token': 'test-token'})) as response:
                    self.assertEqual(json.load(response)['coverage']['sessions'], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
