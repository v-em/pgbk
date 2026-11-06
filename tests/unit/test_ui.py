import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from pgbk.catalog import Catalog
from pgbk.config import canonical, normalize
from pgbk.errors import Error
from pgbk.kube import Kubernetes
from pgbk.ui.adapters import Cluster, job_summary
from pgbk.ui.admission import validate
from pgbk.ui.auth import Auth, Vault
from pgbk.ui.common import Problem, next_occurrence, project
from pgbk.ui.index import MetadataIndex
from pgbk.ui.journal import Journal
from pgbk.ui.server import handler
from pgbk.ui.service import Service
from test_catalog import Held, MemoryS3, completed


class FakeVault:
    version = 1
    password = "test-only-password"
    unavailable = False

    def current(self):
        if self.unavailable:
            raise Problem("AUTHENTICATION_UNAVAILABLE")
        return "administrator", self.password, self.version


class FakeCluster:
    version = "template-1"
    missing = False
    unavailable = False
    lost = False

    def __init__(self):
        self.created = {}
        self.creates = 0

    def template(self, profile):
        if self.missing:
            raise Problem("TEMPLATE_MISSING", 409)
        return None, {}, self.version, {"globals": "enabled", "maintenanceDatabase": '<literal "db">'}

    def find_job(self, name):
        if self.unavailable:
            raise Problem("KUBERNETES_UNAVAILABLE")
        return self.created.get(name)

    def launch(self, profile, cron, name, request_id, version):
        self.creates += 1
        assert name not in self.created
        self.created[name] = {"metadata": {"name": name, "uid": "uid-1", "namespace": "backups",
            "creationTimestamp": "2026-11-06T10:00:00Z", "labels": {"pgbk.io/request": request_id[:63]},
            "annotations": {"pgbk.io/trigger": "manual", "pgbk.io/template-version": version}},
            "spec": {"template": {"metadata": {"labels": {"pgbk.io/installation": "example", "pgbk.io/profile": profile, "pgbk.io/operation": "backup"},
                "annotations": {"pgbk.io/trigger": "manual"}}}}}
        if self.lost:
            raise Error("JOB_CREATE_FAILED", "response lost")
        return {"namespace": "backups", "job": name, "jobUid": "uid-1", "status": "Pending"}

    def jobs(self, namespace):
        if self.unavailable:
            raise Problem("KUBERNETES_UNAVAILABLE")
        return list(self.created.values())


@pytest.fixture
def ui(values, tmp_path):
    c = normalize(values)
    cat = Catalog(MemoryS3(), c, "example-logical")
    completed(cat)
    journal = Journal(str(tmp_path / "journal.sqlite"))
    index = MetadataIndex({"example-logical": cat}, journal)
    index.refresh()
    config = {"installationId": "example", "namespace": "backups", "uiNamespace": "console", "serviceAccount": "pgbk-ui",
              "manualLaunch": True, "origin": "https://console.example.invalid", "profiles": {
                  "example-logical": {"sourceId": cat.scope["sourceId"], "kind": "logical"},
                  "other": {"sourceId": "other", "kind": "physical-full"}}}
    config["installationId"] = cat.scope["installationId"]
    cluster = FakeCluster()
    # Keep synthetic label scope equal to the actual fixture installation.
    cluster_installation = config["installationId"]
    original_launch = cluster.launch
    def launch(*args):
        result = original_launch(*args)
        cluster.created[args[2]]["spec"]["template"]["metadata"]["labels"]["pgbk.io/installation"] = cluster_installation
        return result
    cluster.launch = launch
    service = Service(config, cluster, index, journal)
    return SimpleNamespace(cat=cat, journal=journal, index=index, service=service, cluster=cluster, config=config)


def test_metadata_path_does_not_read_or_head_archives(ui):
    reads = []
    get = ui.cat.s3.get
    ui.cat.s3.get = lambda key: (reads.append(key), get(key))[1]
    ui.cat.s3.call = lambda *a, **kw: pytest.fail("metadata path must not HEAD archives")
    ui.index.refresh()
    detail = ui.index.detail("example-logical", "run-1")
    assert detail["manifestVerified"]
    ui.index.recovery("example-logical", "run-1", "en")
    assert reads and not any("/artifacts/" in key for key in reads)
    assert "source.invalid" not in json.dumps(detail)


