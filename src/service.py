from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


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
        payload = dict(data or {})
        if (
            entity["kind"] == "cluster"
            and action == "confirm_cluster"
            and entity["status"] == "confirmed"
        ):
            return self._replay_confirm(entity, payload)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        if entity["kind"] == "cluster" and action == "confirm_cluster":
            return self._confirm_cluster(actor, entity, expected, next_status, merged, patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    @staticmethod
    def _replay_confirm(entity, payload):
        requested = payload.get("observation_ids")
        if requested is None:
            return entity
        existing = entity["data"].get("observation_ids") or []
        if list(dict.fromkeys(requested)) == list(existing):
            return entity
        raise ConflictError(
            "cluster already confirmed with a different set of observations"
        )

    def _confirm_cluster(self, actor, entity, expected, next_status, merged, patch):
        updates = [
            {
                "id": entity["id"],
                "expected_version": expected,
                "status": next_status,
                "data": merged,
            }
        ]
        observations = []
        for observation_id in merged.get("observation_ids") or []:
            observation = self.repository.get_entity(observation_id)
            if observation is None:
                continue
            linked = dict(observation["data"])
            linked["cluster_id"] = entity["id"]
            observations.append(observation)
            updates.append(
                {
                    "id": observation["id"],
                    "expected_version": None,
                    "status": observation["status"],
                    "data": linked,
                }
            )
        updated = self.repository.update_entities(updates)[0]
        self.audit.record(
            entity["id"],
            actor,
            "confirm_cluster",
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        for observation in observations:
            self.audit.record(
                observation["id"],
                actor,
                "link_cluster",
                observation["status"],
                observation["status"],
                {"cluster_id": entity["id"]},
            )
        return updated

    def cluster_observations(self, cluster_id):
        cluster = self.get(cluster_id)
        if cluster["kind"] != "cluster":
            raise ValidationError("entity is not a cluster: " + cluster_id)
        observations = []
        missing = []
        for observation_id in cluster["data"].get("observation_ids") or []:
            observation = self.repository.get_entity(observation_id)
            if observation is None:
                missing.append(observation_id)
            else:
                observations.append(observation)
        return {"cluster": cluster, "observations": observations, "missing": missing}

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
