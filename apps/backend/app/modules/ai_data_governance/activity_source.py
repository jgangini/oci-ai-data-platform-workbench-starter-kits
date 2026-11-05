"""Build the fixed monitoring command from the same typed repository code."""

import inspect
import pprint

from . import activity_records, activity_commit, activity_program, delta, inventory, model_output, repository
from .schema import NAMESPACE, load_model


def activity_program_source():
    symbols = (inventory.canonical,inventory.identity,model_output._unique_members,model_output._invalid_constant,model_output.model_json,
        delta.qualified_table,delta.table_location,delta._verify_table,
        repository.checked_rows,repository._value,repository._spark_schema,repository.DeltaRepository,
        repository._comparison_frame,repository._comparable,repository._restore_timestamps,
        activity_records.activity_agent,activity_records.activity_time,activity_records.activity_event,
        activity_records.activity_event_type,activity_records.activity_batch,activity_records.validate_activity_batch,
        activity_records.checked_activity_manifest,activity_records.checked_activity_content,
        activity_records.activity_typed_rows,activity_commit.activity_receipt,activity_commit.activity_failure_receipt,
        activity_program.activity_scope,activity_program.check_activity_scope,activity_program.register_activity_agent,
        activity_program.prepare_activity_rows,activity_program.activity_rows_match,
        activity_program.activity_jobs_terminal,activity_program.run_activity_program)
    header = ('"""Fixed AI_DG consultation activity command. No model SQL or business writes."""\n'
        'from __future__ import annotations\nimport json\nimport re\nimport hashlib\n'
        'from datetime import datetime,timedelta,timezone\nfrom uuid import UUID,uuid4\nfrom typing import Any\n'
        f'NAMESPACE = {NAMESPACE!r}\nMODEL = {pprint.pformat(load_model(),sort_dicts=False)}\n'
        'def load_model():\n    return MODEL\n\n')
    source = header + "\n\n".join(inspect.getsource(symbol) for symbol in symbols)
    compile(source,"consultation_activity.py","exec")
    return source
