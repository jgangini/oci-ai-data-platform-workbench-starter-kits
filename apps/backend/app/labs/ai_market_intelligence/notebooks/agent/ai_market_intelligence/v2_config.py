"""Operator-owned bindings for the portable v2 runtime, without SQL configuration."""
import re

from .runtime_core import Scope, public_url
from .runtime_versions import RUNTIME_DISTRIBUTIONS


V2_SERVICE_FIELDS={'credential_name','identity_sha256','region','model_endpoint','model_id','compartment_id',
    'guardrails_config','tenant_id','generation_id','namespace','bucket','enabled_sources','search_context',
    'candidate_revision','runtime_profile','spending_mode'}
V2_SOURCE_KEYS={'web','x','youtube','facebook','instagram','market_data','world_bank','financial_document'}


def validate_v2_config(config):
    if (not isinstance(config,dict) or set(config) not in
            ({'runtime_version','research_enabled'},{'runtime_version','research_enabled','services'})
            or config.get('runtime_version')!='v2' or type(config.get('research_enabled')) is not bool):
        raise ValueError('An exact v2 runtime binding is required')
    services=config.get('services')
    if services is None:
        if config['research_enabled']:
            raise ValueError('Research requires explicit authenticated service bindings')
        return config
    if not isinstance(services,dict) or set(services)!=V2_SERVICE_FIELDS:
        raise ValueError('V2 services require the complete reviewed binding, without legacy SQL fields')
    v2_service_references(services)
    v2_service_sources(services)
    v2_service_profile(services['runtime_profile'])
    return config


def v2_service_references(services):
    for key in ('credential_name','region','model_id','compartment_id','tenant_id','namespace','bucket'):
        if not isinstance(services[key],str) or not 1<=len(services[key])<=256:
            raise ValueError('A bounded operator service reference is required: '+key)
    for key in ('identity_sha256','candidate_revision'):
        if not isinstance(services[key],str) or not re.fullmatch('[a-f0-9]{64}',services[key]):
            raise ValueError('The service binding requires its exact identity and candidate hashes')
    Scope(services['tenant_id'],'binding-validation',services['generation_id'])
    public_url(services['model_endpoint'])
    if services['spending_mode']!='unlimited':
        raise ValueError('This development runtime requires the explicitly authorized unlimited grant')
    if not isinstance(services['guardrails_config'],dict):
        raise ValueError('The existing model guardrail configuration must be preserved')


def v2_service_sources(services):
    sources=services['enabled_sources']
    if not isinstance(sources,list) or not sources or len(set(sources))!=len(sources) or not set(sources)<=V2_SOURCE_KEYS:
        raise ValueError('Only explicit reviewed v2 sources may be enabled')
    context=services['search_context']
    if (not isinstance(context,dict) or set(context)!={'country_code','language_code'}
            or not re.fullmatch('[A-Z]{2}',str(context['country_code']))
            or not re.fullmatch('[a-z]{2}(?:-[A-Z]{2})?',str(context['language_code']))):
        raise ValueError('An explicit search country and language are required')


def v2_service_profile(profile):
    if (not isinstance(profile,dict) or set(profile)!={'python','packages'}
            or not isinstance(profile['packages'],dict)
            or not set(RUNTIME_DISTRIBUTIONS)<=set(profile['packages'])
            or not re.fullmatch(r'3\.11\.\d+',str(profile['python']))):
        raise ValueError('The exact observed Python and shared package profile is required')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',name) or not isinstance(version,str)
           or not 1<=len(version)<=128 for name,version in profile['packages'].items()):
        raise ValueError('Runtime package observations must have bounded names and versions')
