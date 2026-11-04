"""Evaluate the portable v2 graph over frozen captured documents, without legacy tables."""
import copy
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tomllib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.model import DecisionModel
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_core import Conflict,Scope
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_versions import exact_runtime_comparison,runtime_versions
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_state import V2RunStore
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_workflow import V2Workflow
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_collector import V2Collector
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_native import v2_question_urls
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.social import social_url
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.runtime_core import public_url
from market_model_bridge import StructuredBridge
from app.lab_packs import load_lab_pack


class V2EvaluationSnapshot:
    maximum_bytes=4*1024*1024

    def __init__(self,scope):
        self.scope=scope
        self.value=None
        self.revision=0

    def read(self):
        return copy.deepcopy(self.value),self.revision

    def write(self,value,etag):
        if etag!=self.revision:
            raise Conflict('Frozen evaluation state changed concurrently')
        self.value=copy.deepcopy(value)
        self.revision+=1


class V2EvaluationStore(V2RunStore):
    def __init__(self,snapshot,question,revision):
        super().__init__(snapshot,revision)
        self.question=question

    def begin(self,request_id,question,**kwargs):
        if question!=self.question:
            raise ValueError('This frozen document set belongs to a different question')
        return super().begin(request_id,question,**kwargs)


class V2FrozenCollector:
    def __init__(self,rows,*,explicit_urls=()):
        if any(count>50 for count in Counter(row['source'] for row in rows).values()):
            raise ValueError('Frozen source exceeds fifty documents; partition and review the corpus before inference')
        self.rows=copy.deepcopy(rows)
        self.calls=[]
        self.authority=V2Collector(None,None,explicit_urls=explicit_urls)

    async def collect(self,**arguments):
        self.calls.append(copy.deepcopy(arguments))
        source=arguments['task']['source']
        rows=[row for row in self.rows if row['source']==source]
        urls=arguments['task']['urls']
        if urls and not self.authority.explicit_targets_match(source,urls):
            raise ValueError('Frozen evaluation cannot retrieve a proposed or missing destination')
        if urls:
            canonical=lambda value:social_url(value,source) if source in {'facebook','instagram'} else public_url(value)
            selected={canonical(url) for url in urls}
            rows=[row for row in rows if selected&{canonical(url) for url in (row['url'],row.get('requested_url')) if url}]
        return {'status':'complete' if rows else 'unavailable','evidence':rows,
                'limitations':['Evaluación sobre documentos congelados; no se ejecutaron las consultas propuestas.'],
                'operation_id':arguments['operation_key']}


def v2_evaluation_graph(frozen,model,checkpointer,*,revision,conversation_context=None,enabled_sources=None):
    if frozen.get('version')!=2 or not isinstance(frozen.get('evidence'),dict) or not frozen.get('brief'):
        raise ValueError('A captured v2 request and its evidence inventory are required')
    context=copy.deepcopy(conversation_context or {})
    prior=context.pop('prior_report',None)
    if not frozen['evidence'] and not frozen['brief'].get('clarification') and frozen['brief'].get('intent','research')=='research':
        raise ValueError('Frozen research requires captured documents; absence is not a positive reasoning control')
    sources=frozen['brief']['sources'] or (prior['brief']['sources'] if prior else enabled_sources)
    snapshot=V2EvaluationSnapshot(Scope(**frozen['scope']))
    store=V2EvaluationStore(snapshot,frozen['question'],revision)
    collector=V2FrozenCollector(list(frozen['evidence'].values()),explicit_urls=v2_question_urls(frozen['question']))
    workflow=V2Workflow(store,model,collector,enabled_sources=sources,
        clock=lambda:datetime.fromisoformat(frozen['started_at']),conversation_context=context,prior_report=prior)
    return workflow.build(checkpointer),store,collector


def v2_checked_json(root,relative,checksum):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError('Evaluation input must stay in its owning project')
    raw=path.read_bytes()
    if len(raw)>4*1024*1024 or hashlib.sha256(raw).hexdigest()!=checksum:
        raise ValueError('Frozen evaluation input changed or exceeds its limit')
    return json.loads(raw)


def build_graph(runtime):
    if runtime['external_effects_allowed'] is not False:
        raise ValueError('Frozen v2 evaluation cannot start external tools')
    if type(runtime['model']).__name__=='FixtureModel':
        from market_v2_fixture_factory import build_graph as build_fixture_graph
        return build_fixture_graph(runtime)
    if getattr(runtime['model'],'client',None) is not None:
        raise ValueError('V2 evaluation requires a fresh model client whose identity and guardrails can be verified')
    root=Path.cwd()
    config=tomllib.loads((root/'aidp-agent.toml').read_text(encoding='utf-8'))
    evaluation=config['market_v2_test']
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=evaluation['factory_sha256']:
        raise ValueError('The v2 evaluation factory changed; review its binding')
    adapter=Path(__file__).with_name('market_model_bridge.py')
    if hashlib.sha256(adapter.read_bytes()).hexdigest()!=evaluation['adapter_sha256']:
        raise ValueError('The shared model adapter changed; review its binding')
    if load_lab_pack('ai_market_intelligence',require_available=False).pack_sha256!=evaluation['candidate_revision']:
        raise ValueError('The evaluation candidate differs from the current runtime package')
    frozen=v2_checked_json(root,evaluation['snapshot_file'],evaluation['snapshot_sha256'])
    binding=v2_checked_json(root,evaluation['binding_file'],evaluation['binding_sha256'])
    profile=binding['runtime_profile']
    if not exact_runtime_comparison(runtime_versions(tuple(profile['packages'])),profile)['exact_match']:
        raise ValueError('Local evaluation does not match the observed AIDP runtime')
    for key in ('model_id','region','compartment_id'):
        if config['model'][key]!=binding[key]:
            raise ValueError('Local/native model bindings differ')
    prompts=Path(__file__).resolve().parents[1]/'app/labs/ai_market_intelligence/notebooks/agent/ai_market_intelligence/v2_prompts.json'
    model=DecisionModel(StructuredBridge(runtime['model'],binding,
        observations_dir=root/'.aidp-local/v2/structured-responses'),prompts=json.loads(prompts.read_text(encoding='utf-8')))
    context={'scope_defaults':binding['search_context']}
    if evaluation.get('context_file'):
        context.update(v2_checked_json(root,evaluation['context_file'],evaluation['context_sha256']))
    graph,_,_=v2_evaluation_graph(frozen,model,runtime['checkpointer'],revision=evaluation['candidate_revision'],
                               conversation_context=context,enabled_sources=binding['enabled_sources'])
    return graph
