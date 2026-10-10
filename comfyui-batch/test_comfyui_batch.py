import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import comfyui_batch as app

DEFS = {
    'Source': {'input': {'required': {'seed': ['INT', {'control_after_generate': True}], 'text': ['STRING', {}]}}, 'input_order': {'required': ['seed', 'text']}},
    'Output': {'input': {'required': {'image': ['IMAGE', {}], 'prefix': ['STRING', {}]}}}
}
UI = {'nodes': [
    {'id': 1, 'type': 'Source', 'widgets_values': [5, 'randomize', '{{text}}']},
    {'id': 2, 'type': 'Reroute', 'inputs': [{'name': '', 'link': 10}]},
    {'id': 3, 'type': 'Output', 'inputs': [{'name': 'image', 'link': 11}], 'widgets_values': ['batch']}
], 'links': [[10, 1, 0, 2, 0, 'IMAGE'], [11, 2, 0, 3, 0, 'IMAGE']]}

class LogicTests(unittest.TestCase):
    def test_recursive_types_and_names(self):
        data = {'x': '{{seed}}', 'list': ['{{image}}', '前缀{{seed}}'], 'nested': {'text': '{{ text }}'}}
        self.assertEqual(app.placeholders(data), {'seed', 'image', 'text'})
        result = app.replace_values(data, {'seed': 123, 'image': '中文.png', 'text': 'a"\nb'})
        self.assertEqual(result['x'], 123)
        self.assertEqual(result['list'], ['中文.png', '前缀123'])
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(data['x'], '{{seed}}')

    def test_parameter_modes(self):
        config = {'seed': {'type': 'int', 'mode': '递增', 'value': '100'}, 'text': {'type': 'string', 'value': 'x'}}
        self.assertEqual(app.parameter_values(config, 2), {'seed': 102, 'text': 'x'})
        config['seed']['mode'] = '随机'
        config['seed']['value'] = ''
        with patch.object(app.secrets, 'randbelow', return_value=42):
            self.assertEqual(app.parameter_values(config, 0)['seed'], 42)
        config['seed'].update(mode='固定', value='abc')
        with self.assertRaises(ValueError):
            app.parameter_values(config, 0)

    def test_ui_conversion_and_reroute(self):
        converted = app.convert_workflow(app.replace_values(UI, {'text': 'hello'}), DEFS)
        self.assertEqual(converted['1']['inputs'], {'seed': 5, 'text': 'hello'})
        self.assertEqual(converted['3']['inputs'], {'image': ['1', 0], 'prefix': 'batch'})
        self.assertNotIn('2', converted)

    def test_reject_ambiguous_widgets_and_missing_node(self):
        data = json.loads(json.dumps(UI))
        data['nodes'][0]['widgets_values'].append('unexpected')
        with self.assertRaisesRegex(ValueError, '额外控件'):
            app.convert_workflow(data, DEFS)
        with self.assertRaisesRegex(ValueError, '无法转换'):
            app.convert_workflow(UI, {})

    def test_api_and_wrapped_api(self):
        graph = {'1': {'class_type': 'Source', 'inputs': {'seed': 1}}}
        self.assertEqual(app.convert_workflow(graph, {}), graph)
        self.assertEqual(app.convert_workflow({'prompt': graph}, {}), graph)

class IntegrationTests(unittest.TestCase):
    def test_local_http_success_and_failure(self):
        submitted = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, data, status=200):
                content = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                submitted.append(body)
                self.reply({'prompt_id': 'test-id'})
            def do_GET(self):
                if self.path == '/history/test-id':
                    self.reply({'test-id': {'status': {'completed': True, 'status_str': 'success'}, 'outputs': {}}})
                else:
                    self.reply({'error-id': {'status': {'completed': False, 'status_str': 'error', 'messages': [['execution_error', {'exception_message': 'OOM'}]]}}})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = app.ComfyClient(f'http://127.0.0.1:{server.server_port}')
            prompt = {'1': {'class_type': 'Source', 'inputs': {'seed': 7}}}
            self.assertEqual(client.submit(prompt), 'test-id')
            self.assertEqual(submitted[0]['prompt'], prompt)
            self.assertTrue(client.wait('test-id', 5, lambda _: None)['status']['completed'])
            with self.assertRaisesRegex(RuntimeError, 'OOM'):
                client.wait('error-id', 5, lambda _: None)
        finally:
            server.shutdown()
            server.server_close()

    def test_batch_order_failure_continue_and_reports(self):
        class FakeClient:
            url = 'http://localhost:8188'
            def __init__(self):
                self.seeds = []
            def call(self, route):
                return {}
            def submit(self, graph):
                self.seeds.append(graph['1']['inputs']['seed'])
                return str(len(self.seeds))
            def wait(self, prompt_id, timeout, log):
                if prompt_id == '1':
                    raise RuntimeError('GPU failure')
                return {'outputs': {'1': {'images': [{'filename': 'a.png', 'subfolder': '', 'type': 'output'}]}}}
        import queue
        class Harness:
            events = queue.Queue()
            stop_event = threading.Event()
        fake = FakeClient()
        graph = {'1': {'class_type': 'Source', 'inputs': {'seed': '{{seed}}'}}}
        files = [{'path': Path('a.json'), 'item': 'a', 'names': {'seed'}, 'data': graph, 'kind': 'API'}]
        config = {'seed': {'type': 'int', 'mode': '递增', 'value': '100'}}
        with tempfile.TemporaryDirectory() as temp:
            app.BatchApp.worker(Harness(), fake, files, 2, 5, config, '', Path(temp))
            records = app.read_json(Path(temp) / 'report.json')
            self.assertEqual(fake.seeds, [100, 101])
            self.assertEqual([r['status'] for r in records], ['失败', '成功'])
            self.assertIn('/view?', records[1]['outputs'][0]['url'])

if __name__ == '__main__':
    unittest.main()
