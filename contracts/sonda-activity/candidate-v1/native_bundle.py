"""Inactive offline prototype: readable Activity source from its canonical files.

No files are read or written by bundle_source. It does not package, install or
execute the result. Callers remain responsible for canonical source hashes and
review. Unsupported scope or loader changes fail instead of using a runtime
module loader. Python model names are private and namespaced; wire fields and
model_dump output are preserved, not generated JSON-schema titles or $defs names.
"""
import ast
import builtins
import hashlib
import symtable


PREFIXES = {'read': 'ActivityRead_', 'producer': 'ActivityProducer_', 'public': 'ActivityPublic_', 'journal': 'ActivityJournal_', 'results': 'ActivityResult_'}
SAFE_IMPORTS = {
    ('import', 'json', None), ('import', 'hashlib', None), ('import', 'copy', None), ('import', 'inspect', None),
    ('import', 'time', None), ('import', 'math', None),
    ('from', 'dataclasses', 'dataclass'), ('from', 'uuid', 'uuid4'),
    ('from', 'copy', 'deepcopy'), ('from', 'contextlib', 'aclosing'),
    ('from', 'datetime', 'datetime'), ('from', 'datetime', 'timezone'), ('from', 'uuid', 'UUID'),
    *{('from', 'typing', name) for name in ('Annotated', 'Any', 'Literal', 'Union')},
    *{('from', 'pydantic', name) for name in ('AwareDatetime', 'BaseModel', 'ConfigDict', 'Field',
        'StrictBool', 'TypeAdapter', 'model_validator', 'BeforeValidator', 'field_validator')},
}
FORBIDDEN_NAMES = frozenset(('__file__', '__import__', 'importlib', 'Path', 'sys', 'open',
    'exec', 'eval', 'compile', 'globals', 'locals', '_read_spec', '_spec'))
PRODUCER_LOADER = '''
import importlib.util
from pathlib import Path
import sys
_read_spec = importlib.util.spec_from_file_location(
    '_sonda_activity_producer_read_candidate', Path(__file__).with_name('reference.py'))
_read = importlib.util.module_from_spec(_read_spec)
sys.modules[_read_spec.name] = _read
_read_spec.loader.exec_module(_read)
'''

JOURNAL_LOADER = """
import importlib.util
from pathlib import Path
import sys
_spec = importlib.util.spec_from_file_location('_sonda_activity_journal_producer', Path(__file__).with_name('producer.py'))
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)
"""
RESULTS_LOADER = """
import importlib.util
from pathlib import Path
import sys
_spec = importlib.util.spec_from_file_location('_sonda_activity_results_producer', Path(__file__).with_name('producer.py'))
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)
"""
SIBLINGS = {'producer': '_read', 'journal': 'p', 'results': 'p'}


def _shape(node):
    return ast.dump(node, include_attributes=False)


def _strip_fixed_loader(tree, label):
    source = {'producer':PRODUCER_LOADER, 'journal':JOURNAL_LOADER, 'results':RESULTS_LOADER}[label]
    expected = [_shape(node) for node in ast.parse(source).body]
    actual = [_shape(node) for node in tree.body]
    if any(actual.count(shape) != 1 for shape in expected):
        raise ValueError('Producer sibling loader differs from its reviewed fixed AST')
    positions = [actual.index(shape) for shape in expected]
    if positions != sorted(positions) or positions[3:] != list(range(positions[3], positions[3]+4)):
        raise ValueError('Producer sibling loader order differs from its reviewed fixed AST')
    tree.body = [node for node in tree.body if _shape(node) not in expected]


def _target_names(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _target_names(item)]
    raise ValueError('Only static named top-level assignment targets are supported')


def _own_bindings(tree):
    names = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(name for target in node.targets for name in _target_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.extend(_target_names(node.target))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise ValueError('Unsupported Activity top-level statement')
    if len(set(names)) != len(names) or any(name.startswith('__') for name in names):
        raise ValueError('Duplicate or special Activity top-level binding')
    return set(names)


def _check_import(node):
    if isinstance(node, ast.Import):
        bindings = [('import', alias.name, None) for alias in node.names]
    else:
        if node.level:
            raise ValueError('Relative Activity imports are unsupported in a native bundle')
        bindings = [('from', node.module, alias.name) for alias in node.names]
    if any(alias.asname for alias in node.names) or any(item not in SAFE_IMPORTS for item in bindings):
        raise ValueError('Activity bundle import is outside the explicit shared dependency allowlist')


def _annotation_name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _check_annotation(node, string_aliases):
    if node is None:
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            raise ValueError('String forward annotations require an explicit reviewed transformation')
        return
    if isinstance(node, ast.Name) and node.id in string_aliases:
        raise ValueError('String-valued annotation aliases require explicit review')
    if isinstance(node, ast.Subscript):
        name = _annotation_name(node.value)
        if name == 'Literal':
            return  # Enum string values are data, never symbol references.
        value = node.slice.elts[0] if name == 'Annotated' and isinstance(node.slice, ast.Tuple) else node.slice
        _check_annotation(value, string_aliases)
    elif isinstance(node, ast.Tuple):
        for item in node.elts:
            _check_annotation(item, string_aliases)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        _check_annotation(node.left, string_aliases)
        _check_annotation(node.right, string_aliases)


def _local_bindings(node):
    names = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
            names.add(item.id)
        elif isinstance(item, ast.arg):
            names.add(item.arg)
        elif item is not node and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(item.name)
        elif isinstance(item, ast.ExceptHandler) and item.name:
            names.add(item.name)
    return names


def _annotation_aliases(tree):
    return {name for node in tree.body if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets for name in _target_names(target)}


def _check_node_annotation(node, string_aliases):
    if isinstance(node, (ast.AnnAssign, ast.arg)):
        _check_annotation(node.annotation, string_aliases)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _check_annotation(node.returns, string_aliases)
    if isinstance(node, ast.Subscript) and _annotation_name(node.value) in ('Annotated', 'Union', 'Literal', 'list', 'dict', 'tuple', 'set'):
        _check_annotation(node, string_aliases)


def _check_scopes(tree, own):
    top_imports = {id(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))}
    string_aliases = _annotation_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.NamedExpr)):
            raise ValueError('Global/nonlocal or expression binding mutation is unsupported')
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                             ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if _local_bindings(node) & own:
                raise ValueError('Activity local binding shadows an owned module symbol')
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if id(node) not in top_imports:
                raise ValueError('Nested Activity imports require explicit review')
            _check_import(node)
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError('Runtime loaders or ambient filesystem names are unsupported')
        _check_node_annotation(node, string_aliases)


