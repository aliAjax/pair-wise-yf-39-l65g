from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
from .rules import RuleEngine, evaluate_cluster


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
        return updated

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

    def _gather_candidates(self, cluster, observation_ids):
        """按编号收集候选记录；不存在或非 observation 的记录逐条指出。编号去重。"""
        candidates = []
        missing = []
        seen = set()
        for observation_id in observation_ids or []:
            observation_id = str(observation_id)
            if observation_id in seen:
                continue
            seen.add(observation_id)
            entity = self.repository.get_entity(observation_id)
            if not entity or entity["kind"] != "observation":
                missing.append({"id": observation_id, "reason": "observation not found"})
            else:
                candidates.append(entity)
        return candidates, missing

    def _evaluate_cluster(self, cluster, observation_ids):
        candidates, missing = self._gather_candidates(cluster, observation_ids)
        confirmed = {
            item["id"]
            for item in self.repository.list_entities(kind="cluster", status="confirmed")
        }
        member_ids, issues = evaluate_cluster(cluster, candidates, confirmed)
        issues = missing + issues
        members = [self.repository.get_entity(oid) for oid in member_ids]
        return members, issues

    def preview_cluster(self, cluster_id, observation_ids=None):
        """只核实不落库：返回候选问题与可成立的成员，供确认前核对。"""
        cluster = self.repository.get_entity(cluster_id)
        if not cluster or cluster["kind"] != "cluster":
            raise NotFoundError("entity not found: " + str(cluster_id))
        ids = observation_ids if observation_ids is not None else cluster["data"].get("observation_ids", [])
        members, issues = self._evaluate_cluster(cluster, ids)
        return {
            "cluster_id": cluster_id,
            "valid": not issues and len(members) >= 3,
            "issues": issues,
            "members": members,
        }

    def confirm_cluster(self, actor, cluster_id, data=None, expected_version=None):
        """核实聚集事件并确认；通过后把事件编号写回每份关联记录。

        重复提交（事件已确认）沿用第一次结果直接返回，不再改动数据。
        """
        self.rules.ensure_action_role(actor, "cluster", "confirm_cluster")
        cluster = self.repository.get_entity(cluster_id)
        if not cluster or cluster["kind"] != "cluster":
            raise NotFoundError("entity not found: " + cluster_id)
        if cluster["status"] == "confirmed":
            return cluster
        if cluster["status"] != "draft":
            raise InvalidTransition("cannot confirm_cluster from status %s" % cluster["status"])
        payload = dict(data or {})
        observation_ids = payload.get("observation_ids", cluster["data"].get("observation_ids"))
        if not observation_ids:
            raise ValidationError("missing required field: observation_ids")
        members, issues = self._evaluate_cluster(cluster, observation_ids)
        if issues or len(members) < 3:
            raise ValidationError(
                "cluster confirmation failed: %d problem(s), %d valid record(s)"
                % (len(issues), len(members)),
                issues=issues,
            )
        merged = dict(cluster["data"])
        merged.update(payload)
        expected = int(expected_version) if expected_version is not None else cluster["version"]
        member_ids = [item["id"] for item in members]
        updated = self.repository.confirm_cluster_members(cluster_id, expected, member_ids, merged)
        self.audit.record(
            cluster_id, actor, "confirm_cluster", cluster["status"], "confirmed",
            {"observation_ids": member_ids, "centroid": merged.get("centroid")},
        )
        for observation_id in member_ids:
            observation = self.repository.get_entity(observation_id)
            self.audit.record(
                observation_id, actor, "link_cluster",
                observation["status"], observation["status"],
                {"cluster_id": cluster_id},
            )
        return updated

    def cluster_members(self, cluster_id):
        """查看事件与观察记录的关联情况。"""
        cluster = self.repository.get_entity(cluster_id)
        if not cluster or cluster["kind"] != "cluster":
            raise NotFoundError("entity not found: " + str(cluster_id))
        linked = [
            item
            for item in self.repository.list_entities(kind="observation")
            if str(item["data"].get("cluster_id")) == str(cluster_id)
        ]
        return {
            "cluster": cluster,
            "observation_ids": cluster["data"].get("observation_ids", []),
            "linked_observations": linked,
        }
