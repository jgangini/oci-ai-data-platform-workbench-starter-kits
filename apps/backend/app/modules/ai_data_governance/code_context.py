"""Bounded static references; source text is neither executed nor sent to a model."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from urllib.parse import quote, unquote

from .inventory import canonical
from .lineage import LineageTransport


def workspace_code_path(path):
    if (not isinstance(path, str) or len(path) > 2048 or not path.startswith('/Workspace/')
            or any(c in path for c in '\\%?#\x00') or any(p in {'', '.', '..'} for p in path.split('/')[1:])
            or not path.endswith(('.py', '.ipynb', '.scala'))):
        raise ValueError('A referenced workspace code file is required')
    return path


def workspace_job_name(job):
    name = job.get('name') if isinstance(job, dict) else None
    if isinstance(name, str) and name.strip() and len(name) <= 256 and name.isprintable():
        return name
    return None


def _literal_table(node):
    if not isinstance(node, ast.Call) or node.keywords or len(node.args) != 1:
        return None
    method = node.func
    if not isinstance(method, ast.Attribute) or method.attr != 'table':
        return None
    receiver = method.value
    if isinstance(receiver, ast.Attribute) and receiver.attr == 'read':
        receiver = receiver.value
    if not isinstance(receiver, ast.Name) or receiver.id != 'spark':
        return None
    value = node.args[0]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return _table_name(value.value)


def _table_name(value):
    parts = value.split('.')
    if len(parts) != 3 or any(not re.fullmatch(r'`?[A-Za-z_][A-Za-z0-9_]*`?', part) for part in parts):
        return None
    if any(part.startswith('`') != part.endswith('`') for part in parts):
        return None
    return '.'.join(part.strip('`') for part in parts)


def _notebook_language(notebook):
    metadata = notebook.get('metadata') or {}
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(k, {}), dict) for k in ('language_info', 'kernelspec')):
        raise ValueError('Invalid notebook language metadata')
    language = (metadata.get('language_info') or {}).get('name') or (metadata.get('kernelspec') or {}).get('language')
    if language is not None and not isinstance(language, str):
        raise ValueError('Invalid notebook language')
    return language


def _code_cells(path, source):
    if not path.endswith('.ipynb'):
        return [('python' if path.endswith('.py') else 'scala', None, source)]
    if path.endswith('.ipynb'):
        notebook = json.loads(source)
        if not isinstance(notebook, dict) or not isinstance(notebook.get('cells'), list) or len(notebook['cells']) > 1000:
            raise ValueError('A bounded notebook cell set is required')
        language = _notebook_language(notebook)
        cells = []
        for index, cell in enumerate(notebook['cells'], 1):
            if not isinstance(cell, dict):
                raise ValueError('Invalid notebook cell')
            if cell.get('cell_type') != 'code':
                continue
            text = cell.get('source')
            if isinstance(text, list) and all(isinstance(line, str) for line in text):
                text = ''.join(text)
            if not isinstance(text, str):
                raise ValueError('Invalid notebook code source')
            cells.append((language, index, text))
    return cells


def _node_references(node, cell):
    refs = []
    table = _literal_table(node)
    if table:
        refs.append({'table_key': table, 'column_name': None, 'cell': cell, 'line': node.lineno,
                     'operation': 'literal_spark_table_reference'})
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {'select', 'groupBy'}:
        table = _literal_table(node.func.value)
        if table and not node.keywords:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', arg.value):
                    refs.append({'table_key': table, 'column_name': arg.value, 'cell': cell, 'line': node.lineno,
                                 'operation': 'literal_column_' + node.func.attr})
    return refs


def _spark_sql_argument(node):
    if (isinstance(node, ast.Call) and not node.keywords and len(node.args) == 1
            and isinstance(node.func, ast.Attribute) and node.func.attr == 'sql'
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'spark'):
        return node.args[0]
    return None


def _ddl_reference(node, value, cell):
    if not isinstance(value, str) or len(value) > 65536:
        return []
    part = r'`?[A-Za-z_][A-Za-z0-9_]*`?'
    match = re.match(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?('
        + part + r'\s*\.\s*' + part + r'\s*\.\s*' + part + r')(?=\s|\()', value, re.I)
    table = _table_name(re.sub(r'\s*\.\s*', '.', match.group(1))) if match else None
    return ([{'table_key': table, 'column_name': None, 'cell': cell, 'line': node.lineno,
              'operation': 'literal_sql_create_table_reference'}] if table else [])


def _statement_mutations(node):
    if isinstance(node, ast.arg):
        return {node.arg}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.alias):
        return {node.asname or node.name.split('.')[0]}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return {node.func.value.id}
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.Assign):
        names = {node.value.id} if isinstance(node.value, ast.Name) else set()
        names.update(target.value.id for target in node.targets
                     if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name))
        return names
    return set()


def _literal_statement_lists(tree):
    assigned, allowed, invalid = {}, set(), set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id in assigned:
                        invalid.add(target.id)
                    assigned[target.id] = node.value
                    allowed.add(id(target))
    for node in ast.walk(tree):
        invalid.update(_statement_mutations(node))
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in allowed:
            invalid.add(node.id)
    return {name: value.elts for name, value in assigned.items()
            if name not in invalid and isinstance(value, (ast.List, ast.Tuple))
            and not any(isinstance(v, ast.Starred) for v in value.elts)}


def _statement_loop_values(loop, literals):
    if isinstance(loop.target, ast.Name) and isinstance(loop.iter, ast.Name):
        return {loop.target.id: literals.get(loop.iter.id, [])}
    if (not isinstance(loop.target, ast.Tuple) or not isinstance(loop.iter, ast.Call)
            or not isinstance(loop.iter.func, ast.Name) or loop.iter.func.id != 'zip'
            or loop.iter.keywords or len(loop.target.elts) != len(loop.iter.args)):
        return {}
    if any(not isinstance(source, ast.Name) or source.id not in literals for source in loop.iter.args):
        return {}
    count = min((len(literals[source.id]) for source in loop.iter.args), default=0)
    return {target.id: literals[source.id][:count] for target, source in zip(loop.target.elts, loop.iter.args)
            if isinstance(target, ast.Name)}


def _sql_declaration_references(tree, cell):
    refs, literals = [], _literal_statement_lists(tree)
    for node in ast.walk(tree):
        argument = _spark_sql_argument(node)
        if isinstance(argument, ast.Constant):
            refs.extend(_ddl_reference(node, argument.value, cell))
        if not isinstance(node, ast.For):
            continue
        values = _statement_loop_values(node, literals)
        body = [child for statement in node.body for child in ast.walk(statement)]
        overwritten = {child.id for child in body if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)}
        for child in body:
            overwritten.update(_statement_mutations(child))
        for child in body:
            argument = _spark_sql_argument(child)
            if not isinstance(argument, ast.Name) or argument.id in overwritten:
                continue
            for literal in values.get(argument.id, []):
                if not isinstance(literal, ast.Constant):
                    continue
                refs.extend(_ddl_reference(child, literal.value, cell))
                if len(refs) > 128:
                    return refs
    return refs


def source_references(path, source):
    path = workspace_code_path(path)
    if not isinstance(source, str) or len(source.encode()) > 2 * 1024 * 1024:
        raise ValueError('Code observation exceeds its byte budget')
    cells = _code_cells(path, source)
    digest = hashlib.sha256(canonical(cells).encode()).hexdigest()
    refs, skipped = [], 0
    for language, cell, text in cells:
        if language not in {'python', 'python3'}:
            skipped += 1
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            skipped += 1
            continue
        for count, _ in enumerate(ast.walk(tree), 1):
            if count > 100000:
                return {'source_hash': digest, 'references': refs[:128], 'truncated': True, 'skipped_cells': skipped}
        refs.extend(_sql_declaration_references(tree, cell))
        if len(refs) > 128:
            return {'source_hash': digest, 'references': refs[:128], 'truncated': True, 'skipped_cells': skipped}
        for count, node in enumerate(ast.walk(tree), 1):
            if count > 100000:
                return {'source_hash': digest, 'references': refs[:128], 'truncated': True, 'skipped_cells': skipped}
            refs.extend(_node_references(node, cell))
            if len(refs) > 128:
                return {'source_hash': digest, 'references': refs[:128], 'truncated': True, 'skipped_cells': skipped}
    return {'source_hash': digest, 'references': refs, 'truncated': False, 'skipped_cells': skipped}


def _validate_source_read(path, prefix):
    raw = path[len(prefix):]
    decoded = workspace_code_path(unquote(raw))
    if quote(decoded, safe='') != raw:
        raise ValueError('Noncanonical workspace source path')


class CodeContextTransport(LineageTransport):
    """Service-only job discovery and code reads in the installation workspace."""

    def __call__(self, method, path, *, params=None, payload=None):
        root = f'/workspaces/{self.workspace}'
        if method == 'GET' and payload is None:
            if path == root + '/jobs':
                if not isinstance(params, dict) or set(params) - {'limit', 'page'} or params.get('limit') != '32':
                    raise ValueError('Invalid bounded job enumeration')
                if 'page' in params and (not isinstance(params['page'], str) or not 1 <= len(params['page']) <= 1024):
                    raise ValueError('Invalid job page')
                return self._exchange(method, path, params=params)
            if path.startswith(root + '/jobs/') and re.fullmatch(r'[a-f0-9-]{36}', path[len(root + '/jobs/'):]) and params is None:
                return self._exchange(method, path)
            prefix = root + '/objects/'
            if path.startswith(prefix) and params is None:
                _validate_source_read(path, prefix)
                return self._exchange(method, path, text=True)
        return super().__call__(method, path, params=params, payload=payload)


def _task_sources(tasks):
    for task in tasks[:64]:
        if not isinstance(task, dict) or task.get('source') not in (None, 'WORKSPACE'):
            continue
        field = 'filePath' if task.get('type') == 'PYTHON_TASK' else 'notebookPath' if task.get('type') == 'NOTEBOOK_TASK' else None
        if field is None:
            continue
        try:
            path = workspace_code_path(task.get(field))
        except ValueError:
            continue
        task_key = task.get('taskKey')
        if not isinstance(task_key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,127}', task_key):
            continue
        yield path, task_key


class WorkspaceCodeReader:
    """One run-local reference cache; no workspace crawl or source persistence."""

    def __init__(self, request, workspace, *, check_active):
        self.request, self.workspace, self.check_active = request, workspace, check_active
        self.references = None
        self.truncated = False

    def find(self, table_key, column_name=None):
        if self.references is None:
            self.references = self._collect()
        return [ref for ref in self.references if ref['table_key'] == table_key and ref['column_name'] == column_name][:16]

    def _read(self, path):
        try:
            return self.request('GET', path)[0]
        except (RuntimeError, ValueError, UnicodeError):
            self.truncated = True
            return None

    def _observe_source(self, path, cache, total_bytes):
        root = f'/workspaces/{self.workspace}'
        if path not in cache:
            if len(cache) >= 32 or total_bytes >= 8 * 1024 * 1024:
                self.truncated = True
                return None, total_bytes
            self.check_active()
            source = self._read(root + '/objects/' + quote(path, safe=''))
            if not isinstance(source, str):
                self.truncated = True
                cache[path] = {'references': [], 'source_hash': '', 'skipped_cells': 1, 'truncated': True}
                return cache[path], total_bytes
            total_bytes += len(source.encode())
            if total_bytes > 8 * 1024 * 1024:
                self.truncated = True
                return None, total_bytes
            try:
                cache[path] = source_references(path, source)
            except (ValueError, RecursionError):
                self.truncated = True
                cache[path] = {'references': [], 'source_hash': '', 'skipped_cells': 1, 'truncated': True}
        observed = cache[path]
        return observed, total_bytes

    def _job_inventory(self):
        root = f'/workspaces/{self.workspace}'
        try:
            response, headers = self.request('GET', root + '/jobs', params={'limit': '32'})
        except (RuntimeError, ValueError, UnicodeError):
            self.truncated = True
            return []
        jobs = response.get('items') if isinstance(response, dict) else None
        if not isinstance(jobs, list) or len(jobs) > 32:
            self.truncated = True
            return []
        self.truncated = any(k.lower() == 'opc-next-page' and v for k, v in headers.items())
        return jobs

    def _collect(self):
        root = f'/workspaces/{self.workspace}'
        jobs = self._job_inventory()
        cache, refs, total_bytes = {}, [], 0
        for row in jobs:
            self.check_active()
            key = row.get('key') if isinstance(row, dict) else None
            if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9-]{36}', key):
                self.truncated = True
                continue
            job = self._read(root + '/jobs/' + key)
            if not isinstance(job, dict) or job.get('key') != key or not isinstance(job.get('tasks'), list):
                self.truncated = True
                continue
            self.truncated = self.truncated or len(job['tasks']) > 64
            job_name = workspace_job_name(job)
            job_label = {'job_name': job_name} if job_name is not None else {}
            for path, task_key in _task_sources(job['tasks']):
                observed, total_bytes = self._observe_source(path, cache, total_bytes)
                if observed is None:
                    continue
                self.truncated = self.truncated or observed['truncated'] or bool(observed['skipped_cells'])
                refs.extend({**ref, 'source_path': path, 'source_hash': observed['source_hash'], 'job_key': key,
                             'task_key': task_key, 'execution_evidence': False, **job_label}
                            for ref in observed['references'])
                if len(refs) > 1024:
                    self.truncated = True
                    return refs[:1024]
        return refs
