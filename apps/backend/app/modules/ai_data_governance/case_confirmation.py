"""Two-turn native confirmation; model output cannot issue a write grant."""

from copy import deepcopy
from datetime import datetime, timedelta
import re
from uuid import uuid4

from .caller_transport import _caller_identity
from .case_activity import case_operation_run_id
from .case_changes import _case_change, case_confirmation, case_time, validate_case_change
from .inventory import canonical, identity
from .journal import JournalConflict
from .projection_query import consultation_scope_ids


CASE_CONFIRM_ACTIONS = {"ABRIR": "OPEN", "PROPONER": "PROPOSE", "APROBAR": "APPROVE",
    "RECHAZAR": "REJECT", "REVOCAR": "REVOKE"}
CASE_REQUEST_FIELDS = frozenset({"action", "scope_id", "assets", "summary", "previous", "draft", "previous_decision"})


class CaseConfirmationExpired(PermissionError):
    """This caller's existing preview expired before any new consent was admitted."""


def case_confirmation_command(question):
    if not isinstance(question, str) or len(question) > 4000:
        raise ValueError("A bounded user message is required")
    command = re.fullmatch(r"CONFIRMAR (ABRIR|PROPONER|APROBAR|RECHAZAR|REVOCAR) ([0-9a-f]{32})", question.strip())
    if command is not None:
        return CASE_CONFIRM_ACTIONS[command[1]], command[2]
    if question.strip().startswith("CONFIRMAR "):
        raise ValueError("The complete case confirmation command is required")
    return None


def confirmed_case_change(binding, specification, actor_ref, session_ref, request_id, at, policy):
    if (not isinstance(specification, dict) or set(specification) != CASE_REQUEST_FIELDS
            or len(canonical(specification).encode()) > 131072):
        raise ValueError("Invalid bounded case request")
    if specification["scope_id"] not in consultation_scope_ids(binding):
        raise PermissionError("Case confirmation escaped the installed coverage")
    operation = identity("governance-case-request", actor_ref, session_ref, request_id)
    return _case_change(actor_ref=actor_ref, session_ref=session_ref, request_id=request_id,
        run_id=case_operation_run_id(binding, {"operation_id": operation}), policy_version=policy["policy_version"], at=at,
        valid_until=datetime.fromisoformat(policy["expires_at"])
            if specification["action"] in {"APPROVE", "REJECT", "REVOKE"} else None, **specification)


