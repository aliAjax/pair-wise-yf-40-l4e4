from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine

BLOCKABLE_STATUSES = ("declared", "inspected")
RESOLVED_STATUSES = ("inspected", "released", "destroyed")


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
        if updated["kind"] == "consignment":
            if updated["status"] == "quarantined":
                self._block_descendants(actor, updated)
            elif updated["status"] in RESOLVED_STATUSES:
                self._restore_descendants(actor, updated)
        return updated

    def _descendants(self, root_id):
        """BFS over the consignment source chain, nearest level first."""
        children = {}
        for item in self.repository.list_entities(kind="consignment"):
            parent_id = item["data"].get("parent_id")
            if parent_id:
                children.setdefault(parent_id, []).append(item)
        result = []
        queue = list(children.get(root_id, []))
        seen = {root_id}
        while queue:
            node = queue.pop(0)
            if node["id"] in seen:
                continue
            seen.add(node["id"])
            result.append(node)
            queue.extend(children.get(node["id"], []))
        return result

    def _block_descendants(self, actor, source):
        source_code = source["data"].get("code", source["id"])
        reason = "source batch %s quarantined" % source_code
        for desc in self._descendants(source["id"]):
            if desc["status"] not in BLOCKABLE_STATUSES:
                continue
            merged = dict(desc["data"])
            merged.update(
                {
                    "pre_block_status": desc["status"],
                    "blocked_by": source["id"],
                    "blocked_by_code": source_code,
                    "block_reason": reason,
                }
            )
            self.repository.update_entity(
                desc["id"], desc["version"], "pending_review", merged
            )
            self.audit.record(
                desc["id"],
                actor,
                "block",
                desc["status"],
                "pending_review",
                {
                    "blocked_by": source["id"],
                    "blocked_by_code": source_code,
                    "reason": reason,
                },
            )

    def _active_blocker(self, entity):
        """Nearest ancestor that still requires this batch to stay pending."""
        seen = {entity["id"]}
        current = entity
        while True:
            parent_id = current["data"].get("parent_id")
            if not parent_id or parent_id in seen:
                return None
            seen.add(parent_id)
            parent = self.repository.get_entity(parent_id)
            if not parent:
                return None
            if parent["status"] == "quarantined":
                return parent
            if parent["status"] == "pending_review":
                blocked_by = parent["data"].get("blocked_by")
                if blocked_by and blocked_by not in seen:
                    blocker = self.repository.get_entity(blocked_by)
                    if blocker:
                        return blocker
                return parent
            current = parent

    def _restore_descendants(self, actor, source):
        source_code = source["data"].get("code", source["id"])
        for desc in self._descendants(source["id"]):
            if desc["status"] != "pending_review":
                continue
            if desc["data"].get("blocked_by") != source["id"]:
                continue
            blocker = self._active_blocker(desc)
            if blocker:
                blocker_code = blocker["data"].get("code", blocker["id"])
                reason = "source batch %s quarantined" % blocker_code
                merged = dict(desc["data"])
                merged.update(
                    {
                        "blocked_by": blocker["id"],
                        "blocked_by_code": blocker_code,
                        "block_reason": reason,
                    }
                )
                self.repository.update_entity(
                    desc["id"], desc["version"], "pending_review", merged
                )
                self.audit.record(
                    desc["id"],
                    actor,
                    "reblock",
                    "pending_review",
                    "pending_review",
                    {
                        "blocked_by": blocker["id"],
                        "blocked_by_code": blocker_code,
                        "reason": reason,
                    },
                )
                continue
            merged = dict(desc["data"])
            target = merged.pop("pre_block_status", "declared")
            for key in ("blocked_by", "blocked_by_code", "block_reason"):
                merged.pop(key, None)
            self.repository.update_entity(desc["id"], desc["version"], target, merged)
            self.audit.record(
                desc["id"],
                actor,
                "restore",
                "pending_review",
                target,
                {
                    "reason": "blocking source %s resolved (%s)"
                    % (source_code, source["status"]),
                    "source": source["id"],
                    "restored_to": target,
                },
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
