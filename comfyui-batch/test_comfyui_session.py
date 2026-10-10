import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import comfyui_batch as app

class SessionTests(unittest.TestCase):
    def test_restart_restores_values_types_modes_and_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'STATE_FILE', Path(temp) / 'last_session.json'):
            root = Path(temp)
            workflow = {'1': {'class_type': 'Example', 'inputs': {'seed': '{{seed}}', 'image': '{{image}}'}}}
            (root / 'workflow.json').write_text(json.dumps(workflow), encoding='utf-8')
            first = app.BatchApp()
            first.withdraw()
            with patch.object(app.filedialog, 'askdirectory', return_value=temp):
                first.import_folder()
            first.select_all(True)
            first.rows['image']['text'].insert('1.0', '中文.png\nsecond line')
            first.rows['seed']['text'].insert('1.0', '123')
            first.rows['seed']['mode'].set('递增')
            first.url.set('http://127.0.0.1:9000')
            first.update()
            # Debounced save fires while the window remains open.
            first.after(650, first.quit)
            first.mainloop()
            self.assertTrue(app.STATE_FILE.exists())
            first.select_all(False)
            self.assertTrue(first.autosave_session())
            first.close()
            second = app.BatchApp()
            second.withdraw()
            try:
                self.assertEqual(second.url.get(), 'http://127.0.0.1:9000')
                with patch.object(app.filedialog, 'askdirectory', return_value=temp):
                    second.import_folder()
                second.select_all(True)
                self.assertEqual(second.rows['image']['text'].get('1.0', 'end-1c'), '中文.png\nsecond line')
                self.assertEqual(second.rows['seed']['mode'].get(), '递增')
                self.assertEqual(second.rows['seed']['type'].get(), 'int')
                self.assertEqual(second.rows['seed']['text'].get('1.0', 'end-1c'), '123')
            finally:
                second.close()

    def test_corrupt_cache_does_not_prevent_startup(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'STATE_FILE', Path(temp) / 'last_session.json'):
            app.STATE_FILE.write_text('{bad', encoding='utf-8')
            window = app.BatchApp()
            window.withdraw()
            try:
                self.assertEqual(window.params, {})
                self.assertIn('无法读取上次参数', window.log_box.get('1.0', 'end'))
            finally:
                window.close()

if __name__ == '__main__':
    unittest.main()
