"""Evidence and assessed-pair checks for model-authored diagnoses and treatments."""

import re
from datetime import datetime, timezone

from .contracts import GovDiagnosis, TreatmentDraft


class CasePreviewUnavailable(ValueError):
    """A deterministic action-state explanation, never a provider error message."""


def validate_case_diagnosis(value, review, evidence):
    diagnosis = GovDiagnosis.model_validate(value)
    supported = {key for claim in review["supported_claims"] for key in claim["evidence_ids"]}
    proofs = {row["evidence_id"]: row for row in evidence}
    for claim in [*diagnosis.findings, *diagnosis.potential_impacts]:
        if not set(claim.evidence_ids).issubset(supported & proofs.keys()):
            raise ValueError("Case diagnosis cites an unsupported claim")
    for claim in diagnosis.potential_impacts:
        if not any(proofs[key]["kind"] == "RUNTIME_LINEAGE" and proofs[key]["facts"].get("links")
                or proofs[key]["kind"] == "STATIC_CODE" and proofs[key]["facts"].get("references") for key in claim.evidence_ids):
            raise ValueError("Potential impact requires an observed relationship")
    if diagnosis.treatment_needed and not diagnosis.findings:
        raise ValueError("A treatment requires a concrete supported finding")
    return diagnosis.model_dump(mode="json")


def validate_case_treatment(value, assets, *, evidence=None):
    draft = TreatmentDraft.model_validate(value)
    pairs = [(row["asset_id"], row["version_id"]) for row in assets if not row["asset_id"].startswith("case:")]
    selected = list(zip(draft.asset_ids, draft.expected_versions))
    if (not 1 <= len(pairs) <= 16 or len(draft.asset_ids) != len(draft.expected_versions)
            or len(selected) != len(pairs) or len(set(selected)) != len(selected) or set(selected) != set(pairs)):
        raise ValueError("The treatment must retain the exact assessed business asset set")
    if evidence is not None and draft.responsible_ref is not None:
        known = {row['facts'].get('owner_ref') for row in evidence if row['kind'] == 'CATALOG'}
        known.update(row['facts'].get('case',{}).get('owner_ref') for row in evidence if row['kind'] == 'CASE_RECORD')
        if draft.responsible_ref not in known:
            raise ValueError('A model treatment cannot invent a responsible identity')
    return draft.model_dump(mode="json")


def case_opening_summary(value):
    draft = TreatmentDraft.model_validate(value)
    summary = draft.problem
    if draft.remaining_gaps:
        summary += '\n\nAlcance y pendientes: ' + '; '.join(draft.remaining_gaps)
    if len(summary) > 2000:
        raise CasePreviewUnavailable('El motivo y sus límites exceden los 2000 caracteres del caso. '
            'Solicita un resumen más breve que conserve esos límites; no se ha registrado ningún cambio.')
    return summary


def render_case_preview(preview):
    if not preview:
        return ""
    if preview.get("available") is False:
        return "\n\n" + case_preview_text(preview["reason"])
    checked_case_preview(preview)
    label = {"OPEN":"abrir el caso", "PROPOSE":"registrar la propuesta de tratamiento", "APPROVE":"registrar la aprobación",
        "REJECT":"registrar el rechazo", "REVOKE":"registrar la revocación"}[preview["action"]]
    details = '\n\nMotivo del caso: ' + case_preview_text(preview['summary'])
    details += '\n\nActivos evaluados: ' + ', '.join(case_preview_text(row['name']) + ' (revisión ' + row['version_id'][:12] + ')'
        for row in preview['assessed_assets']) + '.'
    if preview.get('treatment'):
        draft = preview['treatment']
        if draft['problem'] != preview['summary']:
            details += '\n\nProblema del tratamiento: ' + case_preview_text(draft['problem'])
        details += '\n\nPropuesta: ' + case_preview_text(draft['suggested_action'])
        details += '\n\nVerificación prevista: ' + case_preview_text(draft['verification_plan'])
        details += '\n\nResponsable propuesto: ' + case_preview_text(draft['responsible_ref'] or 'pendiente de identificar') + '.'
        if draft['remaining_gaps']:
            details += '\n\nPendientes del tratamiento: ' + '; '.join(case_preview_text(text) for text in draft['remaining_gaps']) + '.'
    if preview['action'] == 'OPEN':
        details += '\n\nPrimero se abrirá el caso; la propuesta de tratamiento todavía no se registrará.'
    deadline = datetime.fromisoformat(preview['expires_at']).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    details += '\n\nVálida hasta: ' + deadline + '.'
    return (details + "\n\nPara " + label + ", envía exactamente:\n\n`" + preview["command"] + "`\n\n"
        "Esta confirmación no ejecuta cambios en los datos ni cierra el tratamiento.")


