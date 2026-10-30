"""Native IO for a single operator-bound Sonda generation and shared OCI identity."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID


def runtime_storage(binding):
    # Spark injects this API into notebook globals; AI compute provides a module.
    secrets_api=globals().get('aidputils')
    if secrets_api is None:
        import aidputils as secrets_api
    import oci
    keys=('tenancy','user','fingerprint','private_key','region')
    values={key:secrets_api.secrets.get(name=binding['credential_name'],key=key) for key in keys}
    if any(not isinstance(value,str) or not value for value in values.values()):
        raise RuntimeError('The existing shared OCI credential is incomplete')
    identity=json.dumps([values[key] for key in ('tenancy','user','fingerprint')],separators=(',',':'))
    if hashlib.sha256(identity.encode()).hexdigest()!=binding['identity_sha256']:
        raise RuntimeError('The shared OCI identity does not match this generation')
    signer=oci.signer.Signer(values['tenancy'],values['user'],values['fingerprint'],
        private_key_file_location=None,private_key_content=values['private_key'])
    cfg={key:values[key] for key in ('tenancy','user','fingerprint')}
    cfg['region']=binding['region']
    cfg['key_content']=values['private_key']
    return oci.object_storage.ObjectStorageClient(cfg,signer=signer,timeout=(10,60)),signer


def operational_prefix(binding):
    registry=str(UUID(binding['registry_id']))
    generation=str(UUID(binding['generation_id']))
    return f'ai_social_network/registries/{registry}/generations/{generation}/control/'


def verified_control(storage,binding,key,*,maximum=4194304):
    if not key.startswith(operational_prefix(binding)) or '..' in key.split('/'):
        raise ValueError('Operational object is outside the bound generation')
    response=storage.get_object(binding['namespace'],binding['artifacts_bucket'],key)
    try:
        raw=response.data.raw.read(maximum+1)
    finally:
        response.data.close()
    if len(raw)>maximum:
        raise ValueError('Operational object exceeds its byte limit')
    return raw


def generation_control(storage,binding):
    # Only the operator/installer may write this protected generation fence.
    key=operational_prefix(binding)+'binding.json'
    try:
        raw=verified_control(storage,binding,key,maximum=16384)
    except Exception as exc:
        if getattr(exc,'status',None)==404:
            return {}
        raise
    value=json.loads(raw)
    if not isinstance(value,dict):
        return {}
    valid=(value.get('registry_id')==binding['registry_id']
        and value.get('generation_id')==binding['generation_id']
        and value.get('contract_sha256')==binding['contract_sha256']
        and value.get('reset_verified') is True)
    return value if valid else {}


def generation_enabled(storage,binding):
    value=generation_control(storage,binding)
    return value.get('enabled') is True and value.get('integrated_acceptance') is True


def generation_bootstrap(storage,binding):
    """Prepare schemas/configuration only; never capture, investigate or enable chat."""
    value=generation_control(storage,binding)
    return value.get('bootstrap') is True and value.get('enabled') is False


def generation_inference(storage,binding):
    """Accepted production or a bounded, explicit operator validation window."""
    value=generation_control(storage,binding)
    if value.get('enabled') is True and value.get('integrated_acceptance') is True:
        return True
    validation=value.get('validation')
    if value.get('enabled') is not False or not isinstance(validation,dict):
        return False
    try:
        operation=str(UUID(validation['operation_id']))
        start=datetime.fromisoformat(validation['started_at'].replace('Z','+00:00'))
        end=datetime.fromisoformat(validation['expires_at'].replace('Z','+00:00'))
        now=datetime.now(timezone.utc)
        return (operation==validation['operation_id'] and start.utcoffset() is not None
            and end.utcoffset() is not None and 0<(end-start).total_seconds()<=3600
            and start<=now<end)
    except (KeyError,TypeError,ValueError):
        return False
