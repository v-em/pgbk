"""Real PostgreSQL 18 Jobs, S3 and coordination; no cloud accounts are used.

The host's S3 transport can use a forwarded endpoint. The configured repository
identity remains identical to the one used by Jobs. An ARM node requires an
explicit test-only emulation flag; production chart platform guards stay intact.
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import yaml
from botocore.config import Config
from kubernetes import client, config as kube_config

from pgbk import cli
from pgbk import __version__
from pgbk.catalog import Catalog
from pgbk.config import StrictLoader, canonical, normalize
from pgbk.errors import Error
from pgbk.kube import Kubernetes
from pgbk.process import Control
from pgbk.runner import repository_lease
from pgbk.storage import S3
from pgbk.verification import snapshot


def source_digest(root):
    h = hashlib.sha256()
    for path in sorted(Path(root).glob("*")):
        if path.suffix in [".py", ".json", ".txt"]:
            h.update(path.name.encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="pgbk-local")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--chart", default="charts/pgbk", help="Chart directory or packaged .tgz")
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--s3-endpoint", required=True)
    parser.add_argument("--s3-host-endpoint")
    parser.add_argument("--allow-amd64-emulation", action="store_true")
    parser.add_argument("--output", default="tests/reports/spec13-minikube18.json")
    parser.add_argument("--logs", required=True)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    kube_config.load_kube_config(context=args.context)
    core, batch = client.CoreV1Api(), client.BatchV1Api()
    nodes = core.list_node().items
    emulation = any(n.status.node_info.architecture != "amd64" for n in nodes)
    assert not emulation or args.allow_amd64_emulation, "Use an amd64 node or explicitly acknowledge test-only emulation"
    namespace = "pgbk13-" + uuid.uuid4().hex[:8]
    verification_namespace = namespace + "-verify"
    logs = Path(args.logs).absolute()
    logs.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pgbk13-"))
    os.environ["PGBK_SECRET_ROOT"] = str(work / "secrets")
    cases, jobs = [], []
    started = time.monotonic()
    result = {"schemaVersion": 1, "pgbkVersion": __version__, "chart": args.chart,
              "chartSha256": hashlib.sha256(Path(args.chart).read_bytes()).hexdigest() if Path(args.chart).is_file() else None,
              "specificationVersion": "1.3", "deliveryStage": "local PostgreSQL 18 subcases",
              "status": "failed", "context": args.context, "namespaces": [namespace, verification_namespace],
              "workerImage": args.worker_image, "sourceSha256": source_digest("src/pgbk"),
              "kubernetes": nodes[0].status.node_info.kubelet_version, "nodeArchitecture": nodes[0].status.node_info.architecture,
              "workerArchitecture": "amd64", "emulation": emulation, "s3": "local Moto; no Yandex Cloud",
              "cases": cases, "jobs": jobs, "cloudCertification": "not-run (excluded by request)",
              "fullAcceptance": "not-run; local subcases do not certify all procedures"}

    def passed(name, **details):
        cases.append({"case": name, "status": "passed", **details})
        print(name + ": passed", flush=True)

    def kubectl(*command, input=None):
        return subprocess.check_output(["kubectl", "--context", args.context, "-n", namespace, *command], input=input, text=True)

    def sql(statement, database="postgres"):
        return kubectl("exec", "-i", "deployment/source", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database, input=statement)

    def routed_s3(storage, control):
        s3 = S3(storage, control)
        if args.s3_host_endpoint:
            from pgbk.credentials import secret
            ref = storage["credentialsSecretRef"]
            s3.client = boto3.client("s3", endpoint_url=args.s3_host_endpoint, region_name=storage["region"],
                aws_access_key_id=secret(ref, "accessKeyId"), aws_secret_access_key=secret(ref, "secretAccessKey"),
                config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 0}, s3={"addressing_style": "path"}))
        return s3

    cli.S3 = routed_s3  # Real S3 service via the host transport; no catalog/API stubs.

    values_file = work / "values.yaml"
    def command(*argv):
        parsed = cli.parser().parse_args(["--config", str(values_file), "--namespace", namespace, "--context", args.context,
                                         "--scratch", str(work / "scratch"), "--output", "json", *argv])
        return cli.execute(parsed)

    def catalog(profile):
        c = normalize(yaml.safe_load(values_file.read_text()))
        p = c["profiles"][profile]
        return Catalog(routed_s3(c["storage"][p["storage"]], Control(1200)), c, profile)

    def wait(job, expected=True):
        jobs.append(job)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            current = batch.read_namespaced_job(job["job"], job["namespace"])
            conditions = current.status.conditions or []
            done = next((c.type for c in conditions if c.status == "True" and c.type in ["Complete", "Failed"]), None)
            if done:
                pods = core.list_namespaced_pod(job["namespace"], label_selector="batch.kubernetes.io/controller-uid=" + current.metadata.uid).items
                text = "\n".join(core.read_namespaced_pod_log(p.metadata.name, job["namespace"]) for p in pods)
                (logs / (job["job"] + ".log")).write_text(text)
                assert (done == "Complete") == expected, f"Unexpected Job outcome: {job['job']}; see {logs}"
                payload = None
                decoder = json.JSONDecoder()
                for line in range(len(text)):
                    if text[line] == "{" and (line == 0 or text[line-1] == "\n"):
                        try:
                            candidate, _ = decoder.raw_decode(text[line:])
                            if "manifest" in candidate or "verificationId" in candidate or "deletedRunIds" in candidate or "overdueRunIds" in candidate or candidate.get("status") == "downloaded":
                                payload = candidate
                        except ValueError:
                            pass
                assert payload, f"Job produced no final report: {job['job']}"
                return payload
            time.sleep(1)
        raise AssertionError("Job exceeded the fixture timeout: " + job["job"])

    def backup(profile):
        report = wait(command("backup", "run", "--profile", profile))
        assert report["status"] == "successful" and report["artifactStatus"] == "Completed"
        return report

    def verify(profile, run_id, environment=None, expected=True, wait_for_job=True):
        argv = ["verify", "--profile", profile, "--run-id", run_id, "--verification-profile",
                environment or values["profiles"][profile]["verification"]["environmentRef"]]
        job = command(*argv)
        return wait(job, expected) if wait_for_job else job

    try:
        for ns in [namespace, verification_namespace]:
            core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))
        password = uuid.uuid4().hex
        secrets = {"source": {"username": "postgres", "password": password},
                   "backup": {"username": "backup", "password": password},
                   "verify": {"username": "postgres", "password": password},
                   "s3": {"accessKeyId": uuid.uuid4().hex, "secretAccessKey": uuid.uuid4().hex}}
        for ref, fields in secrets.items():
            directory = work / "secrets" / ref
            directory.mkdir(parents=True, mode=0o700)
            for key, value in fields.items():
                (directory / key).write_text(value)
                (directory / key).chmod(0o600)
            for ns in [namespace, verification_namespace]:
                core.create_namespaced_secret(ns, client.V1Secret(metadata=client.V1ObjectMeta(name=ref), string_data=fields))
        s3_storage = {"bucket": namespace, "prefix": "postgres", "endpoint": args.s3_endpoint,
                      "region": "us-east-1", "credentialsSecretRef": "s3", "tls": {"verify": True}, "addressingStyle": "path"}
        routed_s3(s3_storage, Control()).client.create_bucket(Bucket=namespace)
        source = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "source", "namespace": namespace},
                  "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "source"}}, "template": {
                      "metadata": {"labels": {"app": "source"}}, "spec": {
                          "securityContext": {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
                          "containers": [{"name": "postgres", "image": args.worker_image, "imagePullPolicy": "IfNotPresent",
                              "command": ["docker-entrypoint.sh"], "args": ["postgres"],
                              "env": [{"name": "PGDATA", "value": "/data/pgdata"}, {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "source", "key": "password"}}}],
                              "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                              "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "postgres"]}, "periodSeconds": 2}}],
                          "volumes": [{"name": "data", "emptyDir": {}}]}}}}
        kubectl("apply", "-f", "-", input=yaml.safe_dump(source))
        core.create_namespaced_service(namespace, client.V1Service(metadata=client.V1ObjectMeta(name="source"),
            spec=client.V1ServiceSpec(selector={"app": "source"}, ports=[client.V1ServicePort(port=5432)])))
        kubectl("rollout", "status", "deployment/source", "--timeout=180s")
        # Fixture provisioning only: product workers never edit source authentication.
        kubectl("exec", "deployment/source", "--", "python3", "-c",
                "from pathlib import Path;p=Path('/data/pgdata/pg_hba.conf');p.write_text(p.read_text()+'\\nhost replication postgres all scram-sha-256\\n')")
        sql("SELECT pg_reload_conf()")
        sql("CREATE DATABASE fixture;\nCREATE ROLE backup LOGIN PASSWORD '" + password + "';\n")
        sql("""CREATE SCHEMA reporting;
