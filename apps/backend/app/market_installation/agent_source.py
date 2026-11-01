"""Build the readable standalone agent from explicitly selected, verified source."""
from __future__ import annotations

import ast
import io
import json
import pprint
import tokenize


MODULES = ('__init__', 'contracts', 'schema', 'repository', 'catalog', 'research',
           'finance', 'connectors', 'execution', 'proposals', 'presentation',
           'workflow_actions', 'workflow', 'model', 'memory', 'mcp_payload',
           'provider', 'streaming', 'native')
RESOURCES = {
    'schema': ('SCHEMA_DEFINITION', "json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))"),
    'model': ('AGENT_PROMPTS', "json.loads(Path(__file__).with_name('prompts.json').read_text(encoding='utf-8'))"),
    'native': ('RUNTIME_CONFIG', "json.loads(Path(__file__).with_name('runtime.json').read_text(encoding='utf-8'))"),
}
MCP_READ = "json.loads(Path(__file__).with_name('mcp_tools.json').read_text(encoding='utf-8'))"
ENTRY_CONTRACT = '''from ai_market_intelligence.native import AgentRuntime
class MarketIntelligenceAgent:
    def __init__(self):
        self.runtime = AgentRuntime()
    def setup(self):
        self.runtime.setup()
    async def invoke(self, user_query, **kwargs):
        return await self.runtime.invoke(user_query, **kwargs)
'''


def _span(source, node):
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    def position(line, byte_column):
        return offsets[line - 1] + len(lines[line - 1].encode('utf-8')[:byte_column].decode('utf-8'))
    return position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)


def _resource_edits(tree, module):
    recipes = [RESOURCES[module]] if module in RESOURCES else []
    if module == 'native':
        recipes.append(('MCP_CONTRACT', MCP_READ))
    edits = []
    for constant, expression in recipes:
        expected = ast.dump(ast.parse(expression, mode='eval').body)
        matches = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.dump(node) == expected]
        if len(matches) != 1:
            raise ValueError('Standalone resource boundary changed: ' + module + '/' + constant)
        edits.append((matches[0], 'copy.deepcopy(' + constant + ')'))
    return edits


def _import_replacement(node, exports):
    if node.module == '__future__':
        return ''
    if not node.level and node.module != 'ai_market_intelligence.native':
        return None
    origin = 'native' if node.module == 'ai_market_intelligence.native' else node.module or '__init__'
    if node.level > 1 or origin not in exports:
        raise ValueError('Unknown standalone import: ' + origin)
    if any(alias.asname or alias.name not in exports[origin] for alias in node.names):
        raise ValueError('Standalone import does not match an exported definition')
    return ''


def _fragment(source, module, exports):
    tree = ast.parse(source)
    edits = _resource_edits(tree, module)
    singles = {id(values[0]) for parent in ast.walk(tree) for _, values in ast.iter_fields(parent)
               if isinstance(values, list) and len(values) == 1}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            replacement = _import_replacement(node, exports)
            if replacement is not None:
                edits.append((node, 'pass' if id(node) in singles else replacement))
    if module == 'schema':
        edits.extend((node, '') for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'SCHEMA_PATH' for target in node.targets))
    spans = [(*_span(source, node), replacement) for node, replacement in edits]
    for start, end, replacement in sorted(spans, reverse=True):
        source = source[:start] + replacement + source[end:]
    renames = {'provider': {'_observation': '_provider_observation'},
               'native': {'AgentRuntime': 'MarketIntelligenceAgent'}}.get(module, {})
    if renames:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        source = tokenize.untokenize(token._replace(string=renames[token.string])
            if token.type == tokenize.NAME and token.string in renames else token for token in tokens)
    return source.strip()


def _definitions(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def assemble_agent(files, entry, config):
    """Only this build-time function combines source; the emitted program is ordinary Python."""
    if ast.dump(ast.parse(entry.decode('utf-8'))) != ast.dump(ast.parse(ENTRY_CONTRACT)):
        raise ValueError('Standalone entry wrapper changed; review its behavior before assembly')
    sources = {module: files[module + '.py'].decode('utf-8') for module in MODULES}
    exports = {module: _definitions(ast.parse(source)) for module, source in sources.items()}
    constants = {'SCHEMA_DEFINITION': json.loads(files['schema.json']),
                 'AGENT_PROMPTS': json.loads(files['prompts.json']),
                 'MCP_CONTRACT': json.loads(files['mcp_tools.json']),
                 'RUNTIME_CONFIG': json.loads(json.dumps(config, allow_nan=False))}
    parts = ['"""AI Market Intelligence: standalone readable AIDP agent. Secrets stay in Credential Store."""',
             'from __future__ import annotations', 'import copy']
    parts.extend('# %% ' + name + '\n' + name + ' = ' + pprint.pformat(value, width=100, sort_dicts=True)
                 for name, value in constants.items())
    parts.extend('# %% ' + module + '\n' + _fragment(source, module, exports)
                 for module, source in sources.items())
    source = '\n\n'.join(parts) + '\n'
    tree = ast.parse(source, filename='market_intelligence.py')
    names = [name for node in tree.body for name in _definitions(ast.Module(body=[node], type_ignores=[]))]
    if len(names) != len(set(names)):
        raise ValueError('Standalone global definitions collide')
    compile(tree, 'market_intelligence.py', 'exec')
    return source.encode('utf-8')
