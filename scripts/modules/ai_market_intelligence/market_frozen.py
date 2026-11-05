"""Run the full portable graph with the real model over sealed native documents, without retrieval."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_frozen_study(root,snapshot_path,binding_path,context_path,study):
    from market import market_project_path
    from app.packages.catalog import load_package
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_versions import exact_runtime_comparison, runtime_versions
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',study):
        raise ValueError('A bounded unique study label is required')
    source=market_project_path(root,snapshot_path)
    binding=json.loads(market_project_path(root,binding_path).read_text(encoding='utf-8'))['services']
    profile=binding['runtime_profile']
    if not exact_runtime_comparison(runtime_versions(tuple(profile['packages'])),profile)['exact_match']:
        raise ValueError('Use the exactly aligned current Python environment for frozen evaluation')
    revision=load_package('ai_market_intelligence',require_available=False).pack_sha256
    if binding['candidate_revision']!=revision:
        raise ValueError('The runtime binding does not select the current candidate')
    captured=json.loads(source.read_text(encoding='utf-8'))
    if captured.get('version')!=2 or not isinstance(captured.get('evidence'),dict) or not captured.get('brief'):
        raise ValueError('The study requires a captured current request and its evidence inventory')
    context={} if context_path is None else json.loads(market_project_path(root,context_path).read_text(encoding='utf-8'))
    output=root/'.aidp-local/market/frozen-studies'/study
    output.mkdir(parents=True,exist_ok=False)
    for name,raw in [('request.json',source.read_bytes()),
                     ('binding.json',json.dumps(binding,sort_keys=True).encode()),
                     ('context.json',json.dumps(context,sort_keys=True).encode())]:
        (output/name).write_bytes(raw)
    fields={'snapshot_file':(output/'request.json').relative_to(root).as_posix(),
        'snapshot_sha256':checksum(output/'request.json'),
        'binding_file':(output/'binding.json').relative_to(root).as_posix(),
        'binding_sha256':checksum(output/'binding.json'),
        'context_file':(output/'context.json').relative_to(root).as_posix(),
        'context_sha256':checksum(output/'context.json'),
        'factory_sha256':checksum(root/'apps/backend/app/modules/ai_market_intelligence/aidp/workspaces/default/files/agents/ai_market_intelligence/ai_market_intelligence/factory.py'),
        'adapter_sha256':checksum(root/'apps/backend/app/modules/ai_market_intelligence/aidp/workspaces/default/files/agents/ai_market_intelligence/ai_market_intelligence/model_bridge.py'),'candidate_revision':revision}
    return output,captured,fields


def bind_frozen_study(root,fields):
    path=root/'aidp-agent.toml'
    original=path.read_text(encoding='utf-8')
    config=tomllib.loads(original)
    if config['agent']['factory']!='app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.factory:build_graph':
        raise ValueError('Select the reviewed current evaluation factory explicitly before running')
    section='[market_test]\n'+''.join(key+' = '+json.dumps(value)+'\n' for key,value in fields.items())
    updated=re.sub(r'(?ms)^\[market_test\]\n.*?(?=^\[|\Z)',lambda _:section+'\n',original)
    if updated==original and '[market_test]' not in original:
        updated=original.rstrip()+'\n\n'+section
    path.write_text(updated,encoding='utf-8')
    return hashlib.sha256(original.encode()).hexdigest()


async def run_frozen_study(root,output,captured,fields,*,expected_status):
    from aidp_agent_flow.worker import experiment
    case={'id':output.name,'input':{'request_id':output.name,'question':captured['question']},
        'require_model_call':True,'assertions':[{'path':'delivered','equals':True},
            {'path':'response.references','not_empty':True} if expected_status=='grounded' else
            {'path':'response.status','equals':expected_status}]}
    job={'project':str(root),'real':True,'max_steps':96,'case':case}
    before=bind_frozen_study(root,fields)
    (output/'intent.json').write_text(json.dumps({'job':job,'bindings':fields,'config_before_sha256':before,
        'authority':'Explicit unlimited current development authorization; separate study receipt, no campaign reset',
        'scope':'G2 full graph over fixed documents; no retrieval or native checkpoint proof',
        'semantic_accepted':False},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    progress={}
    try:
        result=await experiment(job,progress)
    except Exception as error:
        result={**progress,'passed':False,'error_type':type(error).__name__}
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    return {key:result.get(key) for key in ('passed','stage','failure_stage','error_type','model_calls','visited_nodes')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--plugin-scripts',type=Path,required=True)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--runtime-config',type=Path,required=True)
    parser.add_argument('--context',type=Path)
    parser.add_argument('--study',required=True)
    parser.add_argument('--expected-status',choices=['complete','partial','needs_clarification','pending','failed','grounded'],required=True)
    args=parser.parse_args()
    root=args.project.resolve()
    sys.path[:0]=[str(root/'apps/backend'),str(root/'apps/backend/tests'),str(args.plugin_scripts)]
    output,captured,fields=prepare_frozen_study(root,args.snapshot,args.runtime_config,args.context,args.study)
    result=asyncio.run(run_frozen_study(root,output,captured,fields,expected_status=args.expected_status))
    print(json.dumps({**result,'semantic_accepted':False}))


if __name__=='__main__':
    main()