@pytest.mark.parametrize("damage,code", [("hash", "MANIFEST_MISMATCH"), ("unmarked", "COPY_REMOVED_OR_UNAVAILABLE"), ("deletion", "COPY_REMOVED_OR_UNAVAILABLE")])
def test_publication_and_deletion_on_reopen(ui, damage, code):
    prefix = ui.cat.run_prefix("run-1")
    if damage == "hash":
        ui.cat.s3.put(prefix + "manifest.json", b"{}")
    elif damage == "unmarked":
        ui.cat.s3.delete(prefix + "complete.json")
    else:
        ui.cat.s3.put(prefix + "deletion.json", b"{}")
    with pytest.raises(Problem) as error:
        ui.index.detail("example-logical", "run-1")
    assert error.value.code == code
    ui.index.refresh()
    assert not ui.index.snapshot()["items"]
    assert ui.index.snapshot()["diagnostics"][0]["reasonCode"] == code


def rewrite_manifest(cat, edit):
    prefix = cat.run_prefix("run-1")
    manifest = json.loads(cat.s3.get(prefix + "manifest.json"))
    edit(manifest)
    data = canonical(manifest)
    cat.s3.put(prefix + "manifest.json", data)
    marker = json.loads(cat.s3.get(prefix + "complete.json"))
    marker["manifestSha256"] = hashlib.sha256(data).hexdigest()
    cat.s3.put(prefix + "complete.json", canonical(marker))


@pytest.mark.parametrize("language", ["en", "ru"])
def test_exact_stored_note_and_second_run(ui, language):
    filename = "BACKUP-RESTORE.md" if language == "en" else "BACKUP-RESTORE.ru.md"
    note = b"# Order from writer\n\n2. second.dump\n1. first.dump\n<script>text only</script>"
    ui.cat.s3.put(ui.cat.run_prefix("run-1") + "recovery/" + filename, note)
    def edit(m):
        item = next(i for i in m["recoveryInstructions"] if i["filename"] == filename)
        item.update(sha256=hashlib.sha256(note).hexdigest(), size=len(note))
    rewrite_manifest(ui.cat, edit)
    assert ui.index.recovery("example-logical", "run-1", language)["text"].encode() == note
    completed(ui.cat, run_id="run-2", filename="second-run.dump")
    assert "second-run.dump" in ui.index.recovery("example-logical", "run-2", language)["text"]


@pytest.mark.parametrize("case,code", [("legacy", "RECOVERY_NOT_STORED"), ("missing", "RECOVERY_INTEGRITY_ERROR"), ("hash", "RECOVERY_INTEGRITY_ERROR"), ("outage", "RECOVERY_TEMPORARILY_UNAVAILABLE")])
def test_recovery_failures_never_synthesize(ui, case, code):
    key = ui.cat.run_prefix("run-1") + "recovery/BACKUP-RESTORE.md"
    if case == "legacy":
        rewrite_manifest(ui.cat, lambda m: m.pop("recoveryInstructions"))
    elif case == "missing":
        ui.cat.s3.delete(key)
    elif case == "hash":
        ui.cat.s3.put(key, b"corrupt")
    else:
        get = ui.cat.s3.get
        def failed(k):
            if k == key:
                raise Problem("S3_UNAVAILABLE")
            return get(k)
        ui.cat.s3.get = failed
    with pytest.raises(Problem) as error:
        ui.index.recovery("example-logical", "run-1", "en")
    assert error.value.code == code
    assert ui.index.detail("example-logical", "run-1")["manifestVerified"]


