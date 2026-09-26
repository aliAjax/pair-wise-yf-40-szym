from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import (
    HOLD_EXEMPT_STATUSES,
    HOLD_STATUS,
    RuleEngine,
    consignment_hold_reasons,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "sample":
            self._sync_consignment_hold(actor, entity["data"].get("consignment_id"))
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "sample":
            self._sync_consignment_hold(actor, updated["data"].get("consignment_id"))
        return updated

    def _sync_consignment_hold(self, actor, consignment_id):
        if not consignment_id:
            return
        consignment = self.repository.get_entity(consignment_id)
        if not consignment or consignment["kind"] != "consignment":
            return
        status = consignment["status"]
        if status in HOLD_EXEMPT_STATUSES:
            return
        samples = self.repository.find_entities("sample", "consignment_id", consignment_id)
        reasons = consignment_hold_reasons(samples)
        data = dict(consignment["data"])
        if reasons:
            if status == HOLD_STATUS and data.get("hold_reasons") == reasons:
                return
            if status != HOLD_STATUS:
                data["hold_return_status"] = status
            data["hold_reasons"] = reasons
            self.repository.update_entity(consignment_id, None, HOLD_STATUS, data)
            self.audit.record(
                consignment_id,
                actor,
                "hold",
                status,
                HOLD_STATUS,
                {"reasons": reasons},
            )
        elif status == HOLD_STATUS:
            target = data.pop("hold_return_status", None) or "inspected"
            data["hold_reasons"] = []
            self.repository.update_entity(consignment_id, None, target, data)
            self.audit.record(
                consignment_id,
                actor,
                "hold_cleared",
                HOLD_STATUS,
                target,
                {},
            )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
