"""Pin the reviewed Sonda native modules in the immutable social component pack."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'apps/backend/app'
LAB=APP/'labs/ai_social_network'


def render(check=False):
    metadata=json.loads((LAB/'lab.json').read_text(encoding='utf-8'))
    metadata['pack_version']='1.4.0'
    metadata['agent']['investigator_name']='ai_social_network_investigator'
    metadata['agent']['contract_version']='1.0.2'
    shared=('sonda_contract.py','sonda_exchange.py','sonda_runtime.py',
        'sonda_source_limits.py','sonda_source_limits_transport.py','sonda_activity_runtime.py',
        'sonda_activity.py','sonda_activity_entry.py','sonda_activity_admission.py')
    paths=sorted((LAB/'notebooks').glob('sonda_*.py'))+sorted((LAB/'notebooks/agent').glob('sonda_*.py'))
    paths += [LAB/'notebooks/social_ingest.py',LAB/'notebooks/agent/social_agent.py']
    drift=[]
    def asset(path,relative):
        current=path.read_bytes()
        content=current.decode('utf-8').replace('\r\n','\n').replace('\r','\n').encode('utf-8')
        if current!=content:
            if check:
                drift.append(relative)
            else:
                path.write_bytes(content)
        return {'file':relative,'sha256':hashlib.sha256(content).hexdigest()}
    metadata['runtime_files']=[{**asset(path,path.relative_to(LAB).as_posix()),
        'workspace_path':path.relative_to(LAB/'notebooks').as_posix()} for path in paths]
    metadata['shared_runtime_files']=[asset(APP/name,name) for name in shared]
    unsigned={key:value for key,value in metadata.items() if key!='pack_sha256'}
    metadata['pack_sha256']=hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    raw=(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n').encode()
    if check:
        if (LAB/'lab.json').read_bytes()!=raw:
            drift.append('lab.json')
    else:
        (LAB/'lab.json').write_bytes(raw)
    if drift:
        raise SystemExit('Regenerate Sonda pack: '+', '.join(drift))
    print('Sonda native pack '+('verified' if check else 'pinned')+': '+metadata['pack_version'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    render(parser.parse_args().check)