CREATE TABLE public.items(id serial PRIMARY KEY, value text NOT NULL CHECK(length(value)>0));
INSERT INTO items(value) SELECT md5(i::text) FROM generate_series(1,1000) i;
CREATE TABLE public.secret(id integer);
INSERT INTO public.secret VALUES(99);
CREATE TABLE reporting.parent(id integer, m integer) PARTITION BY RANGE(m);
CREATE TABLE reporting.jan PARTITION OF reporting.parent FOR VALUES FROM(1) TO(2);
CREATE TABLE reporting.feb PARTITION OF reporting.parent FOR VALUES FROM(2) TO(3);
INSERT INTO reporting.parent VALUES(42,1),(43,2);
SELECT lo_from_bytea(0,decode('cafebabe','hex'));
GRANT USAGE ON SCHEMA public,reporting TO backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public,reporting TO backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public,reporting TO backup;
DO $$DECLARE r record; BEGIN FOR r IN SELECT oid FROM pg_largeobject_metadata LOOP
EXECUTE format('GRANT SELECT ON LARGE OBJECT %s TO backup',r.oid); END LOOP; END$$;
""", "fixture")
        system_id = sql("SELECT system_identifier FROM pg_control_system()").strip()
        resources = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}}
        env = {"namespace": verification_namespace, "serviceAccountName": "verifier", "concurrencyKey": namespace,
               "maxConcurrent": 1, "image": args.worker_image, "resources": resources,
               "scratch": {"type": "pvc", "size": "2Gi", "storageClassName": "standard"}, "timeoutSeconds": 900,
               "failureDebugTtlSeconds": 300, "credentialsSecretRef": "verify", "clientCredentialsSecretRef": "verify",
               "assertions": ["SELECT NOT pg_is_in_recovery()"]}
        logical_env = {**copy.deepcopy(env), "assertions": ["SELECT count(*)=1000 FROM public.items"]}
        values = {"installationId": namespace, "coordinationNamespace": namespace, "rbac": {"create": True},
                  "coordination": {"createLeases": True}, "images": {"18": args.worker_image}, "storage": {"archive": s3_storage},
                  "targets": {"fixture": {"host": "source." + namespace + ".svc.cluster.local", "sourceId": "fixture", "concurrencyKey": namespace,
                      "serverMajor": 18, "credentialsSecretRef": "backup", "tls": {"mode": "disable"}},
                      "physical": {"host": "source." + namespace + ".svc.cluster.local", "sourceId": "fixture", "concurrencyKey": namespace,
                      "serverMajor": 18, "credentialsSecretRef": "source", "tls": {"mode": "disable"},
                      "physicalEndpoint": "direct-primary", "expectedSystemIdentifier": system_id}},
                  "verificationEnvironments": {"pg18": logical_env, "physical": env,
                      "fail": {**copy.deepcopy(env), "assertions": ["SELECT false"]},
                      "slow": {**copy.deepcopy(logical_env), "assertions": ["SELECT true FROM pg_sleep(12)", "SELECT count(*)=1000 FROM public.items"]}},
                  "profiles": {}}
        for name in ["logical", "direct", "directory", "filtered", "optional", "overdue", "retention", "physical"]:
            p = {"target": "physical" if name == "physical" else "fixture", "kind": "physical-full" if name == "physical" else "logical",
                 "storage": "archive", "serviceAccounts": {"create": True}, "execution": {"resources": resources, "activeDeadlineSeconds": 1200},
                 "scratch": {"type": "pvc", "size": "2Gi", "storageClassName": "standard"},
                 "retention": {"maxAgeDays": 30, "minSuccessfulBackups": 3, "schedule": {"enabled": False}},
                 "verification": {"enabled": True, "environmentRef": "physical" if name == "physical" else "pg18",
                                  "required": name != "optional", "dueWithinSeconds": 1 if name == "overdue" else 3600}}
            if name != "physical":
                p["logical"] = {"databases": ["fixture"]}
            if name == "direct":
                p["logical"]["transfer"] = {"mode": "direct", "partSizeMiB": 5}
                p["scratch"] = {"type": "emptyDir", "size": "64Mi"}
            if name == "directory":
                p["logical"].update(format="directory", jobs=2)
            if name == "filtered":
                p["logical"]["tableSelection"] = {"byDatabase": {"fixture": {"mode": "all", "exclude": {"tables": [{"schema": "public", "table": "secret"}]}}}}
            if name in ["logical", "physical"]:
                claim = "download-" + name
                p["download"] = {"allowedDestinationClaims": [claim]}
                core.create_namespaced_persistent_volume_claim(namespace, client.V1PersistentVolumeClaim(
                    metadata=client.V1ObjectMeta(name=claim, labels={"pgbk.io/installation": namespace, "pgbk.io/purpose": "download"}),
                    spec=client.V1PersistentVolumeClaimSpec(access_modes=["ReadWriteOnce"], storage_class_name="standard",
                        resources=client.V1VolumeResourceRequirements(requests={"storage": "1Gi"}))))
            values["profiles"][name] = p
        values_file.write_text(yaml.safe_dump(values))
        normalize(values)
        rendered = subprocess.check_output([args.helm, "template", "pgbk", args.chart, "-n", namespace, "-f", str(values_file)])
        for obj in yaml.load_all(rendered, Loader=StrictLoader):
            if obj and obj["kind"] == "Role":
                assert not {"subjects", "roleRef"} & set(obj), "Chart merged adjacent RBAC documents"
        subprocess.run([args.helm, "upgrade", "--install", "pgbk", args.chart, "--kube-context", args.context,
                        "-n", namespace, "-f", str(values_file)], check=True, stdout=subprocess.DEVNULL)
        if emulation:
            for ns in [namespace, verification_namespace]:
                for cron in batch.list_namespaced_cron_job(ns).items:
                    batch.patch_namespaced_cron_job(cron.metadata.name, ns,
                        {"spec": {"jobTemplate": {"spec": {"template": {"spec": {"nodeSelector": {"kubernetes.io/arch": None}}}}}}})
        passed("chart-install-real-api-rbac-pvc-and-manual-only-verification")
        runtime_digest = kubectl("exec", "deployment/source", "--", "python3", "-c",
            "import hashlib,pathlib,pgbk;h=hashlib.sha256();[(h.update(p.name.encode()+b'\\0'+p.read_bytes())) for p in sorted(pathlib.Path(pgbk.__file__).parent.glob('*')) if p.suffix in ['.py','.json','.txt']];print(h.hexdigest())").strip()
        assert runtime_digest == result["sourceSha256"], "Worker image does not match the new source implementation"
        passed("running-image-matches-new-implementation-source-sha256")

        reports = {}
        for name in ["logical", "direct", "directory", "filtered", "physical"]:
            reports[name] = backup(name)
            cat = catalog(name)
            run_id = reports[name]["runId"]
            view = cat.view(run_id)
            assert not view["profileCompliant"] and view["verificationPending"]
            before = [cat.s3.get(cat.run_prefix(run_id) + f) for f in ["manifest.json", "complete.json"]]
            if name == "logical":
                changed = copy.deepcopy(values)
                changed["verificationEnvironments"]["pg18"].update(assertions=["SELECT false"], timeoutSeconds=1)
                values_file.write_text(yaml.safe_dump(changed))
            check = verify(name, run_id)
            values_file.write_text(yaml.safe_dump(values))
            assert check["status"] == "passed" and check["sourceConnections"] == 0
            assert cat.view(run_id)["profileCompliant"]
            assert before == [cat.s3.get(cat.run_prefix(run_id) + f) for f in ["manifest.json", "complete.json"]]
            if name == "physical":
                assert any(t["rows"] == 1000 and t["table"] == "items" for c in check["checks"] for t in c["tableRowCounts"])
            passed(name + "-real-backup-s3-manual-restore-verification-immutable-publication")
        run_id = reports["logical"]["runId"]
        failure = verify("logical", run_id, "fail", expected=False)
        assert failure["reasonCode"] == "VERIFICATION_ASSERTION_FAILED"
        cat = catalog("logical")
        assert cat.view(run_id)["status"] == "verification-failed"
        retry = verify("logical", run_id, "slow", wait_for_job=False)
        assert cat.view(run_id)["status"] == "verification-failed"
        try:
            verify("logical", run_id, wait_for_job=False)
            raise AssertionError("Duplicate attempt was admitted")
        except Error as e:
            assert e.code == "VERIFICATION_BUSY" and e.details["activeAttemptId"] == retry["verificationId"]
        wait(retry)
        assert cat.view(run_id)["status"] == "verification-successful"
        assert len(cat.verification_records(run_id)[0]) == 3
        passed("required-failure-active-retry-duplicate-busy-late-rehabilitation-history")

        optional = backup("optional")
        verify("optional", optional["runId"], "fail", expected=False)
        assert catalog("optional").view(optional["runId"])["status"] == "successful"
        assert catalog("optional").view(optional["runId"])["profileCompliant"]
        passed("optional-failure-preserves-success-and-compliance")
        overdue = backup("overdue")
        time.sleep(2)
        assert catalog("overdue").view(overdue["runId"])["verificationOverdue"]
        reconciled = wait(command("verification", "reconcile", "--profile", "overdue"))
        assert overdue["runId"] in reconciled["overdueRunIds"]
        verify("overdue", overdue["runId"])
        assert not catalog("overdue").view(overdue["runId"])["verificationOverdue"]
        passed("read-time-overdue-service-job-reconciliation-and-late-pass")

        retention = catalog("retention")
        kube = Kubernetes(namespace, args.context, namespace)
        source_cat = catalog("logical")
        original = source_cat.describe(run_id)
        with repository_lease(kube, retention, Control(300)) as lease:
            for i in range(9):
                m = copy.deepcopy(original)
                m.update(retention.scope)
                m.update(runId=f"aged-{i}", completedAt=(datetime.now(timezone.utc)-timedelta(days=40+i)).isoformat())
                m["verificationPolicy"] = snapshot(normalize(values), "retention", m["completedAt"])
                for a in m["artifacts"]:
                    a["snapshotAt"] = m["completedAt"]
                    retention.s3.put(retention.run_prefix(m["runId"])+"artifacts/"+a["fileId"], source_cat.s3.get(source_cat.run_prefix(run_id)+"artifacts/"+a["fileId"]))
                retention.publish(m, lease)
                if i >= 3:
                    _, active = retention.start_verification(m["runId"], f"seed-{i}", env)
                    if i < 8:
                        retention.finish_verification({**active, "status": "passed", "finishedAt": datetime.now(timezone.utc).isoformat()})
        cleaned = wait(command("retention", "run", "--profile", "retention"))
        assert set(cleaned["deletedRunIds"]) == {"aged-6", "aged-7"}
        recovered = command("verification", "recover", "--profile", "retention", "--run-id", "aged-8",
                            "--attempt-id", "seed-8", "--acknowledge-worker-fenced")
        assert recovered["status"] == "recovered"
        cleaned = wait(command("retention", "run", "--profile", "retention"))
        assert cleaned["deletedRunIds"] == ["aged-8"]
        assert {r["runId"] for r in retention.list()["backups"]} == {f"aged-{i}" for i in range(6)}
        passed("dual-three-copy-minima-actual-s3-cleanup-and-operation-pin")

        download = logs / "offline-artifacts"
        if download.exists():
            shutil.rmtree(download)
        download.mkdir()
        for name in ["logical", "physical"]:
            completed_download = wait(command("backup", "download", "--profile", name, "--run-id", reports[name]["runId"],
                "--destination", "/destination/offline-copy", "--destination-pvc", "download-" + name, "--acknowledge-dedicated-pvc"))
            assert completed_download["status"] == "downloaded"
        reader = "download-reader"
        core.create_namespaced_pod(namespace, client.V1Pod(metadata=client.V1ObjectMeta(name=reader), spec=client.V1PodSpec(
            restart_policy="Never", automount_service_account_token=False,
            security_context=client.V1PodSecurityContext(run_as_user=999, run_as_group=999, fs_group=999),
            containers=[client.V1Container(name="reader", image=args.worker_image, command=["sleep", "300"],
                volume_mounts=[client.V1VolumeMount(name=name, mount_path="/downloads/" + name, read_only=True) for name in ["logical", "physical"]])],
            volumes=[client.V1Volume(name=name, persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name="download-" + name, read_only=True)) for name in ["logical", "physical"]])))
        kubectl("wait", "--for=condition=Ready", "pod/" + reader, "--timeout=120s")
        import tarfile
        from pgbk.storage import hash_file
        for name in ["logical", "physical"]:
            bundle = work / (name + ".tar")
            with bundle.open("wb") as output:
                subprocess.run(["kubectl", "--context", args.context, "-n", namespace, "exec", reader, "--",
                    "tar", "-C", "/downloads/" + name + "/offline-copy", "-cf", "-", "."], stdout=output, check=True)
            (download / name).mkdir()
            with tarfile.open(bundle) as archive:
                archive.extractall(download / name, filter="data")
            m = json.loads((download / name / "manifest.json").read_text())
            for a in m["artifacts"]:
                assert hash_file(download / name / a["filename"]) == (a["sha256"], a["size"])
            core.read_namespaced_persistent_volume_claim("download-" + name, namespace)
        core.delete_namespaced_pod(reader, namespace)
        passed("download-sha256-native-restore-instructions-source-independent-artifacts")
        result["offlineArtifacts"] = ["logical", "physical"]
        result["tools"] = reports["physical"]["manifest"]["tools"]
        all_logs = "\n".join(p.read_text() for p in logs.glob("*.log"))
        assert not any(value in all_logs for fields in secrets.values() for key, value in fields.items() if key != "username")
        passed("synthetic-secret-canaries-absent-from-worker-logs")
        # Generic ephemeral PVCs are owned by attempt Pods, never by the source.
        for job in jobs:
            batch.delete_namespaced_job(job["job"], job["namespace"], propagation_policy="Foreground")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and core.list_namespaced_persistent_volume_claim(verification_namespace).items:
            time.sleep(1)
        assert not core.list_namespaced_persistent_volume_claim(verification_namespace).items
        passed("owned-verification-pvc-garbage-collection-after-job-removal")
        result["status"] = "passed"
    except Exception as error:
        result["errorType"] = type(error).__name__
        result["error"] = str(error) if isinstance(error, AssertionError) else "Fixture execution failed; inspect private logs"
        raise
    finally:
        result["elapsedSeconds"] = time.monotonic() - started
        result["finishedAt"] = datetime.now(timezone.utc).isoformat()
        Path(args.output).write_bytes(json.dumps(result, indent=2).encode() + b"\n")
        if not args.keep:
            for ns in [namespace, verification_namespace]:
                try:
                    core.delete_namespace(ns)
                except Exception:
                    pass
            shutil.rmtree(work)


if __name__ == "__main__":
    main()
