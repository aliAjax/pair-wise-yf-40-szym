from copy import deepcopy
from datetime import datetime, timezone

from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 样本结论
PENDING = "pending"
NEGATIVE = "negative"
POSITIVE = "positive"
SAMPLE_CONCLUSIONS = (PENDING, NEGATIVE, POSITIVE)
RECHECK_CONCLUSIONS = (NEGATIVE, POSITIVE)

# 批次/设施状态中文名
STATUS_LABELS = {
    "declared": "已申报",
    "pending_disposition": "待处置",
    "released": "已放行",
    "quarantined": "已隔离",
    "destroyed": "已销毁",
    "registered": "已登记",
    "traced": "已追溯",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _samples(data):
    return list(data.get("samples") or [])


def _find_sample(samples, sample_no):
    for sample in samples:
        if sample.get("sample_no") == sample_no:
            return sample
    return None


def _latest_recheck(sample):
    rechecks = sample.get("rechecks") or []
    return rechecks[-1] if rechecks else None


def _is_positive(sample):
    if sample.get("conclusion") == POSITIVE:
        return True
    latest = _latest_recheck(sample)
    return bool(latest and latest.get("conclusion") == POSITIVE)


def batch_hold_reasons(data):
    """返回批次停在“待处置”的原因；为空表示满足放行条件。"""
    reasons = []
    samples = _samples(data)
    if not samples:
        reasons.append("尚未登记任何检测样本")
        return reasons
    for sample in samples:
        label = sample.get("sample_no")
        conclusion = sample.get("conclusion")
        if not conclusion or conclusion == PENDING:
            reasons.append("样本 %s 初检结果待检" % label)
        elif conclusion == POSITIVE:
            reasons.append("样本 %s 初检结论为阳性" % label)
        latest = _latest_recheck(sample)
        if latest is None:
            reasons.append("样本 %s 尚未由另一名检测人复核" % label)
        elif latest.get("conclusion") == POSITIVE:
            reasons.append(
                "样本 %s 复核结论为阳性（复核人 %s）"
                % (label, latest.get("tester"))
            )
    return reasons


def _require_text(data, field):
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("missing required field: " + field)
    return value.strip()


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_register_sample(actor, entity, data, lookup):
    sample_no = _require_text(data, "sample_no")
    sampling_point = _require_text(data, "sampling_point")
    tester = _require_text(data, "tester")
    conclusion = data.get("conclusion") or PENDING
    if conclusion not in SAMPLE_CONCLUSIONS:
        raise ValidationError("conclusion must be one of pending/negative/positive")
    samples = _samples(entity["data"])
    if _find_sample(samples, sample_no):
        raise ValidationError("sample_no already registered: " + sample_no)
    samples.append(
        {
            "sample_no": sample_no,
            "sampling_point": sampling_point,
            "tester": tester,
            "conclusion": conclusion,
            "registered_at": _now(),
            "tested_at": _now() if conclusion != PENDING else None,
            "rechecks": [],
        }
    )
    return {"samples": samples}


def _validate_record_result(actor, entity, data, lookup):
    """实验室补出初检结论（待检 -> 阴性/阳性）。"""
    sample_no = _require_text(data, "sample_no")
    conclusion = data.get("conclusion")
    if conclusion not in RECHECK_CONCLUSIONS:
        raise ValidationError("conclusion must be negative or positive")
    samples = deepcopy(_samples(entity["data"]))
    sample = _find_sample(samples, sample_no)
    if sample is None:
        raise ValidationError("sample not found: " + sample_no)
    if sample.get("conclusion") != PENDING:
        raise ValidationError("sample result already recorded: " + sample_no)
    sample["conclusion"] = conclusion
    sample["tested_at"] = _now()
    return {"samples": samples}


def _validate_recheck(actor, entity, data, lookup):
    """复检/复核：必须由另一名检测人完成。"""
    sample_no = _require_text(data, "sample_no")
    tester = _require_text(data, "tester")
    conclusion = data.get("conclusion")
    if conclusion not in RECHECK_CONCLUSIONS:
        raise ValidationError("conclusion must be negative or positive")
    samples = deepcopy(_samples(entity["data"]))
    sample = _find_sample(samples, sample_no)
    if sample is None:
        raise ValidationError("sample not found: " + sample_no)
    if sample.get("conclusion") == PENDING:
        raise ValidationError("初检结论尚未出具，不能复检样本 %s" % sample_no)
    if tester == sample.get("tester"):
        raise ValidationError(
            "复检必须由另一名检测人完成，不能与初检检测人 %s 为同一人"
            % sample.get("tester")
        )
    sample.setdefault("rechecks", []).append(
        {"tester": tester, "conclusion": conclusion, "at": _now()}
    )
    return {"samples": samples}


def _recheck_target(actor, entity, data, patch, lookup):
    current = entity["status"]
    merged = dict(entity["data"])
    merged.update(patch)
    if current == "released":
        # 放行后复检改出阳性（或补出待处理问题）：退回待处置；阴性则维持放行
        return "pending_disposition" if batch_hold_reasons(merged) else "released"
    if current == "quarantined":
        # 复核后仍有阳性维持隔离，否则回到待处置重新决定去向
        still_positive = any(_is_positive(sample) for sample in _samples(merged))
        return "quarantined" if still_positive else "pending_disposition"
    return "pending_disposition"


def _validate_release(actor, entity, data, lookup):
    reasons = batch_hold_reasons(entity["data"])
    if reasons:
        raise ValidationError("批次不能放行：" + "；".join(reasons))
    record = {"released_by": actor.user_id, "released_at": _now()}
    # 原放行记录在退回待处置后仍需保留
    history = list(entity["data"].get("release_history") or [])
    history.append(record)
    return {
        "released_by": actor.user_id,
        "released_at": record["released_at"],
        "release_history": history,
    }


def _validate_quarantine(actor, entity, data, lookup):
    positives = [
        sample.get("sample_no")
        for sample in _samples(entity["data"])
        if _is_positive(sample)
    ]
    if not positives:
        raise ValidationError("只有存在阳性样本的批次才能隔离")
    return {"quarantined_by": actor.user_id, "positive_samples": positives}


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_trace(actor, entity, data, lookup):
    refs = data.get("consignment_ids")
    if not isinstance(refs, list) or not refs:
        raise ValidationError("missing required field: consignment_ids")
    resolved = []
    for ref in refs:
        # 设施追溯支持批次编号(code)或实体ID，批次状态变化后仍按编号可查
        consignment = _find_one(lookup, "consignment", "id", ref)
        if consignment is None:
            consignment = _find_one(lookup, "consignment", "code", ref)
        if consignment is None:
            raise ValidationError("追溯关联的批次不存在: %s" % ref)
        resolved.append(
            {"id": consignment["id"], "code": consignment["data"].get("code")}
        )
    return {
        "consignment_ids": [item["id"] for item in resolved],
        "traced_consignments": resolved,
    }


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


CUSTOM_CREATE = {"consignment": _validate_consignment}
CUSTOM_TRANSITIONS = {
    ("consignment", "register_sample"): _validate_register_sample,
    ("consignment", "record_result"): _validate_record_result,
    ("consignment", "recheck"): _validate_recheck,
    ("consignment", "release"): _validate_release,
    ("consignment", "quarantine"): _validate_quarantine,
    ("facility", "trace"): _validate_trace,
}


class RuleEngine:
    ALIASES = {"consignments": "consignment", "facilities": "facility"}
    INITIAL_STATUS = {"consignment": "declared", "facility": "registered"}
    # action: (允许的源状态, 目标状态或目标状态计算函数)
    TRANSITIONS = {
        "consignment": {
            "register_sample": (
                ("declared", "pending_disposition"),
                "pending_disposition",
            ),
            "record_result": (
                ("pending_disposition",),
                "pending_disposition",
            ),
            "recheck": (
                ("pending_disposition", "released", "quarantined"),
                _recheck_target,
            ),
            "release": (("pending_disposition",), "released"),
            "quarantine": (("pending_disposition",), "quarantined"),
            "destroy": (("quarantined",), "destroyed"),
        },
        "facility": {"trace": (("registered",), "traced")},
    }
    CREATE_REQUIRED = {
        "consignment": ("code", "origin", "destination"),
        "facility": ("name", "address"),
    }
    ACTION_REQUIRED = {
        ("consignment", "register_sample"): ("sample_no", "sampling_point", "tester"),
        ("consignment", "record_result"): ("sample_no", "conclusion"),
        ("consignment", "recheck"): ("sample_no", "tester", "conclusion"),
        ("consignment", "release"): (),
        ("consignment", "quarantine"): (),
        ("consignment", "destroy"): ("method", "witnessed_by"),
        ("facility", "trace"): ("consignment_ids",),
    }
    CREATE_ROLES = {
        "consignment": ("admin", "inspector"),
        "facility": ("admin", "quarantine"),
    }
    ROLE_ACTIONS = {
        "register_sample": ("admin", "inspector", "lab"),
        "record_result": ("admin", "lab"),
        "recheck": ("admin", "lab"),
        "release": ("admin", "quarantine"),
        "quarantine": ("admin", "quarantine"),
        "destroy": ("admin", "quarantine"),
        "trace": ("admin", "quarantine"),
    }

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
        allowed_statuses, target = transition
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
        if callable(target):
            next_status = target(actor, entity, data, patch, lookup)
        else:
            next_status = target
        return next_status, patch
