"""Readable case command assembled from the same controller and Delta repository."""

import inspect

from . import case_activity, case_changes, case_commit, case_program, contracts, journal, projection_query, service_transport
from .activity_source import activity_program_source


def case_program_source():
    symbols = (contracts.Contract, contracts.TreatmentDraft,
        case_changes.case_time, case_changes.case_digest, case_changes.case_versions, case_changes.case_typed, case_changes.case_record_snapshot,
        case_changes.case_change, case_changes._case_change, case_changes.case_asset_links,
        case_changes._initial_case_state, case_changes._proposed_case, case_changes._case_decision,
        case_changes.validate_case_change, case_changes.case_change_receipt, case_changes.case_confirmation, case_changes.case_effect_admission,
        case_commit.CasePending, case_commit._same, case_commit.CaseCommitter,
        case_activity.case_operation_run_id, case_activity.case_run_start, case_activity.case_run_event, case_activity.CaseActivityRun,
        journal.JournalConflict, journal.GovernanceJournal,
        service_transport.runtime_signer, service_transport.runtime_sdk_config, service_transport.runtime_object_storage,
        projection_query.consultation_scope_ids, case_program.checked_case_command,
        case_program.execute_case_program, case_program.case_pending_diagnostics,
        case_program.case_pending_check, case_program.run_case_program)
    # Reuse the approved repository/model assembly, not another schema or hidden loader.
    header = ('\nfrom copy import deepcopy\nimport asyncio\n'
        'from typing import Annotated,Literal\nfrom pydantic import BaseModel,ConfigDict,Field\n'
        'Identifier = Annotated[str,Field(min_length=1,max_length=256)]\n'
        'ShortText = Annotated[str,Field(min_length=1,max_length=2000)]\n'
        'References = Annotated[list[Identifier],Field(max_length=128)]\n'
        f'CASE_MODES = frozenset({sorted(case_program.CASE_MODES)!r})\n')
    source = activity_program_source() + header + '\n\n'.join(inspect.getsource(symbol) for symbol in symbols)
    source += '\nTreatmentDraft.model_rebuild(_types_namespace=globals())\n'
    compile(source, 'consultation_case.py', 'exec')
    return source
