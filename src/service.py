from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import BLOCKABLE_STATUSES, HELD_STATUSES, PENDING_REVIEW, RuleEngine


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
        if kind == "consignment":
            entity = self._block_if_source_held(entity, actor)
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
        if entity["kind"] == "consignment":
            if action == "quarantine":
                self._cascade_block(entity_id, actor, "source_quarantined", action)
            elif action == "release":
                self._cascade_restore(entity_id, actor, "source_released", action)
            elif action == "destroy":
                self._cascade_restore(entity_id, actor, "source_destroyed", action)
        return updated

    def _children_of(self, entity_id):
        return [
            item
            for item in self.repository.list_entities(kind="consignment")
            if item["data"].get("parent_id") == entity_id
        ]

    def _find_active_blocker(self, entity):
        """Walk the source chain; return the batch responsible for an active hold."""
        seen = set()
        parent_id = entity["data"].get("parent_id")
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = self.repository.get_entity(parent_id)
            if not parent:
                return None
            if parent["status"] == "quarantined":
                return parent["id"]
            if parent["status"] == PENDING_REVIEW:
                return parent["data"].get("blocked_by") or parent["id"]
            parent_id = parent["data"].get("parent_id")
        return None

    def _block_if_source_held(self, entity, actor):
        blocker_id = self._find_active_blocker(entity)
        if not blocker_id:
            return entity
        return self._apply_block(entity, blocker_id, actor, "source_quarantined", "create")

    def _apply_block(self, entity, blocker_id, actor, reason, trigger):
        data = dict(entity["data"])
        if entity["status"] != PENDING_REVIEW:
            data["resume_status"] = entity["status"]
        data["blocked_by"] = blocker_id
        data["block_reason"] = reason
        updated = self.repository.update_entity(entity["id"], None, PENDING_REVIEW, data)
        self.audit.record(
            entity["id"],
            actor,
            "block",
            entity["status"],
            PENDING_REVIEW,
            {
                "blocked_by": blocker_id,
                "reason": reason,
                "trigger": trigger,
                "resume_status": data["resume_status"],
            },
        )
        return updated

    def _apply_restore(self, entity, actor, reason, trigger):
        data = dict(entity["data"])
        resume = data.pop("resume_status", None) or "declared"
        blocked_by = data.pop("blocked_by", None)
        data.pop("block_reason", None)
        updated = self.repository.update_entity(entity["id"], None, resume, data)
        self.audit.record(
            entity["id"],
            actor,
            "restore",
            PENDING_REVIEW,
            resume,
            {"unblocked_by": blocked_by, "reason": reason, "trigger": trigger},
        )
        return updated

    def _cascade_block(self, source_id, actor, reason, trigger):
        """Move every circulating downstream batch to pending review, level by level."""
        visited = set()
        queue = self._children_of(source_id)
        while queue:
            current = queue.pop(0)
            if current["id"] in visited:
                continue
            visited.add(current["id"])
            if current["status"] in BLOCKABLE_STATUSES:
                self._apply_block(current, source_id, actor, reason, trigger)
                queue.extend(self._children_of(current["id"]))
            elif current["status"] == PENDING_REVIEW:
                queue.extend(self._children_of(current["id"]))

    def _cascade_restore(self, source_id, actor, reason, trigger):
        """Restore held descendants level by level, stopping where an upstream hold remains."""
        visited = set()
        queue = self._children_of(source_id)
        while queue:
            current = queue.pop(0)
            if current["id"] in visited:
                continue
            visited.add(current["id"])
            if current["status"] == PENDING_REVIEW:
                parent_id = current["data"].get("parent_id")
                parent = self.repository.get_entity(parent_id) if parent_id else None
                if parent is None or parent["status"] not in HELD_STATUSES:
                    self._apply_restore(current, actor, reason, trigger)
                    queue.extend(self._children_of(current["id"]))
            elif current["status"] in BLOCKABLE_STATUSES:
                queue.extend(self._children_of(current["id"]))

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
