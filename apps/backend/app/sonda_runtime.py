"""Build readable, self-contained native source from reviewed Python modules.

This is a build-time operation. Native execution contains ordinary Python source,
not encoded archives, dynamic source downloads or an exec-based module loader.
"""
import ast
from pathlib import Path
from .lab_packs import module_runtime_source, shared_runtime_source

ROOT = Path(__file__).resolve().parent


class NativeImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        if node.module == '__future__' or (node.module or '').startswith('app.'):
            return None
        if node.level and any(alias.name == 'sonda_contract' for alias in node.names):
            return None
        return node

    def visit_Attribute(self,node):
        if isinstance(node.value,ast.Name) and node.value.id == 'contract':
            return ast.copy_location(ast.Name(id=node.attr,ctx=node.ctx),node)
        return self.generic_visit(node)


def readable_source(paths,*,excluded=None):
    """Compose only fixed owning-repository paths, rejecting duplicate definitions."""
    sections, names = [], set()
    shared_runtime_source('ai_social_network','sonda_runtime.py')
    for relative in paths:
        path = (ROOT/relative).resolve()
        if not path.is_relative_to(ROOT) or path.suffix != '.py':
            raise ValueError('Native source must be an owning Python module')
        if relative.startswith('labs/ai_social_network/'):
            raw=module_runtime_source('ai_social_network',relative.removeprefix('labs/ai_social_network/'))
        else:
            raw=shared_runtime_source('ai_social_network',relative)
        tree = ast.parse(raw.decode('utf-8'))
        omitted=(excluded or {}).get(relative,set())
        tree.body=[node for node in tree.body if not (isinstance(node,
            (ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name in omitted)]
        defined = {node.name for node in tree.body
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}
        if names & defined:
            raise ValueError('Native modules define conflicting names: '+','.join(sorted(names&defined)))
        names.update(defined)
        tree=ast.fix_missing_locations(NativeImports().visit(tree))
        sections.append('# Source: '+relative+'\n'+ast.unparse(tree))
    source = '\n\n'.join(sections)+'\n'
    compile(source,'sonda-native.py','exec')  # Syntax check only; never execute during packaging.
    return source.encode('utf-8')


def investigator_source():
    return readable_source(('sonda_contract.py','sonda_source_limits.py',
        'sonda_exchange.py','sonda_activity_runtime.py','sonda_activity.py',
        'labs/ai_social_network/notebooks/sonda_io.py',
        'sonda_activity_entry.py',
        'labs/ai_social_network/notebooks/sonda_tools.py',
        'labs/ai_social_network/notebooks/agent/sonda_x_research.py',
        'labs/ai_social_network/notebooks/agent/sonda_study.py',
        'labs/ai_social_network/notebooks/agent/sonda_research_tools.py',
        'labs/ai_social_network/notebooks/agent/sonda_model.py',
        'labs/ai_social_network/notebooks/agent/sonda_memory.py',
        'labs/ai_social_network/notebooks/agent/sonda_worker.py'))


def ingestion_source():
    return readable_source(('sonda_contract.py','sonda_source_limits.py',
        'sonda_source_limits_transport.py','sonda_exchange.py',
        'sonda_activity_runtime.py','sonda_activity.py',
        'labs/ai_social_network/notebooks/sonda_io.py',
        'sonda_activity_entry.py',
        'labs/ai_social_network/notebooks/sonda_tools.py',
        'labs/ai_social_network/notebooks/agent/sonda_x_research.py',
        'labs/ai_social_network/notebooks/agent/sonda_study.py',
        'sonda_activity_admission.py',
        'labs/ai_social_network/notebooks/sonda_delta.py',
        'labs/ai_social_network/notebooks/sonda_limits_io.py',
        'labs/ai_social_network/notebooks/sonda_runner.py'))


def chat_source():
    legacy='labs/ai_social_network/notebooks/agent/social_agent.py'
    return readable_source(('sonda_contract.py','sonda_exchange.py','sonda_activity_runtime.py','sonda_activity.py',
        'labs/ai_social_network/notebooks/sonda_io.py','sonda_activity_entry.py',
        legacy,'labs/ai_social_network/notebooks/agent/sonda_chat.py',
        'labs/ai_social_network/notebooks/agent/sonda_memory.py',
        'labs/ai_social_network/notebooks/agent/sonda_chat_entry.py'),excluded={legacy:{
            'SocialNetworkAgent','relation','snapshot_sql','checked_snapshot','tool_sql',
            'query_evidence','input_context','social_conditions','social_period_conditions',
            'social_query_source','social_summary_sql','checked_saved_network','checked_saved_search',
            'social_chat_model','compile_social_graph'}})