def test_sort_pagination_cursor_scope_filters_and_time(ui):
    for i in range(6):
        completed(ui.cat, run_id=f"run-{i + 2}", at="2026-01-02T00:00:00Z")
    ui.index.refresh()
    page = ui.service.backups({"limit": "2"}, "admin")
    assert [b["runId"] for b in page["items"]] == ["run-7", "run-6"]
    next_page = ui.service.backups({"limit": "2", "cursor": page["nextCursor"]}, "admin")
    assert [b["runId"] for b in next_page["items"]] == ["run-5", "run-4"]
    for query, subject in [({"limit": "3", "cursor": page["nextCursor"]}, "admin"), ({"limit": "2", "cursor": page["nextCursor"]}, "other")]:
        with pytest.raises(Problem) as e:
            ui.service.backups(query, subject)
        assert e.value.code == "CURSOR_INVALID"
    assert not ui.service.backups({"source": "outside"}, "admin")["items"]
    assert not ui.service.backups({"from": "2026-02-01T00:00:00Z"}, "admin")["items"]


def test_two_profiles_keep_identical_run_ids_separate_across_pages(ui):
    other = Catalog(MemoryS3(), {"installationId": ui.config["installationId"], "profiles": {"other": {"target": "db"}}, "targets": {"db": {"sourceId": "other"}}}, "other")
    completed(other, run_id="run-1")
    ui.index.catalogs["other"] = other
    ui.index.refresh()
    first = ui.service.backups({"limit": "1"}, "admin")
    second = ui.service.backups({"limit": "1", "cursor": first["nextCursor"]}, "admin")
    assert first["items"][0]["runId"] == second["items"][0]["runId"] == "run-1"
    assert first["items"][0]["profileId"] != second["items"][0]["profileId"]
    assert ui.service.detail("other", "other", "run-1")["sourceId"] == "other"
    with pytest.raises(Problem) as error:
        ui.service.detail("other", ui.cat.scope["sourceId"], "run-1")
    assert error.value.status == 404
    del ui.config["profiles"]["other"]
    with pytest.raises(Problem) as error:
        ui.service.backups({"limit": "1", "cursor": first["nextCursor"]}, "admin")
    assert error.value.code == "CURSOR_INVALID"


def test_redaction_nested_projection_and_literal_names():
    value = project({"artifacts": [{"database": '<img src=x onerror="alert(1)">', "selection": {"sql": "select secret", "tables": ["public.table"], "unknown": "hidden"},
        "compression": {"method": "gzip", "level": 6}}], "sourceEndpoint": {"host": "private.internal"},
        "verificationPolicy": {"environment": {"credentialsSecretRef": "hidden", "assertions": ["select secret"]}}})
    result = json.dumps(value)
    assert "hidden" not in result and "select secret" not in result and "private.internal" not in result
    assert value["artifacts"][0]["database"].startswith("<img")
    assert value["artifacts"][0]["selection"]["tables"] == ["public.table"]


def test_cron_timezones_dst_and_sunday():
    clock = datetime.fromisoformat("2026-11-06T00:00:00+00:00")
    assert next_occurrence("0 2 * * *", "UTC", clock) != next_occurrence("0 2 * * *", "Europe/Moscow", clock)
    assert next_occurrence("30 2 * * *", "America/New_York", datetime.fromisoformat("2026-03-08T06:00:00+00:00")).startswith("2026-03-09T02:30:00-04:00")
    first = next_occurrence("30 1 * * *", "America/New_York", datetime.fromisoformat("2026-11-01T04:00:00+00:00"))
    second = next_occurrence("30 1 * * *", "America/New_York", datetime.fromisoformat(first))
    assert first.endswith("-04:00") and second.endswith("-05:00")
    assert datetime.fromisoformat(next_occurrence("0 0 * * 7", "UTC", clock)).weekday() == 6


def test_durable_idempotency_concurrency_restart_ttl_and_conflict(ui, tmp_path):
    key = ui.service.issue("example-logical", "administrator")["key"]
    def post(_):
        return ui.service.launch("example-logical", key, "administrator", "http-request")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post, range(8)))
    assert ui.cluster.creates == 1
    assert {r[0] for r in results} <= {200, 201, 202}
    reopened = Journal(str(tmp_path / "journal.sqlite"))
    ui.service.journal = reopened
    assert post(None)[0] == 200
    ui.cluster.created.clear()
    assert post(None)[1]["status"] == "Job removed"
    assert ui.cluster.creates == 1
    with pytest.raises(Problem) as error:
        ui.service.launch("other", key, "administrator", "request")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with reopened.transaction() as db:
        db.execute("UPDATE requests SET issued=?", (time.time() - 86401,))
    with pytest.raises(Problem) as error:
        post(None)
    assert error.value.code == "LAUNCH_TOKEN_EXPIRED"


