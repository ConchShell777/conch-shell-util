"""ComfyUI batch runner. Python 3.10+, standard library only."""
from __future__ import annotations
import copy
import json
import queue
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
MAX_SEED = 2**64 - 1

def output_strings(value, path=()):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from output_strings(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from output_strings(child, path + (str(index),))


def find_task_display_nodes(prompt):
    graph = {str(k): v for k, v in prompt.items()}
    found = {'api_log': [], 'image_status': []}
    def origin(link, seen=None):
        seen = set() if seen is None else set(seen)
        if not isinstance(link, list) or len(link) != 2 or not isinstance(link[1], int):
            return None
        node_id, slot = str(link[0]), link[1]
        if node_id in seen:
            return None
        seen.add(node_id)
        node = graph.get(node_id, {})
        if node.get('class_type') == 'Reroute':
            return origin(next(iter(node.get('inputs', {}).values()), None), seen)
        if node.get('class_type') == 'MAIE_ExecuteTasks' and slot in (1, 2):
            return node_id, slot
        return None
    for node_id, node in graph.items():
        if node.get('class_type') != 'ShowText|pysssss':
            continue
        source = origin(node.get('inputs', {}).get('text'))
        if source:
            task_id, slot = source
            found['api_log' if slot == 1 else 'image_status'].append(
                {'node_id': node_id, 'task_node_id': task_id, 'output_index': slot})
    return found


def extract_task_result(prompt, history):
    mapping = find_task_display_nodes(prompt)
    outputs = {str(k): v for k, v in history.get('outputs', {}).items()}
    result = {'nodes': mapping, 'api_logs': [], 'status_outputs': [],
              'image_statuses': [], 'failed_images': [], 'fallback': None,
              'has_task_nodes': any(n.get('class_type') == 'MAIE_ExecuteTasks' for n in prompt.values())}
    for role, destination in (('api_log', 'api_logs'), ('image_status', 'status_outputs')):
        for node in mapping[role]:
            output = outputs.get(node['node_id'], {})
            text_output = output.get('text', []) if isinstance(output, dict) else []
            for path, text in output_strings(text_output):
                entry = {**node, 'path': ['text', *path], 'text': text}
                result[destination].append(entry)
                if role == 'image_status':
                    for index, state in enumerate(text.split('|'), 1):
                        state = state.strip()
                        item = {**node, 'text_item_path': list(path), 'image_index': index, 'status': state}
                        result['image_statuses'].append(item)
                        if state != 'success':
                            result['failed_images'].append(item)
    missing = [role for role, key in (('api_log', 'api_logs'), ('image_status', 'status_outputs'))
               if not mapping[role] or not result[key]]
    if missing:
        strings = list(output_strings(outputs))
        path, text = max(strings, key=lambda pair: len(pair[1])) if strings else ((), '')
        result['fallback'] = {'missing_roles': missing, 'path': list(path), 'text': text}
    task_ids = {str(k) for k, n in prompt.items() if n.get('class_type') == 'MAIE_ExecuteTasks'}
    observed = {x['task_node_id'] for x in result['status_outputs']}
    result['unobserved_task_nodes'] = sorted(task_ids - observed)
    result['generation_failed'] = bool(result['failed_images'])
    return result


class ExecutionFailure(RuntimeError):
    def __init__(self, message, history):
        super().__init__(message)
        self.history = history


def save_task_logs(report_dir, index, record, prompt, history, execution_error=None):
    result = extract_task_result(prompt, history)
    record['generation_result'] = result
    output_file = report_dir / f'{index + 1:05d}_outputs.json'
    output_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    record['output_record_file'] = str(output_file)
    failed = execution_error is not None or result['generation_failed']
    if failed or result['fallback'] is not None:
        suffix = 'failure_log' if failed else 'fallback_log'
        log_file = report_dir / f'{index + 1:05d}_{suffix}.txt'
        sections = [f"工作流：{record['file']}", f"任务 ID：{record.get('prompt_id', '')}"]
        if execution_error:
            sections.append('ComfyUI 执行错误：' + str(execution_error))
        sections.append('识别的节点：' + json.dumps(result['nodes'], ensure_ascii=False))
        for entry in result['status_outputs']:
            sections.append(f"图片状态（任务节点 {entry['task_node_id']}，ShowText 节点 {entry['node_id']}）：\n{entry['text']}")
        for entry in result['api_logs']:
            sections.append(f"API 日志（任务节点 {entry['task_node_id']}，ShowText 节点 {entry['node_id']}）：\n{entry['text']}")
        if result['fallback'] is not None:
            fallback = result['fallback']
            sections.append('缺少的日志来源：' + ', '.join(fallback['missing_roles']))
            sections.append('output 最长字符串，来源 ' + '/'.join(fallback['path']) + '：\n' + fallback['text'])
        if failed and not result['api_logs'] and not (result['fallback'] or {}).get('text'):
            sections.append('服务器 output 未返回可提取的 API 日志。')
        log_file.write_text('\n\n'.join(sections) + '\n', encoding='utf-8')
        record['log_file'] = str(log_file)
    return result


def placeholders(value):
    if isinstance(value, str):
        return {m.group(1).strip() for m in TOKEN.finditer(value)}
    if isinstance(value, (list, dict)):
        found = set()
        for child in (value.values() if isinstance(value, dict) else value):
            found.update(placeholders(child))
        return found
    return set()


def replace_values(value, values):
    if isinstance(value, str):
        match = TOKEN.fullmatch(value)
        if match:
            return values[match.group(1).strip()]
        return TOKEN.sub(lambda m: str(values[m.group(1).strip()]), value)
    if isinstance(value, list):
        return [replace_values(v, values) for v in value]
    if isinstance(value, dict):
        return {k: replace_values(v, values) for k, v in value.items()}
    return value


def parameter_values(config, index):
    result = {}
    for name, setting in config.items():
        if setting['type'] == 'string':
            result[name] = setting['value']
            continue
        if setting['type'] != 'int':
            raise ValueError(f'{name}: 不支持的类型')
        mode = setting.get('mode', '固定')
        if mode == '随机':
            result[name] = secrets.randbelow(MAX_SEED + 1)
            continue
        try:
            value = int(setting['value'].strip())
        except ValueError:
            raise ValueError(f'{name}: 请输入整数') from None
        if mode == '递增':
            if not 0 <= value <= MAX_SEED:
                raise ValueError(f'{name}: 递增种子须在 0 到 {MAX_SEED} 之间')
            value = (value + index) % (MAX_SEED + 1)
        elif mode != '固定':
            raise ValueError(f'{name}: 模式无效')
        result[name] = value
    return result


def read_json(path):
    with Path(path).open(encoding='utf-8-sig') as handle:
        return json.load(handle)


def workflow_kind(data):
    if not isinstance(data, dict):
        raise ValueError('JSON 顶层必须是对象')
    if isinstance(data.get('nodes'), list):
        return '普通'
    graph = data.get('prompt', data)
    if isinstance(graph, dict) and graph and all(
        isinstance(n, dict) and isinstance(n.get('class_type'), str)
        and isinstance(n.get('inputs'), dict) for n in graph.values()
    ):
        return 'API'
    raise ValueError('不是可识别的 API 或普通工作流')


def convert_workflow(data, definitions):
    """Reject ambiguous UI widget layouts instead of silently guessing."""
    if workflow_kind(data) == 'API':
        return copy.deepcopy(data.get('prompt', data))
    if data.get('definitions', {}).get('subgraphs'):
        raise ValueError('含子图的普通工作流请导出 API 格式')
    nodes = {str(n['id']): n for n in data['nodes']}
    links = {}
    for link in data.get('links', []):
        if isinstance(link, list) and len(link) >= 6:
            links[str(link[0])] = (str(link[1]), int(link[2]))
        elif isinstance(link, dict):
            links[str(link['id'])] = (str(link['origin_id']), int(link['origin_slot']))
        else:
            raise ValueError('无法识别连线格式，请导出 API 格式')

    def resolve(link_id, seen=None):
        seen = set() if seen is None else set(seen)
        key = str(link_id)
        if key in seen or key not in links:
            raise ValueError(f'无效或循环连线 {key}')
        seen.add(key)
        source, slot = links[key]
        node = nodes.get(source)
        if node is None:
            raise ValueError(f'不存在的节点 {source}')
        kind = node.get('type')
        mode = node.get('mode', 0)
        if mode == 2:
            raise ValueError(f'连线引用禁用节点 {source}')
        if kind == 'Reroute':
            ins = node.get('inputs', [])
            if not ins or ins[0].get('link') is None:
                raise ValueError(f'转接节点 {source} 未连接')
            return resolve(ins[0]['link'], seen)
        if kind == 'PrimitiveNode':
            widgets = node.get('widgets_values', [])
            if not isinstance(widgets, list) or not widgets:
                raise ValueError(f'常量节点 {source} 缺少值')
            return widgets[0]
        if mode == 4:
            raise ValueError(f'节点 {source} 处于绕过模式，请导出 API 格式')
        if kind not in definitions:
            raise ValueError(f'节点 {source} ({kind}) 不在服务器节点定义中')
        return [source, slot]

    result = {}
    for node_id, node in nodes.items():
        kind = node.get('type')
        if node.get('mode', 0) in (2, 4) or kind in ('Reroute', 'PrimitiveNode', 'Note', 'MarkdownNote'):
            continue
        if kind not in definitions:
            raise ValueError(f'节点 {node_id} ({kind}) 无法转换；请安装节点或导出 API 格式')
        definition = definitions[kind]
        schema = definition.get('input', {})
        order = definition.get('input_order', {})
        specs = []
        for section in ('required', 'optional'):
            group = schema.get(section, {})
            specs.extend((name, group[name]) for name in order.get(section, list(group)) if name in group)
        slots = {item['name']: item for item in node.get('inputs', [])}
        widgets = [] if kind == 'ShowText|pysssss' else node.get('widgets_values', [])
        if not isinstance(widgets, (list, dict)):
            raise ValueError(f'节点 {node_id}: 控件格式不支持')
        inputs, pos = {}, 0
        for name, spec in specs:
            dtype = spec[0]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            slot = slots.get(name, {})
            widget = isinstance(dtype, list) or dtype in ('INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO')
            widget = widget and not opts.get('forceInput', False)
            if slot and not slot.get('widget'):
                widget = False
            if widget:
                if isinstance(widgets, dict):
                    if name in widgets:
                        inputs[name] = widgets[name]
                    elif slot.get('link') is None:
                        raise ValueError(f'节点 {node_id}: 缺少控件 {name}')
                else:
                    if pos >= len(widgets):
                        raise ValueError(f'节点 {node_id}: 缺少控件 {name}，请导出 API 格式')
                    inputs[name] = widgets[pos]
                    pos += 1
                    if opts.get('control_after_generate') or name in ('seed', 'noise_seed'):
                        if pos < len(widgets) and widgets[pos] in ('fixed', 'increment', 'decrement', 'randomize'):
                            pos += 1
                    if opts.get('image_upload') and pos < len(widgets) and widgets[pos] in ('image', 'upload'):
                        pos += 1
            if slot.get('link') is not None:
                inputs[name] = resolve(slot['link'])
        if isinstance(widgets, list) and pos != len(widgets):
            raise ValueError(f'节点 {node_id} ({kind}): 额外控件值无法可靠转换，请导出 API 格式')
        result[node_id] = {'class_type': kind, 'inputs': inputs, '_meta': {'title': node.get('title', kind)}}
    if not result:
        raise ValueError('工作流中没有可执行节点')
    return result


class ComfyClient:
    def __init__(self, url):
        self.url = url.rstrip('/')
        parts = parse.urlparse(self.url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('请输入完整的 http:// 或 https:// 地址')
        self.client_id = str(uuid.uuid4())

    def call(self, route, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = request.Request(self.url + route, data=body, headers={'Content-Type': 'application/json'})
        try:
            with request.urlopen(req, timeout=20) as response:
                content = response.read()
                return json.loads(content) if content else {}
        except error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f'无法连接 ComfyUI: {exc}') from exc

    def submit(self, prompt):
        # Do not retry POST: the server may already have queued the request.
        response = self.call('/prompt', {'prompt': prompt, 'client_id': self.client_id})
        if not response.get('prompt_id'):
            raise ConnectionError(f'服务器未返回任务 ID，入队状态不明：{response}')
        return response['prompt_id']

    def wait(self, prompt_id, timeout, log):
        start, failures = time.monotonic(), 0
        while True:
            if time.monotonic() - start > timeout:
                raise ConnectionError(f'任务 {prompt_id} 等待超时；可能仍在执行，停止后续提交')
            try:
                history = self.call('/history/' + parse.quote(prompt_id, safe=''))
                failures = 0
            except (ConnectionError, RuntimeError, ValueError):
                failures += 1
                if failures >= 3:
                    raise ConnectionError(f'任务 {prompt_id} 状态无法确认，停止后续提交') from None
                log('连接暂时中断，重新查询任务状态')
                time.sleep(2)
                continue
            record = history.get(prompt_id)
            if record:
                status = record.get('status', {})
                errors = [m for m in status.get('messages', []) if m[0] in ('execution_error', 'execution_interrupted')]
                if status.get('status_str') == 'error' or errors:
                    raise ExecutionFailure('执行失败：' + json.dumps(errors or status, ensure_ascii=False), record)
                if status.get('completed'):
                    return record
            time.sleep(1)


class BatchApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('ComfyUI 批量生图测试')
        self.geometry('1180x820')
        self.minsize(880, 620)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self.files, self.items, self.params, self.rows = {}, {}, {}, {}
        self.selected, self.names = set(), []
        self.install = tk.StringVar()
        self.url = tk.StringVar(value='http://127.0.0.1:8188')
        self.repeat = tk.StringVar(value='1')
        self.timeout = tk.StringVar(value='3600')
        self.status = tk.StringVar(value='请导入工作流文件夹')
        self.make_ui()
        self.after(100, self.drain)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def make_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill='x')
        for row, (label, variable) in enumerate((('ComfyUI 目录', self.install), ('服务地址', self.url))):
            ttk.Label(top, text=label).grid(row=row, column=0, sticky='w', pady=4)
            ttk.Entry(top, textvariable=variable).grid(row=row, column=1, sticky='ew', padx=6)
        ttk.Button(top, text='选择目录', command=self.choose_install).grid(row=0, column=2)
        self.connect_button = ttk.Button(top, text='测试连接', command=self.test_connection)
        self.connect_button.grid(row=1, column=2)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='请先自行启动 ComfyUI。目录用于定位本地输出；图片输入需已存在于服务器 input 目录。').grid(row=2, column=0, columnspan=3, sticky='w')
        tools = ttk.Frame(self, padding=(10, 0, 10, 8))
        tools.pack(fill='x')
        for label, callback in (('导入文件夹', self.import_folder), ('一键全选', lambda: self.select_all(True)),
                                ('取消全选', lambda: self.select_all(False)), ('保存配置', self.save_config), ('加载配置', self.load_config)):
            ttk.Button(tools, text=label, command=callback).pack(side='left', padx=(0, 6))
        pane = ttk.Panedwindow(self, orient='horizontal')
        pane.pack(fill='both', expand=True, padx=10)
        left = ttk.LabelFrame(pane, text='占位符参数（来自已勾选文件）', padding=6)
        right = ttk.LabelFrame(pane, text='文件层级（点击名称勾选；文件夹可整组选中）', padding=6)
        pane.add(left, weight=2)
        pane.add(right, weight=3)
        self.canvas = tk.Canvas(left, highlightthickness=0)
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.param_frame = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.param_frame, anchor='nw')
        self.param_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.tree = ttk.Treeview(right, columns=('format', 'state'), selectmode='browse')
        for column, label, width in (('#0', '文件层级', 280), ('format', '格式', 65), ('state', '状态', 140)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, stretch=column != 'format')
        scroll = ttk.Scrollbar(right, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Button-1>', self.tree_click)
        self.tree.bind('<space>', lambda e: self.toggle(self.tree.focus()))
        self.tree.bind('<<TreeviewSelect>>', self.show_file)
        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill='x')
        ttk.Label(bottom, text='每个文件重复次数').pack(side='left')
        ttk.Spinbox(bottom, from_=1, to=10000, width=6, textvariable=self.repeat).pack(side='left', padx=5)
        ttk.Label(bottom, text='单任务等待上限（秒）').pack(side='left', padx=(10, 0))
        ttk.Entry(bottom, textvariable=self.timeout, width=8).pack(side='left', padx=5)
        self.run_button = ttk.Button(bottom, text='执行选中工作流', command=self.run_batch)
        self.run_button.pack(side='right')
        self.stop_button = ttk.Button(bottom, text='停止后续执行', command=self.stop, state='disabled')
        self.stop_button.pack(side='right', padx=6)
        self.progress = ttk.Progressbar(self, mode='determinate')
        self.progress.pack(fill='x', padx=10)
        ttk.Label(self, textvariable=self.status).pack(anchor='w', padx=10, pady=3)
        self.log_box = ScrolledText(self, height=9, wrap='word', state='disabled')
        self.log_box.pack(fill='x', padx=10, pady=(0, 10))

    def log(self, text):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', f'[{datetime.now():%H:%M:%S}] {text}\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def choose_install(self):
        if not self.busy:
            path = filedialog.askdirectory(title='选择 ComfyUI 或便携版根目录')
            if path:
                self.install.set(path)

    def test_connection(self):
        if self.busy:
            return
        url = self.url.get().strip()
        self.connect_button.configure(state='disabled')
        def worker():
            try:
                client = ComfyClient(url)
                client.call('/system_stats')
                definitions = client.call('/object_info')
                text = f'连接成功，服务器提供 {len(definitions)} 个节点类型'
            except Exception as exc:
                text = str(exc)
            self.events.put(('connection', text))
        threading.Thread(target=worker, daemon=True).start()

    def import_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(title='选择工作流文件夹')
        if not folder:
            return
        self.capture_params()
        root = Path(folder)
        self.files.clear()
        self.selected.clear()
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        root_item = self.tree.insert('', 'end', text=root.name, open=True)
        self.items[root_item] = root
        directories = {root: root_item}
        try:
            paths = sorted(root.rglob('*'), key=lambda p: str(p.relative_to(root)).casefold())
            for path in paths:
                if not path.is_file() or path.suffix.lower() != '.json':
                    continue
                parent = root
                for part in path.parent.relative_to(root).parts:
                    child = parent / part
                    if child not in directories:
                        directories[child] = self.tree.insert(directories[parent], 'end', text=part, open=True)
                        self.items[directories[child]] = child
                    parent = child
                info = {'path': path}
                try:
                    data = read_json(path)
                    info.update(data=data, kind=workflow_kind(data), names=placeholders(data), error='')
                except Exception as exc:
                    info.update(kind='无效', names=set(), error=str(exc))
                item = self.tree.insert(directories[parent], 'end', text=path.name, values=(info['kind'], '无效' if info['error'] else '待执行'))
                info['item'] = item
                self.items[item] = path
                self.files[path] = info
        except Exception as exc:
            self.log(f'读取文件夹出错：{exc}')
        self.refresh_tree()
        self.rebuild_params()
        self.log(f'已导入 {len(self.files)} 个 JSON 文件：{folder}')

    def descendants(self, item):
        path = self.items.get(item)
        if path in self.files:
            return {path} if not self.files[path]['error'] else set()
        return {p for p, info in self.files.items() if not info['error'] and path in p.parents}

    def toggle(self, item):
        if self.busy or item not in self.items:
            return
        paths = self.descendants(item)
        if paths and paths <= self.selected:
            self.selected.difference_update(paths)
        else:
            self.selected.update(paths)
        self.refresh_tree()
        self.rebuild_params()

    def tree_click(self, event):
        if self.tree.identify_column(event.x) == '#0' and 'indicator' not in self.tree.identify_element(event.x, event.y):
            self.toggle(self.tree.identify_row(event.y))

    def select_all(self, checked):
        if self.busy:
            return
        self.selected = {p for p, info in self.files.items() if not info['error']} if checked else set()
        self.refresh_tree()
        self.rebuild_params()

    def refresh_tree(self):
        for item, path in self.items.items():
            paths = self.descendants(item)
            mark = '☑' if paths and paths <= self.selected else '◩' if paths & self.selected else '☐'
            if path in self.files and self.files[path]['error']:
                mark = '⚠'
            self.tree.item(item, text=f'{mark} {path.name}')
        self.status.set(f'已选择 {len(self.selected)} / {len(self.files)} 个文件')

    def show_file(self, event=None):
        path = self.items.get(self.tree.focus())
        if path in self.files and self.files[path]['error']:
            self.log(f"{path.name}: {self.files[path]['error']}")

    def capture_params(self):
        for name, row in self.rows.items():
            self.params[name] = {'type': row['type'].get(), 'mode': row['mode'].get(), 'value': row['text'].get('1.0', 'end-1c')}

    def rebuild_params(self):
        self.capture_params()
        self.rows.clear()
        for child in self.param_frame.winfo_children():
            child.destroy()
        names = set()
        for path in self.selected:
            names.update(self.files[path]['names'])
        self.names = sorted(names, key=str.casefold)
        if not names:
            ttk.Label(self.param_frame, text='勾选 JSON 后显示 {{名称}}；无占位符也可执行。', wraplength=340).pack(anchor='w', pady=10)
        for name in self.names:
            saved = self.params.get(name, {'type': 'int' if 'seed' in name.lower() or '种子' in name else 'string', 'value': '', 'mode': '固定'})
            box = ttk.LabelFrame(self.param_frame, text=name, padding=5)
            box.pack(fill='x', pady=4)
            line = ttk.Frame(box)
            line.pack(fill='x')
            kind = tk.StringVar(value=saved['type'])
            mode = tk.StringVar(value=saved.get('mode', '固定'))
            types = ttk.Combobox(line, textvariable=kind, values=('string', 'int'), state='readonly', width=8)
            types.pack(side='left')
            modes = ttk.Combobox(line, textvariable=mode, values=('固定', '随机', '递增'), state='readonly', width=7)
            modes.pack(side='left', padx=6)
            text = tk.Text(box, height=3, wrap='word', undo=True)
            text.pack(fill='x', pady=(5, 0))
            text.insert('1.0', saved.get('value', ''))
            row = {'type': kind, 'mode': mode, 'text': text, 'modes': modes}
            self.rows[name] = row
            types.bind('<<ComboboxSelected>>', lambda e, r=row: self.update_mode(r))
            self.update_mode(row)

    def update_mode(self, row):
        if row['type'].get() == 'string':
            row['mode'].set('固定')
            row['modes'].configure(state='disabled')
        else:
            row['modes'].configure(state='readonly')

    def save_config(self):
        self.capture_params()
        path = filedialog.asksaveasfilename(title='保存参数配置（建议存到工作流文件夹外）', defaultextension='.json')
        if path:
            try:
                data = {'version': 1, 'url': self.url.get(), 'install': self.install.get(), 'repeat': self.repeat.get(), 'timeout': self.timeout.get(), 'parameters': self.params}
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                self.log(f'配置已保存：{path}')
            except Exception as exc:
                messagebox.showerror('保存失败', str(exc))

    def load_config(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[('JSON', '*.json')])
        if not path:
            return
        try:
            data = read_json(path)
            params = data['parameters']
            if not isinstance(params, dict):
                raise ValueError('parameters 必须是对象')
            for name, value in params.items():
                if not isinstance(value, dict) or value.get('type') not in ('string', 'int') or not isinstance(value.get('value'), str) or value.get('mode', '固定') not in ('固定', '随机', '递增'):
                    raise ValueError(f'参数配置无效：{name}')
            self.rows.clear()
            self.params = params
            self.url.set(data.get('url', self.url.get()))
            self.install.set(data.get('install', ''))
            self.repeat.set(str(data.get('repeat', '1')))
            self.timeout.set(str(data.get('timeout', '3600')))
            self.rebuild_params()
            self.log('参数配置已加载；请另行导入工作流文件夹')
        except Exception as exc:
            messagebox.showerror('配置无效', str(exc))

    def set_busy(self, busy):
        self.busy = busy
        self.run_button.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if busy else 'disabled')
        self.connect_button.configure(state='disabled' if busy else 'normal')

    def run_batch(self):
        if self.busy:
            return
        try:
            if not self.selected:
                raise ValueError('请先勾选工作流')
            repeats, timeout = int(self.repeat.get()), int(self.timeout.get())
            if repeats < 1 or timeout < 1:
                raise ValueError('重复次数和等待上限必须是正整数')
            client = ComfyClient(self.url.get().strip())
            self.capture_params()
            config = copy.deepcopy({name: self.params[name] for name in self.names})
            parameter_values(config, 0)
            files = [copy.deepcopy(info) for p, info in self.files.items() if p in self.selected]
            install = self.install.get().strip()
            report_dir = Path(__file__).resolve().parent / 'batch_results' / (datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6])
            report_dir.mkdir(parents=True)
        except Exception as exc:
            messagebox.showerror('无法执行', str(exc))
            return
        self.stop_event.clear()
        self.set_busy(True)
        self.progress.configure(maximum=len(files) * repeats, value=0)
        self.log(f'开始批次：{len(files)} 个文件 × {repeats} 次。报告目录：{report_dir}')
        self.log('参数和工作流已快照；界面编辑仅影响下一批次')
        threading.Thread(target=self.worker, args=(client, files, repeats, timeout, config, install, report_dir), daemon=True).start()

    def worker(self, client, files, repeats, timeout, config, install, report_dir):
        records, completed = [], 0
        emit = lambda text: self.events.put(('log', text))
        def save():
            temp = report_dir / 'report.json.tmp'
            temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(report_dir / 'report.json')
        def outputs(history):
            found = []
            base = Path(install) if install else None
            if base and (base / 'ComfyUI').is_dir():
                base /= 'ComfyUI'
            for node_id, output in history.get('outputs', {}).items():
                for field, items in output.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict) or 'filename' not in item:
                            continue
                        query = {k: item[k] for k in ('filename', 'subfolder', 'type') if k in item}
                        entry = {'node': node_id, 'field': field, **query, 'url': client.url + '/view?' + parse.urlencode(query)}
                        if base and item.get('type') in ('output', 'temp', 'input'):
                            entry['local_path_hint'] = str(base / item['type'] / item.get('subfolder', '') / item['filename'])
                        found.append(entry)
            return found
        try:
            client.call('/system_stats')
            definitions = client.call('/object_info') if any(f['kind'] == '普通' for f in files) else {}
            for info in files:
                for repeat in range(repeats):
                    if self.stop_event.is_set():
                        break
                    record = {'file': str(info['path']), 'repeat': repeat + 1, 'index': completed, 'started_at': datetime.now().isoformat(timespec='seconds'), 'status': '准备中'}
                    records.append(record)
                    self.events.put(('file_status', (info['item'], '准备中')))
                    halt = False
                    try:
                        values = parameter_values({k: config[k] for k in info['names']}, completed)
                        record['parameters'] = values
                        prompt = convert_workflow(replace_values(info['data'], values), definitions)
                        (report_dir / f'{completed + 1:05d}_prompt.json').write_text(json.dumps(prompt, ensure_ascii=False, indent=2), encoding='utf-8')
                        record['log_nodes'] = find_task_display_nodes(prompt)
                        emit('日志节点：' + json.dumps(record['log_nodes'], ensure_ascii=False))
                        record['status'] = '正在提交'
                        save()
                        if self.stop_event.is_set():
                            record['status'] = '未提交（用户停止）'
                            save()
                            break
                        try:
                            prompt_id = client.submit(prompt)
                        except ConnectionError as exc:
                            raise ConnectionError(f'提交响应丢失，可能已入队；请检查 ComfyUI 队列。{exc}') from exc
                        record.update(prompt_id=prompt_id, status='已入队')
                        save()
                        emit(f"{info['path'].name} 第 {repeat + 1} 次，任务 ID：{prompt_id}")
                        self.events.put(('file_status', (info['item'], '等待完成')))
                        history = client.wait(prompt_id, timeout, emit)
                        result = save_task_logs(report_dir, completed, record, prompt, history)
                        record.update(status='成功', outputs=outputs(history))
                        if result['generation_failed']:
                            record.update(status='失败', error=f"图片生成失败：{len(result['failed_images'])} / {len(result['image_statuses'])} 张状态不是 success")
                            emit(record['error'])
                        elif result['has_task_nodes'] and result['unobserved_task_nodes']:
                            record['status'] = '生成状态未确认'
                            emit('缺少任务节点的图片状态输出，已保存 output 最长字符串供检查')
                        if record.get('log_file'):
                            emit('日志文件：' + record['log_file'])
                        emit(f"完成：{info['path'].name}，输出文件 {len(record['outputs'])} 个")
                        for output in record['outputs']:
                            emit('输出：' + output['url'])
                            if output.get('local_path_hint'):
                                emit('本地路径参考：' + output['local_path_hint'])
                    except ExecutionFailure as exc:
                        record.update(status='失败', error=str(exc), outputs=outputs(exc.history))
                        save_task_logs(report_dir, completed, record, prompt, exc.history, execution_error=exc)
                        emit(f"{info['path'].name}：{exc}；日志：{record.get('log_file', '')}")
                    except ConnectionError as exc:
                        record.update(status='状态未确认', error=str(exc))
                        emit(str(exc))
                        halt = True
                    except Exception as exc:
                        record.update(status='失败', error=str(exc))
                        emit(f"{info['path'].name}：{exc}")
                    record['finished_at'] = datetime.now().isoformat(timespec='seconds')
                    save()
                    completed += 1
                    self.events.put(('progress', completed))
                    self.events.put(('file_status', (info['item'], record['status'])))
                    if halt:
                        self.stop_event.set()
                        break
                if self.stop_event.is_set():
                    break
            success = sum(r['status'] == '成功' for r in records)
            emit(f"批次{'已停止' if self.stop_event.is_set() else '结束'}：成功 {success}，失败 {sum(r['status'] == '失败' for r in records)}，生成状态未确认 {sum(r['status'] == '生成状态未确认' for r in records)}；报告：{report_dir / 'report.json'}")
        except Exception as exc:
            emit(f'批次停止：{exc}')
        finally:
            try:
                save()
            except Exception as exc:
                emit(f'无法保存报告：{exc}')
            self.events.put(('done', None))

    def stop(self):
        self.stop_event.set()
        self.stop_button.configure(state='disabled')
        self.log('已停止后续提交；已入队任务仍会完成，继续记录其结果')

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.log(value)
                elif kind == 'connection':
                    self.log(value)
                    if not self.busy:
                        self.connect_button.configure(state='normal')
                elif kind == 'progress':
                    self.progress['value'] = value
                    self.status.set(f"已处理 {value} / {int(self.progress['maximum'])} 个任务")
                elif kind == 'file_status':
                    item, state = value
                    if self.tree.exists(item):
                        self.tree.item(item, values=(self.tree.item(item, 'values')[0], state))
                elif kind == 'done':
                    self.set_busy(False)
                    self.status.set('批次已结束，详见日志和结果报告')
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def close(self):
        if self.busy:
            messagebox.showinfo('正在执行', '请先停止后续执行，等待已提交任务结束并保存报告后再关闭。')
        else:
            self.destroy()


if __name__ == '__main__':
    BatchApp().mainloop()
