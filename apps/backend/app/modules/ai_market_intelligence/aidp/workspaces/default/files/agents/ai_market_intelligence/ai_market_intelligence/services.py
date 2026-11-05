"""Native clients only; reasoning and durable operational state stay portable."""
import hashlib
import json
from pathlib import Path

from . import APIFY_CREDENTIAL_NAME
from .model import DecisionModel
from .provider import ApifyProvider
from .runtime_versions import exact_runtime_comparison, runtime_versions


def build_market_services(config):
    expected=config['runtime_profile']
    observed=runtime_versions(tuple(expected['packages']))
    if not exact_runtime_comparison(observed,expected)['exact_match']:
        raise ValueError('The native runtime changed from the accepted exact dependency profile')
    import aidputils
    import oci
    from aidputils.agents.toolkit.agent_helper import GenAIChatInvoker, GenerativeAiInferenceV2Client
    from aidputils.agents.toolkit.tool_helper import build_structured_tools_from_allowed_mcp_tools
    from aidputils.agents.tools.mcp.mcp_service import get_mcp_client
    identity={key:aidputils.secrets.get(name=config['credential_name'],key=key)
              for key in ('tenancy','user','fingerprint','private_key')}
    checksum=hashlib.sha256(json.dumps([identity[key] for key in ('tenancy','user','fingerprint')],
                                      separators=(',',':')).encode()).hexdigest()
    if checksum!=config['identity_sha256']:
        raise ValueError('OCI identity differs from the operator-owned runtime binding')
    signer=oci.signer.Signer(identity['tenancy'],identity['user'],identity['fingerprint'],
        private_key_file_location=None,private_key_content=identity['private_key'])
    storage_config={key:identity[key] for key in ('tenancy','user','fingerprint')}
    storage_config.update(key_content=identity['private_key'],region=config['region'])
    storage=oci.object_storage.ObjectStorageClient(storage_config,signer=signer)
    contract=json.loads(Path(__file__).with_name('mcp_tools.json').read_text(encoding='utf-8'))
    token=aidputils.secrets.get(name=APIFY_CREDENTIAL_NAME,key='token')
    auth={'authType':'BEARER_TOKEN','token':token}
    name='AiMarketIntelligenceApify'
    tools=build_structured_tools_from_allowed_mcp_tools(
        allowed_tools=[{'tool':tool,'instruction':'','argOverrides':{}} for tool in contract['tools']],
        server_name=name,endpoint=contract['endpoint'],transport='streamable_http',auth=auth,headers={})
    mcp=get_mcp_client(server_name=name,server_url=contract['endpoint'],auth=auth,transport='streamable_http')
    settings={'model_id':config['model_id'],'model_kwargs':{'temperature':0,'max_tokens':6000,'reasoning_effort':'LOW'},
              'guardrails_config':config['guardrails_config']}
    model=DecisionModel(GenAIChatInvoker(provider='generic',auth_type='API_KEY',
        client=GenerativeAiInferenceV2Client(endpoint=config['model_endpoint'],signer=signer),
        is_stream=False,compartment_id=config['compartment_id'],service_endpoint=config['model_endpoint'],**settings),
        prompts=json.loads(Path(__file__).with_name('prompts.json').read_text(encoding='utf-8')))
    return {'storage':storage,'model':model,'provider':ApifyProvider({tool.name:tool for tool in tools},website_text_bytes=600000),'mcp':mcp}
