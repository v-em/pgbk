import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from pgbk.catalog import Catalog, retention_candidates
from pgbk.config import canonical, normalize
from pgbk.errors import Error
from pgbk.verification import coverage, snapshot, state, validate_environment
from test_catalog import MemoryS3, Held, completed


CLOCK = datetime(2026, 11, 3, tzinfo=timezone.utc)


def environment(values):
    return {"namespace": "checks", "serviceAccountName": "checks", "concurrencyKey": "checks",
            "image": "example/worker@sha256:" + "0" * 64,
            "resources": values["profiles"]["example-logical"]["execution"]["resources"],
            "scratch": {"type": "pvc", "size": "1Gi", "storageClassName": "standard"},
            "timeoutSeconds": 600, "credentialsSecretRef": "check-server", "clientCredentialsSecretRef": "check-client",
            "assertions": ["SELECT true"]}


@pytest.fixture
def configured(values):
    values["coordinationNamespace"] = "backups"
    values["verificationEnvironments"] = {"pg18": environment(values)}
    values["profiles"]["example-logical"]["verification"] = {
        "enabled": True, "environmentRef": "pg18", "required": True, "dueWithinSeconds": 3600}
    return values


def manifest(configured, at=None):
    at = at or CLOCK.isoformat()
    c = normalize(configured)
    return {"runId": "copy", "completedAt": at, "artifacts": [],
            "verificationPolicy": snapshot(c, "example-logical", at)}


def report(index, passed=True):
    return {"runId": "copy", "verificationId": f"attempt-{index}", "sequence": index,
            "finishedAt": (CLOCK + timedelta(minutes=index)).isoformat(), "status": "passed" if passed else "failed",
            **({} if passed else {"reasonCode": "VERIFICATION_ASSERTION_FAILED"})}


def test_capture_is_independent_of_edits(configured):
    m = manifest(configured)
    configured["profiles"]["example-logical"]["verification"].update(required=False, dueWithinSeconds=10)
    configured["verificationEnvironments"]["pg18"].update(timeoutSeconds=99, credentialsSecretRef="rotated-reference")
    policy = m["verificationPolicy"]
    assert policy["required"] and policy["dueAt"] == (CLOCK + timedelta(hours=1)).isoformat()
    assert policy["environment"]["timeoutSeconds"] == 600
    assert policy["environment"]["credentialsSecretRef"] == "check-server"
    assert "password" not in canonical(policy).decode()


@pytest.mark.parametrize("reports,minutes,expected,compliant,protected", [
    ([], 0, "successful", False, True),
    ([], 60, "verification-failed", False, False),
    ([report(1, False)], 0, "verification-failed", False, False),
    ([report(1, False), report(2)], 90, "verification-successful", True, False),
    ([report(1), report(2, False)], 90, "verification-failed", False, False),
    ([report(1)], 500, "verification-successful", True, False),
])
def test_status_deadline_and_retry(configured, reports, minutes, expected, compliant, protected):
    m = manifest(configured)
    original = canonical(m)
    s = state(m, reports, active={"verificationId": "retry"}, clock=CLOCK + timedelta(minutes=minutes))
    assert (s["status"], s["profileCompliant"], s["waitingProtected"]) == (expected, compliant, protected)
    assert s["operationPinned"]
    assert canonical(m) == original


def test_optional_failure_does_not_invalidate_copy(configured):
    configured["profiles"]["example-logical"]["verification"]["required"] = False
    s = state(manifest(configured), [report(1, False)], clock=CLOCK + timedelta(days=100))
    assert s["status"] == "successful" and s["profileCompliant"]
    assert s["restoreVerification"]["status"] == "failed" and not s["waitingProtected"]


def test_latest_completed_attempt_not_active_controls_status(configured):
    s = state(manifest(configured), [report(2, False), report(1)], active={"sequence": 3}, clock=CLOCK)
    assert s["status"] == "verification-failed"
    assert [r["sequence"] for r in s["restoreVerification"]["attempts"]] == [1, 2]


