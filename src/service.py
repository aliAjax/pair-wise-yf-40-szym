from uuid import uuid4

from .audit import AuditTrail
from .domain import NotFoundError
from .rules import STATUS_LABELS, RuleEngine, batch_hold_reasons


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _present(self, entity):
        if entity is None:
            return None
        item = dict(entity)
        item["status_label"] = STATUS_LABELS.get(entity["status"], entity["status"])
        if entity["kind"] == "consignment":
            reasons = batch_hold_reasons(entity["data"])
            item["data"] = dict(entity["data"])
            item["data"]["hold_reasons"] = reasons
        return item

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
        return self._present(entity)

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
        return self._present(updated)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self._present(entity)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return [self._present(item) for item in self.repository.list_entities(kind=kind, status=status)]

    def trace_consignment(self, code=None, entity_id=None):
        """按批次编号或ID追溯：批次放行后退回待处置，记录仍按编号可查。"""
        entity = None
        if entity_id:
            entity = self.repository.get_entity(entity_id)
        elif code:
            rows = self.repository.find_entities("consignment", "code", code)
            entity = rows[0] if rows else None
        if not entity:
            raise NotFoundError("consignment not found: " + str(code or entity_id))
        downstream = self.repository.list_entities(kind="facility")
        facilities = [
            item
            for item in downstream
            if entity["id"] in (item["data"].get("consignment_ids") or [])
        ]
        return {
            "consignment": self._present(entity),
            "traced_facilities": [self._present(item) for item in facilities],
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