def test_template_change_and_ambiguous_restart_do_not_create(ui):
    key = ui.service.issue("example-logical", "admin")["key"]
    ui.cluster.version = "changed"
    with pytest.raises(Problem) as error:
        ui.service.launch("example-logical", key, "admin", "request")
    assert error.value.code == "TEMPLATE_CHANGED"
    ui.cluster.version = "template-1"
    ui.journal.reserve(key, "admin", "example-logical", "template-1")
    status, result = ui.service.launch("example-logical", key, "admin", "request")
    assert status == 202 and result["status"] == "Outcome unknown"
    assert ui.cluster.creates == 0


def test_reused_job_name_cannot_hijack_request(ui):
    key = ui.service.issue("example-logical", "admin")["key"]
    _, record = ui.service.launch("example-logical", key, "admin", "request")
    ui.cluster.created[record["job"]]["metadata"]["uid"] = "different-uid"
    with pytest.raises(Problem) as error:
        ui.service.launch("example-logical", key, "admin", "request")
    assert error.value.code == "JOB_IDENTITY_CONFLICT"


def test_auth_rotation_expiry_logout_outage_and_no_password_storage(tmp_path):
    journal = Journal(str(tmp_path / "auth.sqlite"))
    vault = FakeVault()
    auth = Auth(journal, vault)
    with pytest.raises(Problem) as error:
        auth.login("administrator", "wrong", "peer", "request")
    assert error.value.code == "INVALID_CREDENTIALS"
    token, _ = auth.login("administrator", vault.password, "peer", "request")
    assert auth.authorize(token)["subject"] == "administrator"
    vault.version = 2
    vault.password = "rotated-password"
    with pytest.raises(Problem) as error:
        auth.authorize(token)
    assert error.value.code == "SESSION_ROTATED"
    token, _ = auth.login("administrator", vault.password, "peer", "request")
    vault.unavailable = True
    for operation in (lambda: auth.authorize(token), lambda: auth.login("administrator", "x", "peer", "req")):
        with pytest.raises(Problem) as error:
            operation()
        assert error.value.code == "AUTHENTICATION_UNAVAILABLE"
    vault.unavailable = False
    auth.logout(token)
    with pytest.raises(Problem):
        auth.authorize(token)
    token, _ = auth.login("administrator", vault.password, "peer", "request")
    with journal.transaction() as db:
        db.execute("UPDATE sessions SET expires=0")
    with pytest.raises(Problem):
        auth.authorize(token)
    dump = "\n".join(journal.db.iterdump())
    assert "rotated-password" not in dump and "test-only-password" not in dump and token not in dump


def test_rate_limited_login(ui):
    auth = Auth(ui.journal, FakeVault())
    for _ in range(10):
        with pytest.raises(Problem):
            auth.login("wrong", "wrong", "same-peer", "request")
    with pytest.raises(Problem) as error:
        auth.login("administrator", "test-only-password", "same-peer", "request")
    assert error.value.status == 429