def checked_case_preview(preview):
    fields = {'available','action','command','assessed_assets','treatment','summary','expires_at','case_ref'}
    words = {'OPEN':'ABRIR','PROPOSE':'PROPONER','APPROVE':'APROBAR','REJECT':'RECHAZAR','REVOKE':'REVOCAR'}
    if (not isinstance(preview,dict) or set(preview) != fields or preview['available'] is not True
            or preview['action'] not in words or not isinstance(preview['summary'],str) or not 1 <= len(preview['summary'].strip()) <= 2000):
        raise ValueError('The confirmation has no exact visible operation')
    if (not isinstance(preview['command'],str) or not re.fullmatch('CONFIRMAR '+words[preview['action']]+' [0-9a-f]{32}',preview['command'])
            or datetime.fromisoformat(preview['expires_at']).utcoffset() is None):
        raise ValueError('The confirmation command or deadline differs')
    checked_preview_assets(preview['assessed_assets'])
    check_visible_case_transition(preview['action'],preview['case_ref'],preview['treatment'],preview['assessed_assets'])


def check_visible_case_transition(action, reference, treatment, assets):
    if action == 'OPEN':
        if reference is not None or treatment is not None:
            raise ValueError('Opening a case does not register a treatment')
        return
    if not isinstance(reference,str) or not re.fullmatch('[0-9a-f]{64}',reference):
        raise ValueError('The confirmation must identify its existing case')
    validate_case_treatment(treatment,assets)


def checked_preview_assets(assets):
    if not isinstance(assets,list) or not 1 <= len(assets) <= 16:
        raise ValueError('The visible assessed set is unavailable')
    for row in assets:
        if (not isinstance(row,dict) or set(row) != {'asset_id','version_id','name'}
                or not isinstance(row['asset_id'],str) or not 1 <= len(row['asset_id']) <= 256
                or not isinstance(row['version_id'],str) or not re.fullmatch('[0-9a-f]{64}',row['version_id'])
                or not isinstance(row['name'],str) or not 1 <= len(row['name']) <= 2000):
            raise ValueError('A visible assessed asset is invalid')
    if len({row['asset_id'] for row in assets}) != len(assets):
        raise ValueError('The visible assessed set has duplicated assets')


def case_preview_text(value):
    text = ' '.join(value.split()).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    return re.sub(r'([\\`*_{}\[\]()#!|])',r'\\\1',text)


def render_case_pending_report(report):
    labels={'FROZEN_START':'Inicio conservado','CURRENT_REFERENCES':'Referencias actuales del registro',
        'EXPECTED_PREVIOUS_STATE':'Estado anterior del caso'}
    reasons={'PENDING':'comprobación incompleta','PERMISSION_ERROR':'condición de acceso no admitida',
        'VALUE_ERROR':'validación fallida','TYPE_ERROR':'error técnico de tipo',
        'RUNTIME_ERROR':'fallo de ejecución','UNAVAILABLE':'comprobación no disponible'}
    lines=[]
    for row in report['checks']:
        detail='comprobado' if row['state']=='PASSED' else reasons[row['error_kind']]
        lines.append('- '+labels[row['phase']]+': '+detail+'.')
    return ('El caso sigue pendiente y conservo tu confirmación. No repetí el registro del caso.\n\n'
        +'\n'.join(lines)+'\n\nLos controles describen el estado actual; no certifican la causa del fallo original.')
