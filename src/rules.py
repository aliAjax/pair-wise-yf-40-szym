from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

SAMPLE_CONCLUSIONS = ("negative", "positive")
HOLD_STATUS = "pending_disposal"
HOLD_EXEMPT_STATUSES = ("quarantined", "destroyed")


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    samples = lookup("sample", "consignment_id", entity["id"]) if lookup else []
    blockers = consignment_release_blockers(samples)
    if blockers:
        raise ValidationError("release blocked: " + "; ".join(blockers))
    return {"released_by": actor.user_id}


def _validate_sample_create(actor, data, lookup):
    consignment = _find_one(lookup, "consignment", "id", data.get("consignment_id"))
    if not consignment:
        raise ValidationError("unknown consignment: " + str(data.get("consignment_id")))
    if consignment.get("status") in HOLD_EXEMPT_STATUSES:
        raise ValidationError(
            "cannot register samples for a %s consignment" % consignment["status"]
        )
    if lookup:
        for sample in lookup("sample", "consignment_id", data.get("consignment_id")) or []:
            if sample["data"].get("sample_no") == data.get("sample_no"):
                raise ConflictError("duplicate sample_no: " + str(data.get("sample_no")))


def _validate_sample_test(actor, entity, data, lookup):
    if data.get("conclusion") not in SAMPLE_CONCLUSIONS:
        raise ValidationError(
            "conclusion must be one of: " + ", ".join(SAMPLE_CONCLUSIONS)
        )
    return {"tested_by": actor.user_id, "last_tester": actor.user_id}


def _validate_sample_retest(actor, entity, data, lookup):
    if data.get("retest_conclusion") not in SAMPLE_CONCLUSIONS:
        raise ValidationError(
            "retest_conclusion must be one of: " + ", ".join(SAMPLE_CONCLUSIONS)
        )
    previous = entity["data"].get("last_tester") or entity["data"].get("tester")
    if actor.user_id == previous:
        raise ValidationError("retest must be performed by a different tester")
    return {"retester": actor.user_id, "last_tester": actor.user_id}


def _validate_trace(actor, entity, data, lookup):
    resolved = []
    for ref in data.get("consignment_ids", []):
        match = _find_one(lookup, "consignment", "id", ref) or _find_one(
            lookup, "consignment", "code", ref
        )
        if not match:
            raise ValidationError("unknown consignment: " + str(ref))
        resolved.append(
            {
                "id": match["id"],
                "code": match["data"].get("code"),
                "status": match["status"],
            }
        )
    return {"traced_consignments": resolved}


def sample_effective_conclusion(sample):
    data = sample.get("data", sample)
    return data.get("retest_conclusion") or data.get("conclusion")


def consignment_hold_reasons(samples):
    reasons = []
    for sample in samples:
        data = sample.get("data", {})
        label = data.get("sample_no") or sample.get("id")
        if sample.get("status") == "pending":
            reasons.append("sample %s pending" % label)
        elif sample_effective_conclusion(sample) == "positive":
            reasons.append("sample %s positive" % label)
    return reasons


def consignment_release_blockers(samples):
    if not samples:
        return ["no samples registered"]
    blockers = list(consignment_hold_reasons(samples))
    for sample in samples:
        if sample.get("status") == "tested":
            label = sample.get("data", {}).get("sample_no") or sample.get("id")
            blockers.append("sample %s not reviewed" % label)
    return blockers


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


CUSTOM_CREATE = {'consignment': _validate_consignment, 'sample': _validate_sample_create}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('sample', 'test'): _validate_sample_test, ('sample', 'retest'): _validate_sample_retest, ('facility', 'trace'): _validate_trace}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'samples': 'sample'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'sample': 'pending'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('declared', 'inspected', 'pending_disposal'), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}, 'sample': {'test': (('pending',), 'tested'), 'retest': (('tested', 'reviewed'), 'reviewed')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address'), 'sample': ('consignment_id', 'sample_no', 'sampling_point', 'tester')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): (), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',), ('sample', 'test'): ('conclusion',), ('sample', 'retest'): ('retest_conclusion',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'sample': ('admin', 'lab', 'inspector')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'test': ('admin', 'lab'), 'retest': ('admin', 'lab')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