@pytest.mark.parametrize("change", [
    lambda c: c["profiles"]["example-logical"]["verification"].pop("dueWithinSeconds"),
    lambda c: c["profiles"]["example-logical"]["verification"].update(dueWithinSeconds=0),
    lambda c: c["profiles"]["example-logical"]["verification"].update(environmentRef="absent"),
    lambda c: c["profiles"]["example-logical"]["verification"].update(enabled=False),
    lambda c: c["profiles"]["example-logical"]["verification"].update(schedule={"enabled": True, "cron": "* * * * *"}),
    lambda c: c["verificationEnvironments"]["pg18"].update(image="unpinned:latest"),
    lambda c: c["verificationEnvironments"]["pg18"].update(maxConcurrent=0),
    lambda c: c["verificationEnvironments"]["pg18"].update(assertions=["DELETE FROM items"]),
    lambda c: c["verificationEnvironments"]["pg18"].pop("serviceAccountName"),
])
def test_invalid_obligations_rejected_before_jobs(configured, change):
    change(configured)
    with pytest.raises(Error):
        normalize(configured)


def test_manual_profile_needs_no_schedule(configured):
    c = normalize(configured)
    assert c["profiles"]["example-logical"]["verification"]["namespace"] == "checks"
    assert c["verificationReconciliation"]["cron"] == "*/5 * * * *"


@pytest.mark.parametrize("provider,host", [("yandex-managed-postgresql", "alias.invalid"), ("generic", "c-example.mdb.yandexcloud.net.")])
def test_yandex_physical_refused_without_connecting(values, provider, host):
    values["targets"]["example"].update(provider=provider, host=host, physicalEndpoint="direct-primary", expectedSystemIdentifier="123")
    p = values["profiles"]["example-logical"]
    p["kind"] = "physical-full"
    p.pop("logical")
    with pytest.raises(Error) as e:
        normalize(values)
    assert e.value.code == "YANDEX_PHYSICAL_UNSUPPORTED"


def test_dual_minima_preserve_older_compliant_copies():
    runs = [{"runId": f"copy-{i}", "completedAt": (CLOCK - timedelta(days=i + 40)).isoformat(),
             "profileCompliant": i >= 3} for i in range(9)]
    candidates = retention_candidates(runs, 30, 3, CLOCK)
    assert {r["runId"] for r in candidates} == {"copy-6", "copy-7", "copy-8"}
    # A verification performed today cannot reorder the retention minima.
    runs[-1]["verifiedAt"] = CLOCK.isoformat()
    assert retention_candidates(runs, 30, 3, CLOCK) == candidates


@pytest.mark.parametrize("verified", [0, 1, 2, 3])
def test_fewer_than_minimum_and_all_failed(verified):
    runs = [{"runId": f"copy-{i}", "completedAt": (CLOCK - timedelta(days=i + 40)).isoformat(),
             "profileCompliant": i >= 6 - verified} for i in range(6)]
    deleted = {r["runId"] for r in retention_candidates(runs, 30, 3, CLOCK)}
    assert deleted == {f"copy-{i}" for i in range(3, 6 - verified)}


def test_waiting_and_operation_protection_are_independent():
    runs = [{"runId": f"copy-{i}", "completedAt": (CLOCK - timedelta(days=40)).isoformat(),
             "profileCompliant": False, "waitingProtected": i == 0, "operationPinned": i == 1} for i in range(5)]
    assert {r["runId"] for r in retention_candidates(runs, 30, 1, CLOCK)} == {"copy-2", "copy-3"}
    runs[0]["waitingProtected"] = False
    assert "copy-0" in {r["runId"] for r in retention_candidates(runs, 30, 1, CLOCK)}


