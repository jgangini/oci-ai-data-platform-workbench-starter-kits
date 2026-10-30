"""Protected Activity opt-in and native-session correlation; legacy is the default."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from uuid import UUID

from app.sonda_activity import activity_invocation
from app.sonda_activity_runtime import ACTIVITY_MANIFEST_SHA256
from app.sonda_exchange import OciObjects
from app.labs.ai_social_network.notebooks.sonda_io import generation_control, generation_inference


def activity_enabled(storage, binding):
    policy=generation_control(storage,binding).get('activity')
    if policy is None or (isinstance(policy,dict) and set(policy)=={'enabled'} and policy['enabled'] is False):
        return False
    if (not isinstance(policy,dict) or set(policy)!={'enabled','manifest_sha256'}
            or type(policy['enabled']) is not bool or policy['manifest_sha256']!=ACTIVITY_MANIFEST_SHA256):
        raise ValueError('Activity policy differs from the installed capability')
    return policy['enabled']


def activity_reference(variables):
    if not isinstance(variables,dict):
        raise ValueError('Activity requires its native session reference')
    values=[variables[key] for key in ('sondaActivityRef','sessionvariables.sondaActivityRef') if key in variables]
    if not values:
        raise ValueError('Activity requires one unambiguous native session reference')
    refs=[]
    for value in values:
        ref=value.get('value') if isinstance(value,dict) else value
        if not isinstance(ref,str) or str(UUID(ref))!=ref:
            raise ValueError('Activity requires a canonical opaque admission reference')
        refs.append(ref)
    if len(set(refs))!=1:
        raise ValueError('Activity requires one unambiguous native session reference')
    return refs[0]


@asynccontextmanager
async def native_activity(storage,binding,native_config,input_sha256,*,protected_work=None):
    """An enabled capability requires admission; it never falls back to legacy."""
    if not await asyncio.to_thread(activity_enabled,storage,binding):
        yield None
        return
    if not await asyncio.to_thread(generation_inference,storage,binding):
        raise RuntimeError('Activity cannot admit work in a paused generation')
    from aidputils.agents.toolkit import chat_context
    ref=activity_reference(chat_context.session_context_var.get())
    native=(native_config.get('configurable') or {}).get('thread_id')
    agent=binding.get('activity_agent_id')
    if (not isinstance(native,str) or not 1<=len(native)<=512
            or not isinstance(agent,str) or str(UUID(agent))!=agent or UUID(agent).int==0):
        raise ValueError('Activity requires the installed agent and actual native session')
    objects=OciObjects(storage,binding['namespace'],{'artifacts':binding['artifacts_bucket']})
    scope={key:binding[key] for key in ('registry_id','generation_id')}
    async with activity_invocation(objects,scope,agent,hashlib.sha256(native.encode('utf-8')).hexdigest(),
            input_sha256,ref,datetime.now(timezone.utc),protected_work=protected_work) as activity:
        yield activity
