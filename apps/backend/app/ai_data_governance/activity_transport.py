"""Central service execution restricted to the fixed monitoring program."""

from .activity_records import activity_agent, validate_activity_batch
from .central_sql import _run_central_command, _observe_central_command
from .inventory import canonical
from .model_output import model_json


def activity_command(binding, program, mode, payload=None, expected_version=None):
    if mode not in {"REGISTER_AGENT","APPEND_ACTIVITY","VERIFY_ACTIVITY"}:
        raise ValueError("Unknown activity command")
    activity_agent(binding)
    if mode == "REGISTER_AGENT":
        if payload is not None:
            raise ValueError("Registration cannot append consultation rows")
    else:
        validate_activity_batch(payload)
        if canonical(payload["agent"]) != canonical(activity_agent(binding)) or expected_version is not None:
            raise ValueError("Activity producer differs from the installed revision")
    # JSON remains literal data even when a field contains Python punctuation.
    data = canonical({"binding":binding,"mode":mode,"payload":payload,"expected_version":expected_version})
    return program + f'\nactivity_input = json.loads({data!r})\nprint(json.dumps(run_activity_program(spark, **activity_input), sort_keys=True))\n'


def run_native_activity(binding, signer, program, mode, payload, check_active, *, expected_version=None, diagnose=None):
    command = activity_command(binding,program,mode,payload,expected_version)
    options = {'diagnose':diagnose} if diagnose is not None else {}
    result = _run_central_command(binding,signer,command,check_active,language="PYTHON",**options)
    _observe_central_command(diagnose, 'receipt_decode', 'started')
    try:
        if (not isinstance(result,dict) or result.get("status") != "SUCCEEDED" or result.get("error")
                or result.get("results_truncated",False) is not False):
            raise RuntimeError("The fixed activity command did not finish with a complete receipt")
        outputs = result.get("outputs")
        if (not isinstance(outputs,list) or len(outputs) != 1 or not isinstance(outputs[0],dict)
                or outputs[0].get("type") != "TEXT_PLAIN" or outputs[0].get("is_base64",False) is not False):
            raise ValueError("Expected one fixed activity receipt")
        receipt = model_json(outputs[0].get("value"),max_bytes=4096)
    except Exception as exc:
        _observe_central_command(diagnose, 'receipt_decode', 'failed', exc)
        raise
    _observe_central_command(diagnose, 'receipt_decode', 'completed')
    return receipt
