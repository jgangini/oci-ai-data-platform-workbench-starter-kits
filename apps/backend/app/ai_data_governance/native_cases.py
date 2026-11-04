"""Compose native case consent and fixed execution outside model capabilities."""

from datetime import datetime, timezone

from .case_changes import case_change_receipt, case_record_snapshot
from .case_confirmation import CaseConfirmations, CaseConfirmationExpired
from .case_policy import native_case_policy
from .case_readmission import CaseReadmission, case_decision_head
from .case_reader import case_history_query, case_history_groups, case_history_snapshot
from .case_transport import native_case_operation, CaseReconciliationPending, checked_case_pending_report
from .inventory import canonical, identity
from .projection_query import projection_search
from .case_previews import validate_case_treatment, case_opening_summary, CasePreviewUnavailable, render_case_pending_report
from .contracts import TreatmentDraft


class NativeCaseActions:
    def __init__(self, binding, caller, activity, program, query, hooks_factory, *, get_context, now=None):
        if caller.access_mode != "CENTRAL_METADATA" or activity is None or not program:
            raise PermissionError("Native cases require the installed central runtime and durable activity")
        self.binding, self.caller, self.activity, self.program = binding, caller, activity, program
        self.query, self.hooks_factory = query, hooks_factory
        self.get_context = get_context
        clock = now or (lambda: datetime.now(timezone.utc))
        self.readmission = CaseReadmission(binding, caller, query, hooks_factory, now=clock)
        async def authorize_action(actor_ref, scope_id, action):
            activity.check_active()
            return await native_case_policy(binding, actor_ref, scope_id, action, query=query,
                journal=activity.journal, journal_call=activity._worker, now=clock)
        self.confirmations = CaseConfirmations(binding, activity.journal, get_context=get_context,
            journal_call=activity._worker, authorize_action=authorize_action, revalidate=self.readmission.revalidate, now=clock)

    def check_active(self):
        self.activity.check_active()
        self.confirmations._check_caller()

    async def offer(self, specification):
        self.check_active()
        offered = await self.confirmations.offer(specification)
        self.check_active()
        return offered

    async def preview(self, state, config):
        self.check_active()
        intent = state['intent']
        if intent['intent'] == 'treatment' and not state.get('treatment'):
            return {'available':False,'reason':'No hay un tratamiento sustentado para registrar todavía.'}
        if intent['intent'] == 'decision' and intent.get('decision_action') not in {'approve','reject','revoke'}:
            return {'available':False,'reason':'Indica si quieres aprobar, rechazar o revocar el tratamiento del caso.'}
        try:
            if intent['intent'] == 'treatment':
                validate_case_treatment(state['treatment'], state['assets'], evidence=state.get('evidence'))
            spec, names = await self._preview_specification(state)
            offered = await self.offer(spec)
        except CasePreviewUnavailable as error:
            self.check_active()
            return {'available':False,'reason':str(error)}
        except (ValueError, PermissionError, RuntimeError):
            self.check_active()
            return {'available':False,'reason':'No pude preparar una confirmación vigente para ese registro. '
                'El caso, las versiones, la evidencia o la política requieren comprobación; no se ha registrado ningún cambio.'}
        frozen = offered['challenge']['specification']
        draft = frozen['draft']
        if draft is None and frozen['previous'] and frozen['previous']['treatment_json']:
            draft = TreatmentDraft.model_validate_json(frozen['previous']['treatment_json']).model_dump(mode='json')
        assets = [dict(asset_id=r['asset_id'],version_id=r['assessed_version_id'],name=names[r['asset_id']]) for r in frozen['assets']]
        return {'available':True,'action':frozen['action'],'command':offered['command'],'assessed_assets':assets,'treatment':draft,
            'summary':frozen['previous']['summary'] if frozen['previous'] else frozen['summary'],
            'expires_at':offered['challenge']['respond_by'],'case_ref':frozen['previous']['case_id'] if frozen['previous'] else None}

    async def _preview_specification(self, state):
        intent = state['intent']
        reference = intent.get('case_reference')
        if reference:
            rows = await self.query(case_history_query(self.binding, reference), limit=48)
            snapshot = case_history_snapshot(self.binding, reference, rows)
            assessed = [row for row in state['assets'] if row['asset_id'].startswith('case:')]
            if snapshot is None or assessed != [snapshot['resource']]:
                raise PermissionError('The assessed case history changed')
            grouped = case_history_groups(self.binding, reference, rows)
            previous = case_record_snapshot('AI_DG_CASE', grouped['CASE'][0])
            if not 1 <= len(grouped['ASSET']) <= 16 or any(row['admitted'] is not True for row in grouped['ASSET']):
                raise PermissionError('The assessed case links are incomplete')
            links = [{key:row[key] for key in ('asset_id','assessed_version_id','impact_role','evidence_id')} for row in grouped['ASSET']]
            head = case_decision_head(previous, grouped['DECISION'])
            action = 'PROPOSE' if intent['intent'] == 'treatment' else intent['decision_action'].upper()
            check_case_preview_action(action, previous, head)
            spec = dict(action=action,scope_id=previous['scope_id'],assets=links,previous=previous,previous_decision=head,
                summary=None,draft=state['treatment'] if action=='PROPOSE' else None)
            return spec, {row['asset_id']:row['qualified_name'] for row in grouped['ASSET']}
        if intent['intent'] != 'treatment':
            raise ValueError('A decision requires an assessed case reference')
        scope, links = await self._opening_links(state['assets'])
        spec = dict(action='OPEN',scope_id=scope,assets=links,summary=case_opening_summary(state['treatment']),previous=None,
            previous_decision=None,draft=None)
        return spec, {row['asset_id']:row['name'] for row in state['assets'] if not row['asset_id'].startswith('case:')}

    async def _opening_links(self, assets):
        selected = [row for row in assets if not row['asset_id'].startswith('case:')]
        if not 1 <= len(selected) <= 16:
            raise ValueError('Opening a case requires a bounded assessed business set')
        rows = await self.query(projection_search(self.binding, [], asset_ids=[r['asset_id'] for r in selected]), limit=32)
        self.hooks_factory()._checked_rows(rows)
        current = [{'asset_id':r['asset_id'],'version_id':r['version_id'],'name':r['qualified_name']} for r in rows]
        if sorted(current,key=lambda r:r['asset_id']) != sorted(selected,key=lambda r:r['asset_id']) or len({r['scope_id'] for r in rows}) != 1:
            raise PermissionError('The case opening assessment changed or spans multiple scopes')
        scope = rows[0]['scope_id']
        links = [dict(asset_id=r['asset_id'],assessed_version_id=r['version_id'],impact_role='SUBJECT',
            evidence_id=identity('AI_DG_EVIDENCE',scope,r['version_id'],r['metadata_hash'])) for r in rows]
        return scope, links

    async def confirm(self, question, *, request_id):
        self.check_active()
        try:
            command = await self.confirmations.confirm(question, request_id=request_id)
        except CaseConfirmationExpired:
            self.check_active()
            return {'result_state':'not_evaluable','action_receipts':[],'case_pending':False,'confirmation_expired':True,
                'answer_markdown':'La vista previa venció antes de admitir esta confirmación. Solicita una nueva vista previa '
                    'para volver a comprobar el alcance y la evidencia. No se ha registrado ningún cambio.'}
        if command is None:
            return None
        self.check_active()
        change = command["change"]
        try:
            receipt = await native_case_operation(self.binding, self.caller, self.program, command["mode"],
                change, command["grant"], get_context=self.get_context)
            self.check_active()
            if canonical(receipt) != canonical(case_change_receipt(change)):
                raise ValueError("Native case execution returned another receipt")
        except CaseReconciliationPending as error:
            self.check_active()
            diagnostic=checked_case_pending_report(self.binding,change,error.diagnostic)
            return {'case_ref':change['case']['case_id'],'result_state':'partial','action_receipts':[],
                'case_pending':True,'case_diagnostic':diagnostic,
                'answer_markdown':render_case_pending_report(diagnostic)}
        except Exception as error:
            self.check_active()
            # The frozen consent survives; only its readback may be requested again.
            status = getattr(error, 'status_code', None)
            timeout = 'Se agotó el tiempo de comprobación del comando. ' if type(status) is int and status == 504 else ''
            return {"case_ref": change["case"]["case_id"], "result_state": "partial", "action_receipts": [],
                "case_pending": True, "answer_markdown": timeout + "El registro del caso quedó pendiente de comprobación. "
                    "Repite el mismo mensaje de confirmación para verificarlo; no se enviará otra escritura."}
        action = {"OPEN": "La apertura del caso", "PROPOSE": "La propuesta de tratamiento", "APPROVE": "La aprobación",
            "REJECT": "El rechazo", "REVOKE": "La revocación"}[change["action"]]
        return {"case_ref": receipt["case_id"], "result_state": "answered", "action_receipts": [receipt],
            "case_pending": False, "answer_markdown": action + " quedó registrada y comprobada. "
                "Esto no acredita la ejecución ni el cierre del tratamiento.\n\nReferencia del caso: `" + receipt['case_id'] + "`."}


def check_case_preview_action(action, previous, head):
    if action == 'PROPOSE' and head is not None and head['decision'] == 'APPROVE':
        raise CasePreviewUnavailable('El tratamiento tiene una aprobación registrada. Revócala antes de reemplazar la propuesta.')
    if action in {'APPROVE','REJECT'} and (previous['status'] != 'WAIT_DECISION' or not previous['change_digest']):
        raise CasePreviewUnavailable('El caso no tiene una propuesta pendiente de decisión.')
    if action in {'APPROVE','REJECT'} and head is not None:
        raise CasePreviewUnavailable('Este tratamiento ya tiene una decisión registrada; no se volverá a decidir.')
    if action == 'REVOKE' and (head is None or head['decision'] != 'APPROVE'):
        raise CasePreviewUnavailable('No hay una aprobación registrada en el estado evaluado que se pueda revocar.')