@pytest.fixture
def http(ui):
    vault = FakeVault()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(ui.service, Auth(ui.journal, vault)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api/v1", vault
    server.shutdown()
    server.server_close()


def test_http_session_csrf_scope_extra_fields_and_deep_links(http, ui):
    base, vault = http
    for endpoint in ("/backups", "/profiles", "/schedules", "/jobs", "/profiles/example-logical/launch-token"):
        assert requests.get(base + endpoint).status_code == 401
    origin = {"Origin": ui.config["origin"]}
    response = requests.post(base + "/session", json={"username": "administrator", "password": vault.password}, headers=origin)
    assert response.status_code == 201
    assert all(flag in response.headers["Set-Cookie"] for flag in ("Secure", "HttpOnly", "SameSite=Strict"))
    cookie = response.headers["Set-Cookie"].split(";", 1)[0]
    headers = {**origin, "Cookie": cookie, "X-CSRF-Token": response.json()["csrfToken"]}
    assert requests.get(base + "/backups", headers=headers).status_code == 200
    assert requests.get(base + "/profiles/forbidden/sources/x/backups/run-1", headers=headers).status_code == 404
    assert requests.get(base + "/profiles/example-logical/sources/wrong/backups/run-1", headers=headers).status_code == 404
    launch = base + "/profiles/example-logical/backups"
    assert requests.post(launch, json={}, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    for field in ("host", "secret", "jobYaml", "cron", "args"):
        assert requests.post(launch, json={field: "injected"}, headers=headers).status_code == 400
    assert requests.post(launch, json={}, headers={**headers, "Origin": "https://evil.invalid"}).status_code == 403
    vault.unavailable = True
    response = requests.get(base + "/backups", headers=headers)
    assert response.status_code == 503 and response.json()["reasonCode"] == "AUTHENTICATION_UNAVAILABLE"
    vault.unavailable = False
    assert requests.delete(base + "/session", headers=headers).status_code == 200
    assert requests.get(base + "/backups", headers=headers).status_code == 401


@pytest.mark.parametrize("conditions,suspend,active,expected", [([{"type": "Complete", "status": "True"}], True, 1, "Complete"),
    ([{"type": "Failed", "status": "True"}], True, 1, "Failed"), ([], True, 1, "Suspended"), ([], False, 1, "Running"), ([], False, 0, "Pending")])
def test_job_state_precedence(conditions, suspend, active, expected):
    job = {"metadata": {"name": "job", "namespace": "backups", "uid": "uid"},
           "spec": {"suspend": suspend, "template": {"metadata": {}}}, "status": {"conditions": conditions, "active": active}}
    assert job_summary(job)["status"] == expected


def test_jobs_correlate_only_uid_scope_and_marker_and_isolate_outages(ui):
    key = ui.service.issue("example-logical", "admin")["key"]
    _, record = ui.service.launch("example-logical", key, "admin", "request")
    ui.cat.event("run-1", "terminal", {"phase": "SkippedBusy", "reasonCode": "SOURCE_BUSY", "namespace": "backups", "jobUid": "uid-1", "jobName": record["job"]})
    ui.index.refresh()
    item = ui.service.jobs({}, "admin")["items"][0]
    assert item["worker"]["phase"] == "SkippedBusy" and item["backup"]["runId"] == "run-1"
    ui.cat.s3.delete(ui.cat.run_prefix("run-1") + "complete.json")
    assert "backup" not in ui.service.jobs({}, "admin")["items"][0]
    ui.cluster.unavailable = True
    page = ui.service.jobs({}, "admin")
    assert page["partialErrors"] and page["items"][0]["status"] != "Job removed"
    assert ui.service.backups({}, "admin")["items"]  # Last successful S3 index remains independent.
    ui.cluster.unavailable = False
    ui.cluster.created.clear()
    assert ui.service.jobs({}, "admin")["items"][0]["status"] == "Job removed"


def test_cluster_paused_template_crosscheck_and_globals_default(ui):
    labels = {"pgbk.io/installation": ui.config["installationId"], "pgbk.io/profile": "example-logical"}
    pod = {"metadata": {"labels": {**labels, "pgbk.io/operation": "backup"}}, "spec": {"volumes": [{"name": "config", "configMap": {"name": "pinned"}}]}}
    cron = {"metadata": {"labels": labels, "uid": "cron-uid", "resourceVersion": "1"}, "spec": {"schedule": "0 2 * * *", "timeZone": "UTC", "suspend": True,
            "jobTemplate": {"metadata": {"labels": labels}, "spec": {"template": pod}}}}
    values = {"installationId": ui.config["installationId"], "profiles": {"example-logical": {"target": "db", "kind": "logical", "storage": "catalog", "logical": {"globals": "enabled"}}},
              "targets": {"db": {"sourceId": ui.cat.scope["sourceId"]}}, "storage": {"catalog": {"bucket": "bucket", "prefix": "postgres", "endpoint": "https://example.invalid"}}}
    ui.config.update(release="pgbk", configMapNames=["pinned"], storage=values["storage"])
    ui.config["profiles"] = {"example-logical": {**ui.config["profiles"]["example-logical"], "storage": "catalog"}}
    batch = SimpleNamespace(read_namespaced_cron_job=lambda *a, **kw: cron)
    core = SimpleNamespace(read_namespaced_config_map=lambda *a, **kw: SimpleNamespace(immutable=True, data={"config.json": json.dumps(values)}))
    cluster = Cluster(ui.config, SimpleNamespace(batch=batch, core=core))
    result = cluster.schedules()[0]
    assert result["status"] == "Paused" and result["nextOccurrence"] is None and result["launchAvailable"]
    assert result["globals"] == "enabled" and result["maintenanceDatabase"] == "postgres"
    values["targets"]["db"]["maintenanceDatabase"] = '<literal "db">'
    assert cluster.schedules()[0]["maintenanceDatabase"] == '<literal "db">'
    values["targets"]["db"]["sourceId"] = "wrong"
    assert cluster.schedules()[0]["reasonCode"] == "PROFILE_CONFIGURATION_MISMATCH"


@pytest.fixture
def admission_case(ui):
    labels = {"pgbk.io/installation": ui.config["installationId"], "pgbk.io/profile": "example-logical"}
    spec = {"backoffLimit": 0, "template": {"metadata": {"labels": {**labels, "pgbk.io/operation": "backup"}, "annotations": {"pgbk.io/trigger": "schedule"}},
        "spec": {"restartPolicy": "Never", "serviceAccountName": "worker", "containers": [{"name": "worker", "image": "worker@sha256:abc", "command": ["pgbk"], "args": ["worker", "backup"], "env": [{"name": "CONFIG", "value": "pinned"}]}],
                 "volumes": [{"name": "config", "configMap": {"name": "pinned"}}]}}}
    cron = {"spec": {"jobTemplate": {"metadata": {"labels": labels}, "spec": spec}}}
    ui.cluster.template = lambda profile: (None, cron, "template-1", {})
    key = ui.service.issue("example-logical", "admin")["key"]
    row, _ = ui.journal.reserve(key, "admin", "example-logical", "template-1")
    job = {"metadata": {"name": row["name"], "labels": {**labels, "pgbk.io/request": row["id"][:63]},
                       "annotations": {"pgbk.io/trigger": "manual", "pgbk.io/template-version": "template-1"}}, "spec": copy.deepcopy(spec)}
    job["spec"]["template"]["metadata"]["annotations"]["pgbk.io/trigger"] = "manual"
    review = {"request": {"uid": "review-uid", "userInfo": {"username": "system:serviceaccount:console:pgbk-ui"},
        "operation": "CREATE", "resource": {"resource": "jobs"}, "namespace": "backups", "object": job}}
    return ui, job, review


@pytest.mark.parametrize("field", ["image", "command", "args", "env", "volumes", "serviceAccountName", "profile", "identity", "annotation", "unknownSpec"])
def test_admission_exact_template_and_injection(admission_case, field):
    ui, job, review = admission_case
    assert validate(ui.service, review)["response"]["allowed"]
    pod = job["spec"]["template"]["spec"]
    if field in ("image", "command", "args", "env"):
        pod["containers"][0][field] = "injected" if field == "image" else []
    elif field in ("volumes", "serviceAccountName"):
        pod[field] = [] if field == "volumes" else "injected"
    elif field == "profile":
        job["metadata"]["labels"]["pgbk.io/profile"] = "forbidden"
    elif field == "identity":
        review["request"]["userInfo"]["username"] = "another-account"
    elif field == "annotation":
        job["metadata"]["annotations"]["extra"] = "injected"
    else:
        job["spec"]["newField"] = "injected"
    assert not validate(ui.service, review)["response"]["allowed"]


def test_admission_accepts_only_expected_job_controller_defaults(admission_case):
    ui, job, review = admission_case
    job["spec"].update(parallelism=1, completions=1, manualSelector=False, completionMode="NonIndexed", suspend=False)
    job["spec"]["template"]["metadata"]["labels"].update({"controller-uid": "uid", "batch.kubernetes.io/controller-uid": "uid", "job-name": job["metadata"]["name"], "batch.kubernetes.io/job-name": job["metadata"]["name"]})
    job["spec"]["selector"] = {"matchLabels": {"batch.kubernetes.io/controller-uid": "uid"}}
    assert validate(ui.service, review)["response"]["allowed"]
    job["spec"]["selector"]["matchLabels"]["extra"] = "not-allowed"
    assert not validate(ui.service, review)["response"]["allowed"]


def test_s3_outage_retains_last_successful_index_and_other_profile(ui):
    initial = ui.index.snapshot()["refreshTime"]
    ui.cat.s3.keys = lambda prefix: (_ for _ in ()).throw(Problem("S3_ACCESS_DENIED"))
    ui.index.refresh()
    snapshot = ui.index.snapshot()
    assert snapshot["partial"] and snapshot["items"] and snapshot["refreshTime"] == initial
    assert snapshot["partialErrors"][0]["reasonCode"] == "S3_ACCESS_DENIED"


def test_lost_create_response_is_reconciled(ui):
    # Explicit fake response loss after persisting the Job.
    launch = ui.cluster.launch
    def lost(*args):
        launch(*args)
        raise Error("JOB_CREATE_FAILED", "lost response")
    ui.cluster.launch = lost
    key = ui.service.issue("example-logical", "admin")["key"]
    status, record = ui.service.launch("example-logical", key, "admin", "request")
    assert status == 200 and record["jobUid"] == "uid-1"
    assert ui.service.launch("example-logical", key, "admin", "request")[1]["jobUid"] == "uid-1"
    assert ui.cluster.creates == 1


def test_view_only_denies_tokens_and_launch(ui):
    ui.config["manualLaunch"] = False
    for action in (lambda: ui.service.issue("example-logical", "admin"), lambda: ui.service.launch("example-logical", "key", "admin", "req")):
        with pytest.raises(Problem) as error:
            action()
        assert error.value.code == "LAUNCH_FORBIDDEN" and error.value.status == 403


def test_physical_metadata_and_bilingual_notes_without_archive_reads(ui):
    manifest = {"schemaVersion": 1, **ui.cat.scope, "runId": "physical-1", "kind": "physical-full", "serverMajor": 18,
        "architecture": "x86_64", "completedAt": "2026-11-06T10:00:00Z", "source": {"tablespaceOids": []}, "tools": {},
        "sourceEndpoint": {"host": "private.internal", "port": 5432}, "artifacts": [
            {"fileId": f"file-{i}", "filename": filename, "format": "postgres-tar", "sha256": "a" * 64, "size": 1}
            for i, filename in enumerate(["base.tar", "pg_wal.tar", "backup_manifest"])]}
    ui.cat.publish(manifest, Held())
    assert ui.index.detail("example-logical", "physical-1")["kind"] == "physical-full"
    for language, filename in [("en", "BACKUP-RESTORE.md"), ("ru", "BACKUP-RESTORE.ru.md")]:
        note = ui.index.recovery("example-logical", "physical-1", language)
        assert note["text"].encode() == ui.cat.s3.get(ui.cat.run_prefix("physical-1") + "recovery/" + filename)


@pytest.mark.parametrize("reason,phase", [("ACCESS_DENIED", "Dumping"), ("TOOL_FAILED", "Dumping"), ("SOURCE_BUSY", "Preflight")])
def test_persisted_failure_diagnostics_without_false_copy_link(ui, reason, phase):
    ui.cat.event("failed-run", "terminal", {"phase": "SkippedBusy" if reason == "SOURCE_BUSY" else "Failed", "failedPhase": phase,
        "reasonCode": reason, "namespace": "backups", "jobUid": "failed-uid", "jobName": "failed-job"})
    ui.index.refresh()
    job = ui.service.job("backups", "failed-uid", "admin")
    assert job["worker"]["reasonCode"] == reason and job["worker"]["failedPhase"] == phase
    assert "backup" not in job and "toolExitCode" not in job["worker"]
