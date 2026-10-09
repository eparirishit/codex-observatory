import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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

    def test_identical_commands_are_candidates_not_retries(self):
        events = self.base()
        for second in (3, 4):
            events.append(event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one', 'item': {'type': 'CommandExecution', 'id': f'shell-{second}', 'command': ['pwd'], 'exit_code': 0}}, second))
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual([call['repeat_candidate'] for call in session['calls']], [False, True])
        self.assertEqual(session['turns'][0]['repeated_calls'], 1)

    def test_mcp_arguments_order_and_unavailable_output_bytes(self):
        events = self.base()
        for second, arguments in [(3, {'first': 1, 'second': 2}), (4, {'second': 2, 'first': 1})]:
            events.append(event('event_msg', {'type': 'item_completed', 'turn_id': 'turn-one', 'item': {'type': 'McpToolCall', 'id': f'mcp-{second}', 'server': 'example', 'tool': 'read', 'arguments': arguments, 'status': 'completed'}}, second))
        self.write(events)
        session = parse_session(self.path)
        self.assertEqual([call['repeat_candidate'] for call in session['calls']], [False, True])
        self.assertIsNone(summarize([session])['tools'][0]['output_bytes'])

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
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
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
