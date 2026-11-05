"""Exact, nonsecret runtime observations; documentation versions are never observations."""
from importlib import metadata
import platform


RUNTIME_DISTRIBUTIONS=('langchain-core','langgraph','langgraph-checkpoint','langgraph-prebuilt',
                       'oci','pydantic','mcp','httpx','pypdf')


def runtime_versions(distributions=RUNTIME_DISTRIBUTIONS):
    packages={}
    for name in distributions:
        try:
            packages[name]=metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name]=None
    return {'python':platform.python_version(),'packages':packages}


def exact_runtime_comparison(local,native):
    expected={'python','packages'}
    if set(local)!=expected or set(native)!=expected:
        raise ValueError('Both runtime observations must contain Python and package versions')
    differences={}
    if not local['python'] or not native['python']:
        differences['python']='unknown'
    elif local['python']!=native['python']:
        differences['python']={'local':local['python'],'native':native['python']}
    for name in sorted(set(RUNTIME_DISTRIBUTIONS)|set(local['packages'])|set(native['packages'])):
        first,second=local['packages'].get(name),native['packages'].get(name)
        if first is None or second is None:
            differences[name]='unknown'
        elif first!=second:
            differences[name]={'local':first,'native':second}
    return {'exact_match':not differences,'differences':differences,
            'scope':'Python patch and exact shared distributions; Oracle-managed aidputils requires native integration tests'}
