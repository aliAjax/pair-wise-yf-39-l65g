from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_observation(actor, data, lookup):
    rows = lookup("observation", "event_id", data.get("event_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("observed_at") == data.get("observed_at"):
            raise ConflictError("duplicate observation event")
    if not data.get("species"):
        raise ValidationError("species is required")


def _validate_sample(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] not in ("submitted", "sampled"):
        raise ValidationError("sample requires a submitted observation")


def _validate_lab_result(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "negative"):
        raise ValidationError("lab result must be positive or negative")


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def _field(item, key):
    """兼容扁平 dict（单元测试）与完整实体（坐标等在 data 下）。"""
    data = item.get("data") if isinstance(item, dict) else None
    if isinstance(data, dict) and key in data:
        return data[key]
    return item.get(key) if isinstance(item, dict) else None


def _has_coords(observation):
    return _field(observation, "lat") is not None and _field(observation, "lon") is not None


def is_cluster(observations, max_days=14, radius_km=10):
    """至少三份记录，且全部两两满足 14 天 / 10 公里才算聚集。"""
    if len(observations) < 3:
        return False
    points = [item for item in observations if _has_coords(item)]
    if len(points) != len(observations) or len(points) < 3:
        return False
    ordinals = [_date_ordinal(_field(item, "observed_at")) for item in points]
    same_window = (max(ordinals) - min(ordinals)) <= max_days
    close = all(
        _haversine_km(_field(points[i], "lat"), _field(points[i], "lon"),
                      _field(points[j], "lat"), _field(points[j], "lon")) <= radius_km
        for i in range(len(points))
        for j in range(i + 1, len(points))
    )
    return same_window and close


def evaluate_cluster(cluster, observations, confirmed_cluster_ids,
                     min_records=3, max_days=14, radius_km=10):
    """核实候选记录，返回 (member_ids, issues)。

    member_ids：通过全部校验的记录编号；issues：[{"id", "reason"}]。
    已归入其他已确认事件、缺坐标、非 submitted、跨区域或不满足
    14 天 / 10 公里条件都会逐条指出问题与对应编号。
    """
    issues = []
    members = []
    cluster_data = cluster.get("data", cluster) if isinstance(cluster, dict) else {}
    region = cluster_data.get("region")
    cluster_id = str(cluster.get("id") or "")
    for observation in observations:
        oid = observation["id"]
        data = observation.get("data", {})
        if observation["status"] != "submitted":
            issues.append({"id": oid, "reason": "status is %s, only submitted records count" % observation["status"]})
            continue
        linked = data.get("cluster_id")
        if linked and str(linked) != cluster_id and str(linked) in confirmed_cluster_ids:
            issues.append({"id": oid, "reason": "already linked to confirmed cluster %s" % linked})
            continue
        if not _has_coords(observation):
            issues.append({"id": oid, "reason": "missing coordinates (lat/lon)"})
            continue
        obs_region = data.get("region", data.get("location"))
        if obs_region != region:
            issues.append({"id": oid, "reason": "region %r does not match cluster region %r" % (obs_region, region)})
            continue
        try:
            _date_ordinal(data.get("observed_at"))
        except (TypeError, ValueError):
            issues.append({"id": oid, "reason": "invalid observed_at: %r" % data.get("observed_at")})
            continue
        members.append(observation)

    if len(members) < min_records:
        if not issues:
            issues.append({
                "id": None,
                "reason": "cluster needs at least %d submitted records, found %d" % (min_records, len(members)),
            })
        return [], issues

    if not is_cluster(members, max_days, radius_km):
        flagged = set()
        ordinals = {item["id"]: _date_ordinal(_field(item, "observed_at")) for item in members}
        ids_by_ordinal = {}
        for item in members:
            ids_by_ordinal.setdefault(ordinals[item["id"]], item["id"])
        ordered = sorted(ids_by_ordinal.items())
        if ordered[-1][0] - ordered[0][0] > max_days:
            # 端点与对侧最近一个点仍超过窗口，说明该端点本身脱离时间窗
            earliest, latest = ordered[0][0], ordered[-1][0]
            middle = [value for value, _ in ordered[1:-1]] or [
                value for value in (earliest, latest)
            ]
            if latest - min(middle) > max_days:
                oid = ordered[-1][1]
                flagged.add(oid)
                issues.append({"id": oid, "reason": "sampling time exceeds %d-day window" % max_days})
            if max(middle) - earliest > max_days:
                oid = ordered[0][1]
                flagged.add(oid)
                issues.append({"id": oid, "reason": "sampling time exceeds %d-day window" % max_days})
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                if right["id"] in flagged:
                    continue
                distance = _haversine_km(
                    _field(left, "lat"), _field(left, "lon"),
                    _field(right, "lat"), _field(right, "lon"),
                )
                if distance > radius_km:
                    flagged.add(right["id"])
                    issues.append({
                        "id": right["id"],
                        "reason": "distance %.1fkm from %s exceeds %skm" % (distance, left["id"], radius_km),
                    })
        if not issues:
            issues.append({"id": None, "reason": "records do not form a cluster within %d days / %skm" % (max_days, radius_km)})
        return [], issues

    return [item["id"] for item in members], issues


CUSTOM_CREATE = {'observation': _validate_observation, 'sample': _validate_sample}
CUSTOM_TRANSITIONS = {('sample', 'lab_result'): _validate_lab_result}


class RuleEngine:
    ALIASES = {'observations': 'observation', 'samples': 'sample', 'clusters': 'cluster'}
    INITIAL_STATUS = {'observation': 'captured', 'sample': 'collected', 'cluster': 'draft'}
    TRANSITIONS = {'observation': {'submit': (('captured',), 'submitted'), 'reject': (('submitted',), 'rejected'), 'link_sample': (('submitted',), 'sampled')}, 'sample': {'send_lab': (('collected',), 'in_lab'), 'lab_result': (('in_lab',), 'resulted'), 'retest': (('resulted',), 'in_lab'), 'close': (('resulted',), 'closed')}, 'cluster': {'confirm_cluster': (('draft',), 'confirmed'), 'dismiss': (('draft',), 'dismissed')}}
    CREATE_REQUIRED = {'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'), 'sample': ('observation_id', 'sample_code'), 'cluster': ('region',)}
    ACTION_REQUIRED = {('observation', 'submit'): ('location', 'observed_at'), ('observation', 'reject'): ('reason',), ('observation', 'link_sample'): ('sample_id',), ('sample', 'send_lab'): ('lab_id',), ('sample', 'lab_result'): ('result', 'result_at'), ('sample', 'retest'): ('reason',), ('sample', 'close'): ('outcome',), ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'), ('cluster', 'dismiss'): ('reason',)}
    CREATE_ROLES = {'observation': ('admin', 'field'), 'sample': ('admin', 'field'), 'cluster': ('admin', 'epidemiologist')}
    ROLE_ACTIONS = {'submit': ('admin', 'field'), 'reject': ('admin', 'epidemiologist'), 'link_sample': ('admin', 'field'), 'send_lab': ('admin', 'field'), 'lab_result': ('admin', 'lab'), 'retest': ('admin', 'lab'), 'close': ('admin', 'epidemiologist'), 'confirm_cluster': ('admin', 'epidemiologist'), 'dismiss': ('admin', 'epidemiologist')}

    def ensure_action_role(self, actor, kind, action):
        kind = self.normalize_kind(kind)
        allowed = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed)

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
