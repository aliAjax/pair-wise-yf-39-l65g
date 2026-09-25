from datetime import datetime, timedelta
from itertools import combinations

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

CLUSTER_MIN_OBSERVATIONS = 3
CLUSTER_MAX_DAYS = 14
CLUSTER_RADIUS_KM = 10.0


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


def is_cluster(observations, max_days=CLUSTER_MAX_DAYS, radius_km=CLUSTER_RADIUS_KM):
    points = [
        item
        for item in observations
        if item.get("lat") is not None
        and item.get("lon") is not None
        and item.get("observed_at")
    ]
    if len(points) < CLUSTER_MIN_OBSERVATIONS:
        return False
    dates = [_date_ordinal(item["observed_at"]) for item in points]
    if max(dates) - min(dates) > max_days:
        return False
    for first, second in combinations(points, 2):
        if _haversine_km(first["lat"], first["lon"], second["lat"], second["lon"]) > radius_km:
            return False
    return True


def _validate_confirm_cluster(actor, entity, data, lookup):
    observation_ids = list(dict.fromkeys(data.get("observation_ids") or []))
    problems = []
    observations = []
    for observation_id in observation_ids:
        found = _find_one(lookup, "observation", "id", observation_id)
        if found is None:
            problems.append("observation not found: %s" % observation_id)
        else:
            observations.append(found)

    submitted = [item for item in observations if item["status"] == "submitted"]
    not_submitted = [item["id"] for item in observations if item["status"] != "submitted"]
    if not_submitted:
        problems.append("observations not submitted: %s" % ", ".join(not_submitted))
    if len(submitted) < CLUSTER_MIN_OBSERVATIONS:
        problems.append(
            "at least %d submitted observations are required, got %d"
            % (CLUSTER_MIN_OBSERVATIONS, len(submitted))
        )

    missing_coords = [
        item["id"]
        for item in observations
        if item["data"].get("lat") is None or item["data"].get("lon") is None
    ]
    if missing_coords:
        problems.append("observations missing coordinates: %s" % ", ".join(missing_coords))

    region = entity["data"].get("region")
    wrong_region = [
        item["id"] for item in observations if item["data"].get("location") != region
    ]
    if wrong_region:
        problems.append(
            "observations outside region %s: %s" % (region, ", ".join(wrong_region))
        )

    for item in observations:
        other = item["data"].get("cluster_id")
        if not other or other == entity["id"]:
            continue
        other_cluster = _find_one(lookup, "cluster", "id", other)
        if other_cluster and other_cluster["status"] == "confirmed":
            problems.append(
                "observation %s already belongs to confirmed cluster %s"
                % (item["id"], other)
            )

    usable = [
        item
        for item in submitted
        if item["data"].get("lat") is not None and item["data"].get("lon") is not None
    ]
    dates = []
    for item in usable:
        try:
            dates.append(_date_ordinal(item["data"].get("observed_at")))
        except (TypeError, ValueError):
            problems.append("observation %s has invalid observed_at" % item["id"])
    if dates and max(dates) - min(dates) > CLUSTER_MAX_DAYS:
        problems.append(
            "observations span more than %d days" % CLUSTER_MAX_DAYS
        )
    far_pairs = []
    for first, second in combinations(usable, 2):
        distance = _haversine_km(
            first["data"]["lat"],
            first["data"]["lon"],
            second["data"]["lat"],
            second["data"]["lon"],
        )
        if distance > CLUSTER_RADIUS_KM:
            far_pairs.append(
                "%s<->%s (%.1f km)" % (first["id"], second["id"], distance)
            )
    if far_pairs:
        problems.append(
            "observations more than %s km apart: %s"
            % (CLUSTER_RADIUS_KM, ", ".join(far_pairs))
        )

    if problems:
        raise ValidationError("; ".join(problems))
    return {"observation_ids": observation_ids}


CUSTOM_CREATE = {'observation': _validate_observation, 'sample': _validate_sample}
CUSTOM_TRANSITIONS = {('sample', 'lab_result'): _validate_lab_result, ('cluster', 'confirm_cluster'): _validate_confirm_cluster}


class RuleEngine:
    ALIASES = {'observations': 'observation', 'samples': 'sample', 'clusters': 'cluster'}
    INITIAL_STATUS = {'observation': 'captured', 'sample': 'collected', 'cluster': 'draft'}
    TRANSITIONS = {'observation': {'submit': (('captured',), 'submitted'), 'reject': (('submitted',), 'rejected'), 'link_sample': (('submitted',), 'sampled')}, 'sample': {'send_lab': (('collected',), 'in_lab'), 'lab_result': (('in_lab',), 'resulted'), 'retest': (('resulted',), 'in_lab'), 'close': (('resulted',), 'closed')}, 'cluster': {'confirm_cluster': (('draft',), 'confirmed'), 'dismiss': (('draft',), 'dismissed')}}
    CREATE_REQUIRED = {'observation': ('event_id', 'species', 'location', 'observed_at'), 'sample': ('observation_id', 'sample_code'), 'cluster': ('region',)}
    ACTION_REQUIRED = {('observation', 'submit'): ('location', 'observed_at'), ('observation', 'reject'): ('reason',), ('observation', 'link_sample'): ('sample_id',), ('sample', 'send_lab'): ('lab_id',), ('sample', 'lab_result'): ('result', 'result_at'), ('sample', 'retest'): ('reason',), ('sample', 'close'): ('outcome',), ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'), ('cluster', 'dismiss'): ('reason',)}
    CREATE_ROLES = {'observation': ('admin', 'field'), 'sample': ('admin', 'field'), 'cluster': ('admin', 'epidemiologist')}
    ROLE_ACTIONS = {'submit': ('admin', 'field'), 'reject': ('admin', 'epidemiologist'), 'link_sample': ('admin', 'field'), 'send_lab': ('admin', 'field'), 'lab_result': ('admin', 'lab'), 'retest': ('admin', 'lab'), 'close': ('admin', 'epidemiologist'), 'confirm_cluster': ('admin', 'epidemiologist'), 'dismiss': ('admin', 'epidemiologist')}

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