def test_attempt_reports_and_duplicate_pin(configured):
    c = normalize(configured)
    catalog = Catalog(MemoryS3(), c, "example-logical")
    m = completed(catalog)
    env = c["verificationEnvironments"]["pg18"]
    original = {k: v for k, v in catalog.s3.objects.items() if "/runs/" in k}
    catalog.start_verification(m["runId"], "first", env, {"jobName": "one"})
    with pytest.raises(Error) as e:
        catalog.start_verification(m["runId"], "second", env)
    assert e.value.exit_code == 4 and e.value.details["activeAttemptId"] == "first"
    _, active = catalog.verification_records(m["runId"])
    catalog.finish_verification({**active, "finishedAt": CLOCK.isoformat(), "status": "failed", "reasonCode": "TEST_FAILURE"})
    catalog.start_verification(m["runId"], "second", env)
    assert catalog.view(m["runId"])["restoreVerification"]["latestAttempt"]["verificationId"] == "first"
    _, active = catalog.verification_records(m["runId"])
    catalog.finish_verification({**active, "finishedAt": CLOCK.isoformat(), "status": "passed"})
    assert len(catalog.verification_records(m["runId"])[0]) == 2
    assert original == {k: v for k, v in catalog.s3.objects.items() if "/runs/" in k}


def test_deleting_copy_cannot_be_pinned(configured):
    c = normalize(configured)
    catalog = Catalog(MemoryS3(), c, "example-logical")
    m = completed(catalog)
    catalog.s3.put(catalog.run_prefix(m["runId"]) + "deletion.json", b"{}")
    with pytest.raises(Error) as e:
        catalog.start_verification(m["runId"], "attempt", c["verificationEnvironments"]["pg18"])
    assert e.value.code == "BACKUP_DELETING"


def test_reconcile_overdue_then_late_pass(configured):
    c = normalize(configured)
    catalog = Catalog(MemoryS3(), c, "example-logical")
    m = completed(catalog)
    m["verificationPolicy"] = snapshot(c, "example-logical", m["completedAt"])
    # Fixture publication only; product never modifies a published copy.
    catalog.s3.delete(catalog.run_prefix(m["runId"]) + "complete.json")
    catalog.publish(m, Held())
    assert catalog.view(m["runId"], CLOCK)["verificationOverdue"]
    assert catalog.reconcile_verification(clock=CLOCK)["overdueRunIds"] == [m["runId"]]
    catalog.start_verification(m["runId"], "late", c["verificationEnvironments"]["pg18"])
    _, active = catalog.verification_records(m["runId"])
    catalog.finish_verification({**active, "finishedAt": CLOCK.isoformat(), "status": "passed"})
    assert catalog.reconcile_verification(clock=CLOCK)["overdueRunIds"] == []
    assert catalog.view(m["runId"], CLOCK)["status"] == "verification-successful"
    assert catalog.describe(m["runId"])["verificationPolicy"]["dueAt"] == m["verificationPolicy"]["dueAt"]


def test_monitoring_uses_data_age_and_unknown_is_labelled(configured):
    old = manifest(configured, (CLOCK - timedelta(days=10)).isoformat())
    old.update(state(old, [report(1)], clock=CLOCK))
    old["runId"] = "old-verified-today"
    new = manifest(configured)
    new.update(state(new, clock=CLOCK))
    summary = coverage([old, new])
    assert summary["latestIntegrityConfirmed"]["runId"] == "copy"
    assert summary["latestProfileCompliant"]["runId"] == "old-verified-today"
    assert summary["latestProfileCompliant"]["timeBasis"] == "completion-time-fallback"
    assert summary["latestProfileCompliant"]["dataTime"] == old["completedAt"]


def test_multidatabase_snapshot_uses_earliest_and_does_not_fill_unknown(configured):
    m = manifest(configured)
    m["artifacts"] = [{"database": "a", "snapshotAt": CLOCK.isoformat()},
                      {"database": "b", "snapshotAt": (CLOCK - timedelta(minutes=10)).isoformat()}]
    assert state(m, clock=CLOCK)["snapshotAt"] == m["artifacts"][1]["snapshotAt"]
    m["artifacts"][1]["snapshotAt"] = None
    assert state(m, clock=CLOCK)["snapshotAt"] is None


