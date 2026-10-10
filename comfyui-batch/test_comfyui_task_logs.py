import queue
import tempfile
import threading
import unittest
from pathlib import Path
import comfyui_batch as app

GRAPH = {
    '42': {'class_type': 'MAIE_ExecuteTasks', 'inputs': {}},
    '105': {'class_type': 'ShowText|pysssss', 'inputs': {'text': ['42', 1]}},
    '209': {'class_type': 'ShowText|pysssss', 'inputs': {'text': ['42', 2]}},
    '999': {'class_type': 'ShowText|pysssss', 'inputs': {'text': ['42', 0]}}
}

class TaskLogTests(unittest.TestCase):
    def test_slot_indices_and_external_failure(self):
        history = {'outputs': {'105': {'text': ['API 503 service unavailable']},
                               '209': {'text': ['success|success|success|success|success|failure_other']},
                               '999': {'text': ['unrelated']}}}
        result = app.extract_task_result(GRAPH, history)
        self.assertEqual(result['nodes']['api_log'][0]['node_id'], '105')
        self.assertEqual(result['nodes']['image_status'][0]['node_id'], '209')
        self.assertEqual(len(result['image_statuses']), 6)
        self.assertEqual(result['failed_images'][0]['image_index'], 6)
        self.assertTrue(result['generation_failed'])
        self.assertIsNone(result['fallback'])
        with tempfile.TemporaryDirectory() as temp:
            record = {'file': 'workflow.json', 'prompt_id': 'abc'}
            app.save_task_logs(Path(temp), 0, record, GRAPH, history)
            text = Path(record['log_file']).read_text(encoding='utf-8')
            self.assertIn('API 503 service unavailable', text)
            self.assertIn('105', text)
            self.assertIn('209', text)
            self.assertTrue(Path(record['output_record_file']).exists())

    def test_success_does_not_write_failure_log(self):
        history = {'outputs': {'105': {'text': ['OK']}, '209': {'text': ['success|success']}}}
        with tempfile.TemporaryDirectory() as temp:
            record = {'file': 'a.json'}
            result = app.save_task_logs(Path(temp), 0, record, GRAPH, history)
            self.assertFalse(result['generation_failed'])
            self.assertNotIn('log_file', record)

    def test_missing_node_or_output_uses_longest_nested_string(self):
        graph = {k: v for k, v in GRAPH.items() if k != '105'}
        history = {'outputs': {'209': {'text': ['success|failure_api']},
                               '500': {'nested': [{'value': 'this is the longest diagnostic message in outputs'}]}}}
        result = app.extract_task_result(graph, history)
        self.assertEqual(result['fallback']['path'], ['500', 'nested', '0', 'value'])
        self.assertEqual(result['fallback']['missing_roles'], ['api_log'])
        self.assertTrue(result['generation_failed'])
        result = app.extract_task_result(GRAPH, {'outputs': {'105': {'text': ['API log only']}}})
        self.assertEqual(result['unobserved_task_nodes'], ['42'])
        self.assertFalse(result['generation_failed'])

    def test_empty_and_non_success_status_are_failures(self):
        result = app.extract_task_result(GRAPH, {'outputs': {'209': {'text': ['success||SUCCESS|timeout']}}})
        self.assertEqual([x['status'] for x in result['failed_images']], ['', 'SUCCESS', 'timeout'])

    def test_ui_showtext_display_widget_does_not_break_conversion(self):
        defs = {'MAIE_ExecuteTasks': {'input': {}}, 'ShowText|pysssss': {'input': {'required': {'text': ['STRING', {'forceInput': True}]}}}}
        ui = {'nodes': [{'id': 42, 'type': 'MAIE_ExecuteTasks', 'widgets_values': []},
                        {'id': 105, 'type': 'ShowText|pysssss', 'inputs': [{'name': 'text', 'link': 7}], 'widgets_values': [['old saved log']]},
                        {'id': 209, 'type': 'ShowText|pysssss', 'inputs': [{'name': 'text', 'link': 8}], 'widgets_values': [['old status']]}],
              'links': [[7, 42, 1, 105, 0, 'STRING'], [8, 42, 2, 209, 0, 'STRING']]}
        converted = app.convert_workflow(ui, defs)
        self.assertEqual(app.find_task_display_nodes(converted)['image_status'][0]['node_id'], '209')

    def test_worker_marks_silent_external_error_failed(self):
        class Fake:
            url = 'http://localhost:8188'
            def call(self, route):
                return {}
            def submit(self, prompt):
                return 'test-id'
            def wait(self, *args):
                return {'status': {'completed': True, 'status_str': 'success'}, 'outputs': {'105': {'text': ['API bad request']}, '209': {'text': ['success|failure_other']}}}
        class Harness:
            events = queue.Queue()
            stop_event = threading.Event()
        files = [{'path': Path('test.json'), 'item': 'a', 'kind': 'API', 'names': set(), 'data': GRAPH}]
        with tempfile.TemporaryDirectory() as temp:
            app.BatchApp.worker(Harness(), Fake(), files, 1, 5, {}, '', Path(temp))
            records = app.read_json(Path(temp) / 'report.json')
            self.assertEqual(records[0]['status'], '失败')
            self.assertIn('API bad request', Path(records[0]['log_file']).read_text(encoding='utf-8'))

    def test_execution_failure_preserves_partial_output_logs(self):
        class Fake:
            url = 'http://localhost:8188'
            def call(self, route):
                return {}
            def submit(self, prompt):
                return 'test-id'
            def wait(self, *args):
                raise app.ExecutionFailure('node error', {'outputs': {'105': {'text': ['partial API diagnostic']}}})
        class Harness:
            events = queue.Queue()
            stop_event = threading.Event()
        files = [{'path': Path('test.json'), 'item': 'a', 'kind': 'API', 'names': set(), 'data': GRAPH}]
        with tempfile.TemporaryDirectory() as temp:
            app.BatchApp.worker(Harness(), Fake(), files, 1, 5, {}, '', Path(temp))
            record = app.read_json(Path(temp) / 'report.json')[0]
            self.assertEqual(record['status'], '失败')
            self.assertIn('partial API diagnostic', Path(record['log_file']).read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main()