class CaseConfirmations:
    """Trusted callbacks establish authority and fresh evidence independently of consent.

    A positive callback must come from the installed runtime policy, never G4/G5,
    the treatment's responsible_ref or an asserted user role. This adapter neither
    executes a case change nor grants access to business data or metadata writes.
    """
    def __init__(self, binding, journal, *, get_context, journal_call, authorize_action, revalidate, now):
        if not all(callable(value) for value in (get_context, journal_call, authorize_action, revalidate, now)):
            raise ValueError("Native identity, runtime policy and evidence readmission are required")
        self.binding, self.journal = deepcopy(binding), journal
        self.get_context, self.authorize_action, self.revalidate, self.now = get_context, authorize_action, revalidate, now
        self.journal_call = journal_call
        self.original = _caller_identity(get_context)
        self.actor_ref = identity("activity-subject", self.original[1])
        self.session_ref = identity("activity-session", self.original[2])

    def _check_caller(self):
        if _caller_identity(self.get_context) != self.original:
            raise PermissionError("Case confirmation caller or session changed")

    def _binding(self):
        return {key: self.binding[key] for key in ("platform_id", "workspace_key", "agent_key", "component_revision", "deployment_stage")}

    def _same_target(self, binding):
        if not isinstance(binding, dict):
            return False
        revision = binding.get('component_revision')
        return (isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{64}', revision) is not None
            and binding == {**self._binding(), 'component_revision': revision})

    async def _policy(self, spec):
        self._check_caller()
        policy = await self.authorize_action(self.actor_ref, spec["scope_id"], spec["action"])
        self._check_caller()
        if (not isinstance(policy, dict) or set(policy) != {"policy_version", "expires_at"}
                or not isinstance(policy["policy_version"], str) or not 1 <= len(policy["policy_version"].strip()) <= 256):
            raise PermissionError("No runtime authority policy permits this action")
        expiry = datetime.fromisoformat(policy["expires_at"])
        if expiry.utcoffset() is None or expiry <= self.now():
            raise PermissionError("Case action policy expired")
        return {**policy, "expires_at": case_time(expiry)}

    async def _check_evidence(self, spec):
        self._check_caller()
        if await self.revalidate(deepcopy(spec)) is not True:
            raise PermissionError("Case versions, evidence or prior state changed")
        self._check_caller()

    async def offer(self, specification):
        spec = deepcopy(specification)
        policy = await self._policy(spec)
        at = self.now()
        # Validate the semantics without freezing the eventual decision time/RUN.
        confirmed_case_change(self.binding, spec, self.actor_ref, self.session_ref, str(uuid4()), at, policy)
        await self._check_evidence(spec)
        token = uuid4().hex
        challenge = {"schema_version": 1, "kind": "GOVERNANCE_CASE_CONFIRMATION", "token": token,
            "binding": self._binding(), "actor_ref": self.actor_ref, "session_ref": self.session_ref,
            "specification": spec, "policy": policy, "offered_at": case_time(at),
            "respond_by": case_time(min(at + timedelta(minutes=5), datetime.fromisoformat(policy["expires_at"])))}
        await self.journal_call(self.journal.create, "batches", identity("case-confirmation", token), challenge)
        self._check_caller()
        word = next(word for word, action in CASE_CONFIRM_ACTIONS.items() if action == spec["action"])
        return {"challenge": challenge, "command": "CONFIRMAR " + word + " " + token}

    async def _challenge(self, token, action):
        challenge = await self.journal_call(self.journal.read, "batches", identity("case-confirmation", token))
        self._check_caller()
        fields = {"schema_version", "kind", "token", "binding", "actor_ref", "session_ref", "specification", "policy", "offered_at", "respond_by"}
        if (not isinstance(challenge, dict) or set(challenge) != fields or type(challenge["schema_version"]) is not int
                or challenge["schema_version"] != 1 or challenge["kind"] != "GOVERNANCE_CASE_CONFIRMATION"
                or challenge["token"] != token or not self._same_target(challenge["binding"])
                or challenge["actor_ref"] != self.actor_ref or challenge["session_ref"] != self.session_ref
                or challenge["specification"]["action"] != action):
            raise PermissionError("The confirmation does not identify this caller's exact pending action")
        offered = datetime.fromisoformat(challenge["offered_at"])
        expiry = datetime.fromisoformat(challenge["policy"]["expires_at"])
        if challenge["respond_by"] != case_time(min(offered + timedelta(minutes=5), expiry)):
            raise ValueError("The frozen confirmation deadline changed")
        return challenge

    async def _new_confirmation(self, challenge, request_id):
        spec, at = challenge["specification"], self.now()
        if not datetime.fromisoformat(challenge["offered_at"]) <= at < datetime.fromisoformat(challenge["respond_by"]):
            raise CaseConfirmationExpired("The case confirmation window expired")
        policy = await self._policy(spec)
        if policy["policy_version"] != challenge["policy"]["policy_version"]:
            raise PermissionError("The action policy changed after the preview")
        policy["expires_at"] = case_time(min(datetime.fromisoformat(policy["expires_at"]),
            datetime.fromisoformat(challenge["policy"]["expires_at"])))
        await self._check_evidence(spec)
        change = confirmed_case_change(self.binding, spec, self.actor_ref, self.session_ref, request_id, at, policy)
        grant = {"confirmed": True, "actor_ref": self.actor_ref, "session_ref": self.session_ref,
            "input_digest": identity("case-change", change), **policy}
        case_confirmation(change, grant, at)
        return {"schema_version": 1, "kind": "GOVERNANCE_CASE_CONSENT",
            "challenge_digest": identity("case-confirmation-body", challenge), "change": change, "grant": grant}

    def _checked_frozen(self, challenge, frozen):
        fields = {"schema_version", "kind", "challenge_digest", "change", "grant"}
        if (not isinstance(frozen, dict) or set(frozen) != fields or type(frozen["schema_version"]) is not int
                or frozen["schema_version"] != 1 or frozen["kind"] != "GOVERNANCE_CASE_CONSENT"
                or frozen["challenge_digest"] != identity("case-confirmation-body", challenge)):
            raise ValueError("The frozen case consent changed")
        change, grant = frozen["change"], frozen["grant"]
        validate_case_change(change)
        at = datetime.fromisoformat(change["observed_at"])
        rebuilt = confirmed_case_change(self.binding, challenge["specification"], self.actor_ref, self.session_ref,
            change["request_id"], at, {key: grant[key] for key in ("policy_version", "expires_at")})
        if (canonical(rebuilt) != canonical(change) or grant["policy_version"] != challenge["policy"]["policy_version"]
                or datetime.fromisoformat(grant["expires_at"]) > datetime.fromisoformat(challenge["policy"]["expires_at"])
                or not datetime.fromisoformat(challenge["offered_at"]) <= at < datetime.fromisoformat(challenge["respond_by"])):
            raise ValueError("The frozen consent differs from its exact preview")
        case_confirmation(change, grant, at)
        return frozen

    async def confirm(self, question, *, request_id):
        self._check_caller()
        command = case_confirmation_command(question)
        if command is None:
            return None
        action, token = command
        challenge = await self._challenge(token, action)
        key = identity("case-consent", token)
        frozen = await self.journal_call(self.journal.read, "responses", key)
        self._check_caller()
        if frozen is not None:
            self._checked_frozen(challenge, frozen)
            self._check_caller()
            return {"mode": "RECONCILE_CASE", "change": frozen["change"], "grant": None}
        if challenge['binding'] != self._binding():
            raise PermissionError('A new consent requires a preview from the current revision')
        candidate = await self._new_confirmation(challenge, request_id)
        try:
            admitted = await self.journal_call(self.journal.create, "responses", key, candidate)
        except (RuntimeError, JournalConflict):
            # An unacknowledged create can only be recovered as readback, never a new dispatch.
            frozen = self._checked_frozen(challenge, await self.journal_call(self.journal.read, "responses", key))
            self._check_caller()
            return {"mode": "RECONCILE_CASE", "change": frozen["change"], "grant": None}
        self._check_caller()
        frozen = self._checked_frozen(challenge, await self.journal_call(self.journal.read, "responses", key))
        self._check_caller()
        return {"mode": "COMMIT_CASE" if admitted else "RECONCILE_CASE",
            "change": frozen["change"], "grant": frozen["grant"] if admitted else None}
