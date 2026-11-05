"""Package and manage the isolated Market Intelligence current TEST through the installed plugin."""
from contextlib import contextmanager
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from market_common import market_project_path, session


REMOTE_ROOT='/Workspace/.artifact/ai_market_intelligence/agents/ai_market_intelligence'


def prepare_market(root,config_path,output):
    from app.packages.catalog import load_package
    from app.modules.ai_market_intelligence.installation.agent_package import agent_package, authoring_files
    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.runtime_versions import exact_runtime_comparison, runtime_versions
    config=json.loads(config_path.read_text(encoding='utf-8'))
    pack=load_package('ai_market_intelligence',require_available=False)
    if config.get('research_enabled'):
        services=config['services']
        if services['candidate_revision']!=pack.pack_sha256:
            raise ValueError('Runtime binding must select the current immutable package hash')
        profile=services['runtime_profile']
        if not exact_runtime_comparison(runtime_versions(tuple(profile['packages'])),profile)['exact_match']:
            raise ValueError('Prepare the package using the exactly aligned current environment')
    source,assets=agent_package(config)
    destination=market_project_path(root,output)
    destination.mkdir(parents=True,exist_ok=True)
    manifest={'schema_version':1,'candidate_revision':pack.pack_sha256,
        'source_sha256':hashlib.sha256(source).hexdigest(),'business_tables_required':False,
        'research_enabled':config['research_enabled']}
    for name,raw in {'market_intelligence.py':source,**assets,**authoring_files()}.items():
        (destination/name).parent.mkdir(parents=True,exist_ok=True)
        (destination/name).write_bytes(raw)
    (destination/'agent-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


def verify_market_target(root,config,*,candidate_required):
    if config['native']['remote_root']!=REMOTE_ROOT:
        raise ValueError('This command cannot manage another module source root')
    if not candidate_required:
        return
    source=market_project_path(root,config['agent']['entry'])
    manifest=json.loads(source.with_name('agent-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version')!=1 or hashlib.sha256(source.read_bytes()).hexdigest()!=manifest.get('source_sha256'):
        raise ValueError('The selected source is not its declared current candidate')


@contextmanager
def market_lifecycle(root,plugin_scripts,*,candidate_required):
    sys.path.insert(0,str(plugin_scripts))
    from aidp_agent_flow.native_lifecycle import NativeLifecycle
    from aidp_agent_flow.project import load_project
    from app.config import Settings
    from app.services.oci.aidp import AidpClient
    _,config=load_project(root)
    verify_market_target(root,config,candidate_required=candidate_required)
    if config.get('oci',{}).get('profile','DEFAULT')!='DEFAULT':
        raise ValueError('The project backend transport currently supports the DEFAULT uploaded operator profile')
    path=Path(config['oci']['config_file']).expanduser()
    if not path.is_absolute():
        path=root/path
    settings=Settings(oci_config_file=str(path),aidp_region=config['model']['region'],
                      aidp_platform_id=config['native']['platform_id'])
    client=AidpClient(settings)
    lifecycle=None
    try:
        client._list('/roles')
        matches=[row for row in client._list('/workspaces') if row.get('key')==config['native']['workspace_key']]
        if len(matches)!=1 or (matches[0].get('state') or matches[0].get('lifecycleState'))!='ACTIVE':
            raise ValueError('The exact configured native workspace is not active or accessible')
        client.session.auth=client.signer
        lifecycle=NativeLifecycle(root,config,transport=client.session)
        if lifecycle.base!=client.base:
            raise ValueError('Native plugin and project transport targets differ')
        yield lifecycle
    finally:
        if lifecycle is not None:
            lifecycle.close()
        client.session.close()
        client.object_storage.base_client.session.close()


def sync_market(lifecycle,*,apply):
    from aidp_agent_flow.project import FlowError
    for _ in range(4):
        try:
            return lifecycle.sync_package(apply=apply)
        except FlowError as error:
            if str(error)!='Native transport failed; reconcile any submitted write before retrying':
                raise
            lifecycle._reconcile()
            lifecycle._guard()
    raise FlowError('Source synchronization did not converge after four exact readback reconciliations')


def native_market_action(lifecycle,action,*,apply,label=None):
    if action=='status':
        observed=lifecycle.status()
        return {'action':'status','test_states':[row['lifecycleState'] for row in observed['test']],
                'production_deployments':len(observed['prod']),'native_test_accepted':False}
    if lifecycle.agent_key and lifecycle._deployments('PROD'):
        raise ValueError('This current TEST command cannot change source shared with a production deployment')
    if action=='sync':
        return sync_market(lifecycle,apply=apply)
    if action=='new-session':
        return session(lifecycle,label,apply=apply)
    method={'ensure-agent':lifecycle.ensure_agent,'test-init':lifecycle.test_init,'test-refresh':lifecycle.test_refresh}[action]
    return method(apply=apply)


def execute_market_command(lifecycle,args,root):
    if args.action=='pilot-run':
        from market_pilot import pilot_lock, run_pilot
        with pilot_lock(root):
            return run_pilot(lifecycle,root,apply=args.apply,cycle=args.cycle,resume_case=args.resume_case)
    if args.action=='invoke':
        if not args.apply or args.command is None or not args.label or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',args.label):
            raise ValueError('invoke requires --apply, --command and a unique bounded --label')
        from market_invoke import invoke_market
        raw=market_project_path(root,args.command).read_bytes()
        if len(raw)>4096:
            raise ValueError('The research command exceeds its input limit')
        return invoke_market(lifecycle,json.loads(raw),root/'.aidp-local/market/evaluations'/args.label,timeout=args.timeout)
    return native_market_action(lifecycle,args.action,apply=args.apply,label=args.label)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','status','sync','ensure-agent','test-init','test-refresh','new-session','invoke',
                                        'pilot-prepare','pilot-run'])
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--runtime-config',type=Path)
    parser.add_argument('--output',default='.aidp-local/market/candidate')
    parser.add_argument('--plugin-scripts',type=Path)
    parser.add_argument('--label')
    parser.add_argument('--command',type=Path)
    parser.add_argument('--definition',type=Path)
    parser.add_argument('--cycle',type=int,choices=[0,1,2],default=0)
    parser.add_argument('--resume-case')
    parser.add_argument('--timeout',type=int,default=300)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    root=args.project.resolve()
    sys.path.insert(0,str(root/'apps/backend'))
    if args.action=='pilot-prepare':
        if args.definition is None:
            parser.error('--definition is required for pilot-prepare')
        from market_pilot import pilot_lock, prepare_pilot
        with pilot_lock(root):
            result=prepare_pilot(root,args.definition,cycle=args.cycle)
    elif args.action=='prepare':
        if args.runtime_config is None:
            parser.error('--runtime-config is required for prepare')
        result=prepare_market(root,args.runtime_config,args.output)
    else:
        if args.plugin_scripts is None:
            parser.error('--plugin-scripts must identify the installed reviewed plugin scripts directory')
        with market_lifecycle(root,args.plugin_scripts,candidate_required=args.action!='status') as lifecycle:
            result=execute_market_command(lifecycle,args,root)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
