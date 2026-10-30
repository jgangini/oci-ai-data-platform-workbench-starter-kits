"""Generation namespace for the existing Oracle AI Lakehouse checkpointer.

Oracle may force thread_id to the authenticated request's session. Isolate with
checkpoint_ns as well, without changing its session/agent authorization context.
LangGraph sees its original namespace; only public checkpoint IO is adapted.
"""
import os
from uuid import UUID

from langchain_core.messages import convert_to_messages
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple


def root_graph_config(config):
    """Oracle's storage namespace 'default' is not a LangGraph subgraph route."""
    result=dict(config)
    values=dict(config.get('configurable') or {})
    if values.get('checkpoint_ns') not in (None,'','default'):
        raise ValueError('This root agent does not accept external subgraph routes')
    values['checkpoint_ns']=''
    result['configurable']=values
    if isinstance(config.get('metadata'),dict):
        result['metadata']={**config['metadata'],'checkpoint_ns':''}
    return result


class GenerationMemory(BaseCheckpointSaver):
    def __init__(self,delegate,generation_id):
        super().__init__(serde=delegate.serde)
        self.delegate=delegate
        self.prefix='sonda/'+str(UUID(str(generation_id)))+'/'

    def namespaced(self,config,*,reverse=False):
        result=dict(config)
        values=dict(config.get('configurable') or {})
        namespace=str(values.get('checkpoint_ns') or '')
        if reverse:
            if not namespace.startswith(self.prefix):
                raise RuntimeError('Memory checkpoint belongs to another generation')
            namespace=namespace[len(self.prefix):]
        else:
            namespace=self.prefix+namespace
        values['checkpoint_ns']=namespace
        result['configurable']=values
        if isinstance(config.get('metadata'),dict):
            result['metadata']={**config['metadata'],'checkpoint_ns':namespace}
        return result

    def restored_tuple(self,value):
        if value is None:
            return None
        if not isinstance(value,CheckpointTuple):
            raise RuntimeError('Invalid native memory checkpoint response')
        channels=dict(value.checkpoint['channel_values'])
        if 'messages' in channels:
            channels['messages']=convert_to_messages(channels['messages'])
        writes=[(task,channel,convert_to_messages(data) if channel=='messages' else data)
            for task,channel,data in (value.pending_writes or [])]
        return value._replace(config=self.namespaced(value.config,reverse=True),
            parent_config=self.namespaced(value.parent_config,reverse=True) if value.parent_config else None,
            checkpoint={**value.checkpoint,'channel_values':channels},pending_writes=writes)

    def get_next_version(self,current,channel):
        return self.delegate.get_next_version(current,channel)

    async def aget_tuple(self,config):
        return self.restored_tuple(await self.delegate.aget_tuple(self.namespaced(config)))

    async def alist(self,config,*,filter=None,before=None,limit=None):
        if config is None:
            raise ValueError('Memory listing requires an authenticated conversation context')
        rows=self.delegate.alist(self.namespaced(config),filter=filter,
            before=self.namespaced(before) if before else None,limit=limit)
        if hasattr(rows,'__aiter__'):
            async for row in rows:
                yield self.restored_tuple(row)
        else:
            rows=await rows
            for row in rows:
                yield self.restored_tuple(row)

    async def aput(self,config,checkpoint,metadata,new_versions):
        result=await self.delegate.aput(self.namespaced(config),checkpoint,metadata,new_versions)
        return self.namespaced(result,reverse=True)

    async def aput_writes(self,config,writes,task_id,task_path=''):
        await self.delegate.aput_writes(self.namespaced(config),writes,task_id,task_path=task_path)


def native_memory(generation_id,agent_name):
    from oracle_memory_clients.client import AsyncProxyCheckpointClient
    # Do not mask unavailable memory as an empty conversation.
    os.environ['AIDP_CHECKPOINTER_GET_GRACEFUL']='false'
    client=AsyncProxyCheckpointClient(base_url=os.getenv('MEMORY_SERVER_URL') or
        os.getenv('MEMORY_URL') or 'http://127.0.0.1:21100',agent=agent_name)
    return GenerationMemory(client,generation_id)
