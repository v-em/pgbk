"""Captured obligations and read-time state; artifact publication stays immutable."""
import copy
import json
from importlib.resources import files
from datetime import datetime, timedelta, timezone

from .config import digest
from .errors import Error
from .kube import lease_name


def snapshot(config, profile_name, completed_at):
    policy = config["profiles"][profile_name]["verification"]
    result = {key: copy.deepcopy(policy[key]) for key in
              ["enabled", "required", "environmentRef", "dueWithinSeconds"] if key in policy}
    result["required"] = policy.get("required", False)
    if policy["enabled"]:
        result["environment"] = copy.deepcopy(config["verificationEnvironments"][policy["environmentRef"]])
        result["environmentDigest"] = digest(result["environment"])
    result["dueAt"] = ((datetime.fromisoformat(completed_at) + timedelta(seconds=policy["dueWithinSeconds"])).isoformat()
                       if result["required"] else None)
    return result


def state(manifest, reports=(), active=None, clock=None):
    from .catalog import timestamp
    clock = clock or datetime.now(timezone.utc)
    policy = manifest.get("verificationPolicy", {"enabled": False, "required": False})
    # Sequence is assigned under repository coordination. Legacy reports use completion time.
    ordered = sorted(reports, key=lambda r: (r.get("sequence", 0), timestamp(r["finishedAt"]), r["verificationId"]))
    latest = ordered[-1] if ordered else None
    required = policy.get("required", False)
    due = policy.get("dueAt")
    overdue = bool(required and due and timestamp(due) <= clock and (not latest or latest["status"] != "passed"))
    status = "successful"
    if required:
        if latest:
            status = "verification-successful" if latest["status"] == "passed" else "verification-failed"
        if overdue:
            status = "verification-failed"
    pending = bool(required and not latest and not overdue)
    snapshots = [{"database": a["database"], "snapshotAt": a.get("snapshotAt"),
                  "snapshotTimeBasis": a.get("snapshotTimeBasis", "unknown")}
                 for a in manifest["artifacts"] if a.get("database")]
    times = [s["snapshotAt"] for s in snapshots]
    snapshot_at = min(times, key=timestamp) if times and all(times) else manifest.get("snapshotAt")
    return {"artifactStatus": "Completed", "status": status,
            "profileCompliant": not required or status == "verification-successful",
            "verificationPending": pending, "verificationOverdue": overdue,
            "waitingProtected": pending, "operationPinned": active is not None,
            "snapshotAt": snapshot_at, "databaseSnapshots": snapshots,
            "snapshotTimeBasis": "backup-start-lower-bound" if snapshot_at else "unknown",
            "restoreVerification": {"status": "overdue" if overdue else latest["status"] if latest else "pending" if required else "not-run",
                                    "required": required, "dueAt": due, "latestAttempt": latest,
                                    "activeAttempt": active, "attempts": ordered,
                                    "reasonCode": "VERIFICATION_OVERDUE" if overdue else latest.get("reasonCode") if latest else None}}


def slot_names(environment):
    return [lease_name("verification", environment["concurrencyKey"] + "/" + str(i))
            for i in range(environment.get("maxConcurrent", 1))]


def acquire_slot(kube, environment, control):
    for name in slot_names(environment):
        lease = kube.lease(name, control)
        try:
            lease.__enter__()
            return lease
        except Error as e:
            if e.exit_code != 4:
                raise
    raise Error("VERIFICATION_CAPACITY_BUSY", "All configured verification slots are occupied", 4)


def coverage(backups):
    from .catalog import timestamp
    def freshest(items):
        # Completion time is an explicitly labelled fallback, never a verification timestamp.
        if not items:
            return None
        item = max(items, key=lambda r: (timestamp(r.get("snapshotAt") or r["completedAt"]), r["runId"]))
        return {"runId": item["runId"], "snapshotAt": item.get("snapshotAt"),
                "dataTime": item.get("snapshotAt") or item["completedAt"],
                "timeBasis": item.get("snapshotTimeBasis") if item.get("snapshotAt") else "completion-time-fallback"}
    return {"latestIntegrityConfirmed": freshest(backups),
            "latestProfileCompliant": freshest([r for r in backups if r["profileCompliant"]]),
            "verificationAlerts": [{"runId": r["runId"], "reasonCode": r["restoreVerification"]["reasonCode"]}
                                   for r in backups if r["status"] == "verification-failed"]}


def validate_environment(data):
    import jsonschema
    try:
        environment = json.loads(data) if isinstance(data, str) else copy.deepcopy(data)
        schema = json.loads(files("pgbk").joinpath("values.schema.json").read_text())
        rule = next(iter(schema["properties"]["verificationEnvironments"]["patternProperties"].values()))
        jsonschema.Draft7Validator(rule).validate(environment)
        from kubernetes.utils.quantity import parse_quantity
        scratch = environment["scratch"]
        if parse_quantity(scratch["size"]) <= 0 or (scratch["type"] == "existingClaim" and
                (scratch["claimName"] not in scratch["allowedClaims"] or scratch.get("retainFailed"))):
            raise ValueError()
        for resource in ["cpu", "memory"]:
            if not 0 < parse_quantity(environment["resources"]["requests"][resource]) <= parse_quantity(environment["resources"]["limits"][resource]):
                raise ValueError()
        from .selection import validate_query
        for assertion in environment.get("assertions", []):
            validate_query(assertion)
        environment.setdefault("maxConcurrent", 1)
        environment.setdefault("failureDebugTtlSeconds", 86400)
        return environment
    except (ValueError, TypeError, KeyError, ArithmeticError, jsonschema.ValidationError):
        raise Error("INVALID_VERIFICATION_ENVIRONMENT", "Verification environment is invalid; values redacted", 2) from None