def _imported_bindings(tree):
    imported = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name for alias in node.names)
    return imported


def _check_global_references(tree, own, label):
    sibling = {SIBLINGS[label]} if label in SIBLINGS else set()
    allowed = own | _imported_bindings(tree) | set(dir(builtins)) | sibling
    pending = [symtable.symtable(ast.unparse(tree), label+'.py', 'exec')]
    while pending:
        table = pending.pop()
        if any(symbol.is_referenced() and symbol.is_global() and symbol.get_name() not in allowed
                for symbol in table.get_symbols()):
            raise ValueError('Activity source references an undeclared ambient global')
        pending.extend(table.get_children())


class _Namespace(ast.NodeTransformer):
    def __init__(self, tree, own, sibling_exports, label):
        self.own, self.exports = own, sibling_exports
        self.sibling = SIBLINGS.get(label)
        self.top_definitions = {id(node) for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    def visit_Name(self, node):
        if node.id in self.own:
            node.id = self.own[node.id]
        elif node.id == self.sibling:
            raise ValueError('Bare sibling module access is unsupported')
        return node

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == self.sibling:
            if not isinstance(node.ctx, ast.Load):
                raise ValueError('Mutation of canonical sibling exports is unsupported')
            if node.attr not in self.exports:
                raise ValueError('Unknown canonical read export in producer source')
            return ast.copy_location(ast.Name(id=self.exports[node.attr], ctx=node.ctx), node)
        return self.generic_visit(node)

    def _definition(self, node):
        if id(node) in self.top_definitions:
            node.name = self.own[node.name]
        return self.generic_visit(node)

    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition
    visit_ClassDef = _definition


def _prepared_sources(read, producer, public, journal_source=None, results_source=None):
    values, maps = {}, {}
    sources = [('read', read), ('producer', producer), ('public', public)]
    if journal_source is not None:
        sources.append(('journal', journal_source))
    if results_source is not None:
        sources.append(('results', results_source))
    for label, source in sources:
        if not isinstance(source, str) or len(source.encode('utf-8')) > 1048576:
            raise ValueError('Activity canonical source must be bounded UTF-8 text')
        normalized = source.replace('\r\n', '\n').replace('\r', '\n')
        tree = ast.parse(normalized, filename=label+'.py')
        if label in SIBLINGS:
            _strip_fixed_loader(tree, label)
        owned = _own_bindings(tree)
        sibling = {SIBLINGS[label]} if label in SIBLINGS else set()
        if owned & sibling:
            raise ValueError('Activity owned symbol shadows its sibling module')
        _check_scopes(tree, owned | sibling)
        _check_global_references(tree, owned, label)
        maps[label] = {name: PREFIXES[label]+name for name in sorted(owned)}
        values[label] = (normalized, tree)
    return values, maps


def binding_map(read, producer, public, journal_source=None, results_source=None):
    """Return every canonical class/function/alias/constant's private native name."""
    _, maps = _prepared_sources(read, producer, public, journal_source, results_source)
    return maps


def bundle_source(read, producer, public, journal_source=None, results_source=None):
    """Produce readable LF Python; never read sources or execute generated code."""
    values, maps = _prepared_sources(read, producer, public, journal_source, results_source)
    sections = ['# Inactive offline Activity bundle prototype; canonical sources remain authoritative.']
    for label in values:
        source, tree = values[label]
        exports = maps['read'] if label == 'producer' else {}
        if label in ('journal', 'results'):
            imported = _imported_bindings(values['producer'][1])
            exports = {**{name: name for name in imported}, **maps['producer']}
        transformed = ast.fix_missing_locations(_Namespace(tree, maps[label], exports, label).visit(tree))
        content = ast.unparse(transformed)
        ast.parse(content, filename='<activity-'+label+'>')
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        sections.append('# Canonical Activity '+label+' SHA256 '+digest+'\n'+content)
    result = '\n\n'.join(sections)+'\n'
    ast.parse(result, filename='<activity-native-bundle>')
    return result