def test_untrusted_environment_has_strict_schema(values):
    env = environment(values)
    env["password"] = "secret-canary"
    with pytest.raises(Error) as e:
        validate_environment(json.dumps(env))
    assert "secret-canary" not in str(e.value)


def test_fenced_recovery_requires_exact_attempt_and_explicit_acknowledgement(configured):
    c = normalize(configured)
    cat = Catalog(MemoryS3(), c, "example-logical")
    m = completed(cat)
    cat.start_verification(m["runId"], "orphan", c["verificationEnvironments"]["pg18"])
    class Kube:
        released = False
        def release_verification_slot(self, active):
            self.released = True
    kube = Kube()
    with pytest.raises(Error, match="fencing"):
        cat.recover_verification(m["runId"], "orphan", kube)
    with pytest.raises(Error, match="no longer active"):
        cat.recover_verification(m["runId"], "different-attempt", kube, fenced=True)
    assert not kube.released
    cat.recover_verification(m["runId"], "orphan", kube, fenced=True)
    assert kube.released and cat.verification_records(m["runId"])[1] is None
    assert cat.verification_records(m["runId"])[0][0]["reasonCode"] == "VERIFICATION_WORKER_FENCED"


def test_report_saved_before_pin_delete_is_recovered_without_overwrite(configured):
    c = normalize(configured)
    cat = Catalog(MemoryS3(), c, "example-logical")
    m = completed(cat)
    _, active = cat.start_verification(m["runId"], "passed", c["verificationEnvironments"]["pg18"])
    pin = cat.s3.get(cat.root + "verification/run-1/active.json")
    report = {**active, "finishedAt": CLOCK.isoformat(), "status": "passed"}
    cat.finish_verification(report)
    cat.s3.put(cat.root + "verification/run-1/active.json", pin)
    class Kube:
        def terminal_verification(self, active):
            return "VERIFICATION_REPORT_MISSING"
        def release_verification_slot(self, active):
            pass
    cat.reconcile_verification(kube=Kube())
    reports, active = cat.verification_records(m["runId"])
    assert reports == [report] and active is None


def test_report_failure_keeps_operation_pin(configured):
    c = normalize(configured)
    cat = Catalog(MemoryS3(), c, "example-logical")
    m = completed(cat)
    _, active = cat.start_verification(m["runId"], "failure", c["verificationEnvironments"]["pg18"])
    original = cat.s3.put
    def fail(key, data):
        if key.endswith("/failure.json"):
            raise Error("S3_ACCESS_DENIED", "Denied", 8)
        return original(key, data)
    cat.s3.put = fail
    with pytest.raises(Error):
        cat.finish_verification({**active, "finishedAt": CLOCK.isoformat(), "status": "failed"})
    assert cat.verification_records(m["runId"])[1]["verificationId"] == "failure"


def test_shared_slot_capacity_is_bounded(values):
    from pgbk.verification import acquire_slot
    occupied = set()
    class Lease:
        def __init__(self, name): self.name = name
        def __enter__(self):
            if self.name in occupied: raise Error("SOURCE_BUSY", "Busy", 4)
            occupied.add(self.name)
        def __exit__(self, *args): occupied.remove(self.name)
    class Kube:
        def lease(self, name, control): return Lease(name)
    env = environment(values)
    first = acquire_slot(Kube(), env, None)
    with pytest.raises(Error) as e: acquire_slot(Kube(), env, None)
    assert e.value.code == "VERIFICATION_CAPACITY_BUSY"
    first.__exit__()
    assert acquire_slot(Kube(), env, None).name == first.name


def test_cli_requires_explicit_copy_and_verification_profile():
    from pgbk.cli import parser
    with pytest.raises(SystemExit) as e:
        parser().parse_args(["verify", "--profile", "logical", "--run-id", "copy"])
    assert e.value.code == 2


def test_cli_verification_profile_has_contract_name():
    from pgbk.cli import parser
    args = parser().parse_args(["verify", "--profile", "logical", "--run-id", "copy", "--verification-profile", "isolated-pg18"])
    assert args.environment == "isolated-pg18"
