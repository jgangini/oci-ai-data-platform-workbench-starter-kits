"""One admitted append per frozen activity batch; uncertain writes are read back."""

from .inventory import canonical, identity
from .activity_records import validate_activity_batch


class ActivityPending(RuntimeError):
    pass


def activity_receipt(batch):
    return {"schema_version":1,"kind":"CONSULTATION_ACTIVITY","operation_id":batch["operation_id"],
        "scope_id":batch["scope_id"],"agent_id":batch["agent"]["agent_id"],"run_id":batch["run"]["run_id"],
        "input_digest":identity("activity-batch",batch),"run_count":1,"event_count":len(batch["events"]),"verified":True}


def activity_failure_receipt(batch):
    # The fixed runner may emit this only after its own program ends with no active jobs.
    return {"schema_version":1,"kind":"CONSULTATION_ACTIVITY","operation_id":batch["operation_id"],
        "input_digest":identity("activity-batch",batch),"terminal":True,"status":"FAILED"}


def activity_receipt_matches(actual, expected):
    return isinstance(actual,dict) and canonical(actual) == canonical(expected)


def activity_abort_receipt(batch):
    return {**activity_failure_receipt(batch),"status":"NOT_STARTED"}


class ActivityCommitter:
    def __init__(self, journal, execute, check_active, *, diagnose=None):
        self.journal,self.execute,self.check_active = journal,execute,check_active
        self.diagnose = diagnose

    def _note(self, stage, transition, error=None):
        if self.diagnose is not None:
            try:
                self.diagnose(stage, transition, error)
            except Exception:
                pass

    def _step(self, stage, function, *args):
        self._note(stage, 'started')
        try:
            result = function(*args)
        except Exception as exc:
            self._note(stage, 'failed', exc)
            raise
        self._note(stage, 'completed')
        return result

    def commit(self, batch):
        self._step('commit_identity', self.check_active)
        validate_activity_batch(batch)
        key = identity("consultation-activity-commit",batch["operation_id"])
        # All timestamps and rows are frozen before the sole write admission.
        self._step('outcome_create', self.journal.create, "outcomes",key,batch)
        if not activity_receipt_matches(self._step('outcome_read', self.journal.read, "outcomes",key),batch):
            failure = ValueError("The frozen activity changed")
            self._note('outcome_match', 'failed', failure)
            raise failure
        expected = activity_receipt(batch)
        current = self._step('materialized_read', self.journal.read, "materialized",key)
        if current is not None and not activity_receipt_matches(current,expected):
            failure = ValueError("Activity receipt changed")
            self._note('materialized_match', 'failed', failure)
            raise failure
        claim = {"schema_version":1,"kind":"CONSULTATION_ACTIVITY","operation_id":batch["operation_id"],
            "input_digest":expected["input_digest"]}
        admitted = self._step('claim_create', self.journal.create, "claims",key,claim) if current is None else False
        if not admitted:
            self._step('commit_identity', self.check_active)
            observed = self._step('verify', self.execute, "VERIFY_ACTIVITY",batch)
            if not activity_receipt_matches(observed,expected):
                raise ActivityPending("Activity write is incomplete or uncertain; do not append again")
        else:
            try:
                self._step('append_identity', self.check_active)
            except Exception:
                # This winner has not entered the executor. A timeout there cannot create this seal.
                self.journal.create("responses",key,activity_abort_receipt(batch))
                raise
            self._append(batch,key,expected)
        self._step('materialized_identity', self.check_active)
        self._step('materialized_create', self.journal.create, "materialized",key,expected)
        return expected

    def _append(self,batch,key,expected):
        try:
            observed = self._step('append', self.execute, "APPEND_ACTIVITY",batch)
        except Exception:
            raise ActivityPending("Activity write is uncertain; reconcile its frozen operation") from None
        if activity_receipt_matches(observed,activity_failure_receipt(batch)):
            self.journal.create("responses",key,observed)
            failure = ActivityPending("The fixed write program failed terminally; explicit repair is available")
            self._note('program_terminal', 'failed', failure)
            raise failure
        if not activity_receipt_matches(observed,expected):
            failure = ActivityPending("Activity readback was not confirmed")
            self._note('receipt_match', 'failed', failure)
            raise failure
        self._note('receipt_match', 'completed')

    def repair(self,batch):
        self.check_active()
        validate_activity_batch(batch)
        key=identity("consultation-activity-commit",batch["operation_id"])
        if not activity_receipt_matches(self.journal.read("outcomes",key),batch):
            raise ValueError("Repair requires the original frozen activity")
        sealed = self.journal.read("responses",key)
        if not any(activity_receipt_matches(sealed,receipt) for receipt in
                (activity_failure_receipt(batch),activity_abort_receipt(batch))):
            raise ActivityPending("No verified terminal failure; repair must not start another write")
        expected=activity_receipt(batch)
        if not activity_receipt_matches(self.execute("VERIFY_ACTIVITY",batch),expected):
            repair_key=identity("consultation-activity-repair-once",key)
            if not self.journal.create("claims",repair_key,{"operation_id":batch["operation_id"],"input_digest":expected["input_digest"]}):
                raise ActivityPending("The single repair is already admitted; reconcile it")
            self.check_active()
            self._append(batch,key,expected)
        self.check_active()
        self.journal.create("materialized",key,expected)
        return expected
