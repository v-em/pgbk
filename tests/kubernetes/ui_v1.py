"""Local acceptance: real Kubernetes, PostgreSQL, Vault KV v2 and TLS Moto S3.

Run from the repository with the integration dependency group. All credentials
are generated in a mode-0700 temporary directory. No cloud accounts are used.
The optional serving phase keeps the test installation open for browser QA.
"""
import argparse
import base64
import copy
import json
import logging
import os
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
import requests
import yaml
from botocore.config import Config
from kubernetes import client, config as kube_config

# Moto captures this setting at import time. The Kubernetes hostname contains dots.
os.environ["S3_IGNORE_SUBDOMAIN_BUCKETNAME"] = "true"
from moto.server import create_backend_app
from werkzeug.serving import make_server, WSGIRequestHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", required=True)
    parser.add_argument("--context", default="pgbk-local")
    parser.add_argument("--worker-image", default="pgbk-worker:18-0.3.3")
    parser.add_argument("--ui-image", default="pgbk-ui:0.1.0")
    parser.add_argument("--allow-amd64-emulation", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--output", default="/tmp/pgbk-ui-integration.json")
    args = parser.parse_args()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    work = Path(tempfile.mkdtemp(prefix="pgbk-ui-"))
    work.chmod(0o700)
    namespace = "ui-v1-" + secrets.token_hex(3)
    container = namespace + "-vault"
    registry = namespace + "-registry"
    processes, servers, cases = [], [], []
    report = {"status": "failed", "namespace": namespace, "cases": cases, "dependencies": "real Kubernetes, PostgreSQL 18, Vault 1.18.0; TLS Moto S3", "browser": "pending"}
    def run(argv, **kwargs):
        result = subprocess.run(argv, capture_output=True, text=True, **kwargs)
        if result.returncode:
            raise RuntimeError(f"{argv[0]} failed: {result.stderr[-2500:]}")
        return result.stdout
    def kubectl(*argv, **kwargs):
        return run(["kubectl", "--context", args.context, "-n", namespace, *argv], **kwargs)
    def helm(*argv):
        return run([args.helm, "--kube-context", args.context, *argv])
    def passed(case):
        cases.append({"case": case, "status": "passed"})
        print(case + ": passed", flush=True)
    def wait(fn, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                result = fn()
                if result:
                    return result
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("Fixture readiness timeout")
    kube_config.load_kube_config(context=args.context)
    core, batch = client.CoreV1Api(), client.BatchV1Api()
    ca, key = work / "tls.crt", work / "tls.key"
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-keyout", str(key), "-out", str(ca),
         "-subj", "/CN=pgbk-ui-local", "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:host.minikube.internal,DNS:pgbk-ui.{namespace}.svc"])
    key.chmod(0o600)
    cert = str(ca)
    origin = "https://localhost:18443"
    root_token, admin_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    s3_failure, vault_failure = threading.Event(), threading.Event()
    s3_trace = []
    (work / "administrator-password").write_text(admin_password)
    (work / "administrator-password").chmod(0o600)
    env_file = work / "vault.env"
    env_file.write_text("VAULT_DEV_ROOT_TOKEN_ID=" + root_token + "\nVAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200\n")
    env_file.chmod(0o600)
    vault_base = "http://127.0.0.1:18200/v1/"
    def vault(path, data=None):
        response = requests.request("GET" if data is None else "POST", vault_base + path,
                                    json=data, headers={"X-Vault-Token": root_token}, timeout=10)
        response.raise_for_status()
        return response.json() if response.content else {}
    class Quiet(WSGIRequestHandler):
        def log_request(self, *args, **kwargs): pass
    class VaultTLS(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def proxy(self):
            if vault_failure.is_set():
                self.send_response(503)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = requests.request(self.command, "http://127.0.0.1:18200" + self.path, data=body,
                headers={k: v for k, v in self.headers.items() if k.lower() in ("content-type", "x-vault-token")}, timeout=10)
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
        do_GET = do_POST = proxy
    try:
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace)))
        run(["minikube", "-p", args.context, "image", "load", args.ui_image])
        run(["minikube", "-p", args.context, "image", "load", args.worker_image])
        def node(command):
            return run(["minikube", "-p", args.context, "ssh", "--", command]).strip()
        node(f"docker run -d --name {registry} -p 19050:5000 registry:2")
        def pin(image, repository):
            ref = f"localhost:19050/{repository}:ui-v1"
            node(f"docker tag {image} {ref}")
            node(f"docker push {ref}")
            return json.loads(node("docker image inspect " + ref + " --format '{{json .RepoDigests}}'"))[0]
        pinned_worker = pin(args.worker_image, "pgbk-worker")
        pinned_ui = pin(args.ui_image, "pgbk-ui")
        report["workerImage"] = pinned_worker
        report["uiImage"] = pinned_ui
        network = next(iter(json.loads(run(["docker", "inspect", args.context, "--format", "{{json .NetworkSettings.Networks}}"]))))
        run(["docker", "run", "-d", "--name", container, "--network", network, "-p", "127.0.0.1:18200:8200", "--env-file", str(env_file), "hashicorp/vault:1.18.0"])
        wait(lambda: requests.get(vault_base + "sys/health", timeout=2).status_code == 200)
        s3_app = create_backend_app("s3")
        def traced_s3(environ, start_response):
            s3_trace.append((environ["REQUEST_METHOD"], environ["PATH_INFO"]))
            if s3_failure.is_set():
                start_response("503 Service Unavailable", [("Content-Type", "application/xml")])
                return [b"<Error><Code>ServiceUnavailable</Code></Error>"]
            return s3_app(environ, start_response)
        s3server = make_server("0.0.0.0", 19443, traced_s3, threaded=True, ssl_context=(cert, str(key)), request_handler=Quiet)
        vaultserver = ThreadingHTTPServer(("0.0.0.0", 19444), VaultTLS)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, str(key))
        vaultserver.socket = tls.wrap_socket(vaultserver.socket, server_side=True)
        for server in (s3server, vaultserver):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        # Separate S3 identity; Moto tests metadata behavior, not AWS IAM enforcement.
        access, secret = secrets.token_hex(16), secrets.token_hex(32)
        s3 = boto3.client("s3", endpoint_url="https://localhost:19443", region_name="us-east-1", verify=cert,
                          aws_access_key_id=access, aws_secret_access_key=secret, config=Config(s3={"addressing_style": "path"}))
        s3.create_bucket(Bucket=namespace)
        source_password = secrets.token_urlsafe(24)
        for name, fields in {"source": {"username": "postgres", "password": source_password}, "s3-worker": {"accessKeyId": access, "secretAccessKey": secret},
                              "s3-reader": {"accessKeyId": secrets.token_hex(16), "secretAccessKey": secrets.token_hex(32)}, "ca": {"ca.crt": ca.read_text()},
                              "pgbk-ui-tls": {"tls.crt": ca.read_text(), "tls.key": key.read_text()}}.items():
            core.create_namespaced_secret(namespace, client.V1Secret(metadata=client.V1ObjectMeta(name=name), string_data=fields))
        source = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "source"}, "spec": {"replicas": 1,
            "selector": {"matchLabels": {"app": "source"}}, "template": {"metadata": {"labels": {"app": "source"}}, "spec": {
                "containers": [{"name": "postgres", "image": args.worker_image, "command": ["docker-entrypoint.sh"], "args": ["postgres"],
                    "env": [{"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "source", "key": "password"}}}],
                    "readinessProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]}, "periodSeconds": 2}}]}}}}
        kubectl("apply", "-f", "-", input=yaml.safe_dump(source))
        core.create_namespaced_service(namespace, client.V1Service(metadata=client.V1ObjectMeta(name="source"), spec=client.V1ServiceSpec(selector={"app": "source"}, ports=[client.V1ServicePort(port=5432)])))
        kubectl("rollout", "status", "deployment/source", "--timeout=180s")
        kubectl("exec", "-i", "deployment/source", "--", "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", input="CREATE DATABASE fixture;\n")
        system_id = kubectl("exec", "deployment/source", "--", "psql", "-U", "postgres", "-Atc", "SELECT system_identifier FROM pg_control_system()").strip()
        values = {"installationId": namespace, "coordinationNamespace": namespace, "rbac": {"create": True}, "coordination": {"createLeases": True},
            "images": {"18": pinned_worker}, "storage": {"archive": {"bucket": namespace, "prefix": "postgres", "endpoint": "https://host.minikube.internal:19443",
            "region": "us-east-1", "addressingStyle": "path", "credentialsSecretRef": "s3-worker", "tls": {"verify": True, "caSecretRef": "ca"}}},
            "targets": {"db": {"host": f"source.{namespace}.svc.cluster.local", "sourceId": "fixture", "concurrencyKey": namespace, "serverMajor": 18,
            "credentialsSecretRef": "source", "tls": {"mode": "disable"}}, "physical": {"host": f"source.{namespace}.svc.cluster.local", "sourceId": "fixture", "concurrencyKey": namespace,
            "serverMajor": 18, "credentialsSecretRef": "source", "tls": {"mode": "disable"}, "physicalEndpoint": "direct-primary", "expectedSystemIdentifier": system_id}}, "profiles": {}}
        for profile, kind, zone in [("logical", "logical", "UTC"), ("physical", "physical-full", "Europe/Moscow")]:
            values["profiles"][profile] = {"target": "physical" if profile == "physical" else "db", "kind": kind, "storage": "archive", "serviceAccounts": {"create": True},
                "execution": {"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}}},
                "scratch": {"type": "emptyDir", "size": "1Gi"}, "schedule": {"enabled": False, "cron": "* * * * *", "timeZone": zone},
                "retention": {"schedule": {"enabled": False}}}
            if kind == "logical": values["profiles"][profile]["logical"] = {"databases": ["fixture"], "globals": "enabled"}
        vf = work / "pgbk.yaml"; vf.write_text(yaml.safe_dump(values))
        helm("upgrade", "--install", "pgbk", "charts/pgbk", "-n", namespace, "-f", str(vf))
        if core.list_node().items[0].status.node_info.architecture != "amd64":
            assert args.allow_amd64_emulation
            for cron in batch.list_namespaced_cron_job(namespace).items:
                batch.patch_namespaced_cron_job(cron.metadata.name, namespace, {"spec": {"jobTemplate": {"spec": {"template": {"spec": {"nodeSelector": {"kubernetes.io/arch": None}}}}}}})
            report["workerEmulation"] = "amd64 worker on arm64 node; test-only nodeSelector patch"
        cm_name = core.list_namespaced_config_map(namespace, label_selector="pgbk.io/installation=" + namespace).items[0].metadata.name
        # Vault Kubernetes auth uses the API's internal minikube address.
        core.create_namespaced_service_account(namespace, client.V1ServiceAccount(metadata=client.V1ObjectMeta(name="vault-reviewer")))
        rbac = client.RbacAuthorizationV1Api()
        rbac.create_cluster_role_binding(client.V1ClusterRoleBinding(metadata=client.V1ObjectMeta(name=namespace + "-reviewer"),
            role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="system:auth-delegator"),
            subjects=[client.RbacV1Subject(kind="ServiceAccount", name="vault-reviewer", namespace=namespace)]))
        reviewer = core.create_namespaced_service_account_token("vault-reviewer", namespace, client.AuthenticationV1TokenRequest(spec=client.V1TokenRequestSpec(audiences=["https://kubernetes.default.svc.cluster.local"], expiration_seconds=7200))).status.token
        cluster_ip = run(["minikube", "-p", args.context, "ip"]).strip()
        cluster_ca = core.read_namespaced_config_map("kube-root-ca.crt", namespace).data["ca.crt"]
        vault("sys/auth/kubernetes", {"type": "kubernetes"})
        vault("auth/kubernetes/config", {"kubernetes_host": f"https://{cluster_ip}:8443", "kubernetes_ca_cert": cluster_ca, "token_reviewer_jwt": reviewer, "disable_iss_validation": True})
        secret_path = "secret/data/pgbk/" + namespace + "/administrator"
        vault("sys/policies/acl/pgbk-ui", {"policy": f'path "{secret_path}" {{ capabilities = ["read"] }}'})
        vault("auth/kubernetes/role/pgbk-ui", {"bound_service_account_names": ["pgbk-ui"], "bound_service_account_namespaces": [namespace], "policies": ["pgbk-ui"], "ttl": "5m"})
        vault(secret_path, {"data": {"username": "administrator", "password": admin_password}})
        ui_values = {"image": pinned_ui, "tls": {"existingSecret": "pgbk-ui-tls"}, "s3Secrets": ["s3-reader", "ca"],
            "admission": {"enabled": True, "caBundle": base64.b64encode(ca.read_bytes()).decode()}, "config": {
            "installationId": namespace, "release": "pgbk", "namespace": namespace, "origin": origin, "manualLaunch": True,
            "configMapNames": [cm_name], "refreshSeconds": 2, "profiles": {p: {"sourceId": "fixture", "kind": v["kind"], "storage": "archive"} for p, v in values["profiles"].items()},
            "storage": {"archive": {"bucket": namespace, "prefix": "postgres", "endpoint": values["storage"]["archive"]["endpoint"], "region": "us-east-1",
                "accessKeyFile": "/s3/s3-reader/accessKeyId", "secretKeyFile": "/s3/s3-reader/secretAccessKey", "caFile": "/s3/ca/ca.crt"}},
            "vault": {"address": "https://host.minikube.internal:19444", "role": "pgbk-ui", "secretPath": secret_path, "caFile": "/s3/ca/ca.crt"}}}
        uvf = work / "ui.yaml"; uvf.write_text(yaml.safe_dump(ui_values))
        def install():
            helm("upgrade", "--install", "pgbk-ui", "charts/pgbk-ui", "-n", namespace, "-f", str(uvf), "--wait", "--timeout", "180s")
        def forward():
            process = subprocess.Popen(["kubectl", "--context", args.context, "-n", namespace, "port-forward", "service/pgbk-ui", "18443:443"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(process)
            wait(lambda: requests.get(origin + "/healthz", verify=cert, timeout=2).status_code == 200)
            return process
        # A real scheduled copy must publish before UI installation.
        def scheduled_copy():
            cron_name = "pgbk-logical-backup"
            before = {o["Key"] for o in s3.list_objects_v2(Bucket=namespace).get("Contents", []) if o["Key"].endswith("complete.json")}
            batch.patch_namespaced_cron_job(cron_name, namespace, {"spec": {"suspend": False}})
            def published():
                after = {o["Key"] for o in s3.list_objects_v2(Bucket=namespace).get("Contents", []) if o["Key"].endswith("complete.json")}
                return after - before
            markers = wait(published, 240)
            batch.patch_namespaced_cron_job(cron_name, namespace, {"spec": {"suspend": True}})
            return markers
        scheduled_copy(); passed("scheduled publication before UI installation")
        install(); pf = forward(); passed("independent UI Helm install and HTTPS readiness")
        browser = requests.Session()
        def login(password):
            r = browser.post(origin + "/api/v1/session", json={"username": "administrator", "password": password}, headers={"Origin": origin}, verify=cert, timeout=20)
            assert r.status_code == 201, r.text
            browser.headers.update({"Origin": origin, "X-CSRF-Token": r.json()["csrfToken"]})
        def get(path):
            r = browser.get(origin + "/api/v1" + path, verify=cert, timeout=30)
            assert r.status_code == 200, r.text
            return r.json()
        login(admin_password); passed("real Vault Kubernetes login and KV-backed session")
        schedules = get("/schedules")["items"]
        assert all(s["suspend"] and s["launchAvailable"] for s in schedules)
        assert next(s for s in schedules if s["profileId"] == "logical")["maintenanceDatabase"] == "postgres"
        original = batch.read_namespaced_cron_job("pgbk-logical-backup", namespace)
        token = get("/profiles/logical/launch-token")["key"]
        def post():
            return browser.post(origin + "/api/v1/profiles/logical/backups", json={}, headers={"Idempotency-Key": token}, verify=cert, timeout=30)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: post(), range(4)))
        assert all(r.status_code in (200, 201, 202) for r in responses), [r.text for r in responses]
        accepted = next(r.json() for r in responses if r.json().get("jobUid"))
        current = batch.read_namespaced_cron_job("pgbk-logical-backup", namespace)
        assert (current.spec.schedule, current.spec.suspend) == (original.spec.schedule, original.spec.suspend)
        assert len([j for j in batch.list_namespaced_job(namespace).items if j.metadata.name == accepted["job"]]) == 1
        passed("paused manual launch, admission acceptance, four simultaneous POSTs, schedule preserved")
        def correlated():
            job = get(f'/jobs/{namespace}/{accepted["jobUid"]}')
            return job if job.get("backup") else None
        job = wait(correlated, 240)
        backup = get(f'/profiles/logical/sources/fixture/backups/{job["backup"]["runId"]}')
        assert backup["job"]["uid"] == accepted["jobUid"]
        for language in ("en", "ru"):
            note = get(f'/profiles/logical/sources/fixture/backups/{backup["runId"]}/recovery/{language}')
            filename = "BACKUP-RESTORE.md" if language == "en" else "BACKUP-RESTORE.ru.md"
            stored = s3.get_object(Bucket=namespace, Key=f'postgres/v1/{namespace}/fixture/logical/runs/{backup["runId"]}/recovery/{filename}')["Body"].read()
            assert note["text"].encode() == stored
        passed("real worker Job, bidirectional UID correlation and exact stored en/ru recovery notes")
        s3_trace.clear()
        get("/backups?limit=1")
        get(f'/profiles/logical/sources/fixture/backups/{backup["runId"]}')
        get(f'/profiles/logical/sources/fixture/backups/{backup["runId"]}/recovery/en')
        get("/jobs")
        assert not any(method in ("GET", "HEAD") and "/artifacts/" in path for method, path in s3_trace)
        report["archiveBodyReadsOnPageViews"] = 0
        passed("S3 request tracing: no archive GET or HEAD during page views")
        s3_failure.set()
        wait(lambda: get("/backups")["partialErrors"], 30)
        assert get("/jobs")["items"]
        s3_failure.clear()
        wait(lambda: not get("/backups")["partialErrors"], 30)
        passed("S3 outage preserves the last index and healthy Kubernetes Jobs")
        role = rbac.read_namespaced_role("pgbk-ui-read", namespace)
        original_rules = copy.deepcopy(role.rules)
        role.rules = [rule for rule in role.rules if "jobs" not in rule.resources or "create" in rule.verbs]
        rbac.replace_namespaced_role("pgbk-ui-read", namespace, role)
        denied = get("/jobs")
        assert denied["partialErrors"] and all(item["status"] != "Job removed" for item in denied["items"])
        assert get("/backups")["items"]
        rbac.patch_namespaced_role("pgbk-ui-read", namespace, {"rules": client.ApiClient().sanitize_for_serialization(original_rules)})
        passed("Kubernetes access failure preserves S3 and never counterfeits Job removal")
        vault_failure.set()
        assert browser.get(origin + "/api/v1/backups", verify=cert).status_code == 503
        assert browser.post(origin + "/api/v1/session", json={"username": "administrator", "password": admin_password}, headers={"Origin": origin}, verify=cert).status_code == 503
        vault_failure.clear()
        passed("Vault outage fails closed for both sessions and protected reads")
        # Impersonate the UI identity with a changed image; admission must deny.
        cron = batch.read_namespaced_cron_job("pgbk-logical-backup", namespace)
        malicious = client.ApiClient().sanitize_for_serialization(client.V1Job(api_version="batch/v1", kind="Job", metadata=client.V1ObjectMeta(name="injected", labels=cron.spec.job_template.metadata.labels), spec=copy.deepcopy(cron.spec.job_template.spec)))
        malicious["spec"]["template"]["spec"]["containers"][0]["image"] = "busybox:latest"
        result = subprocess.run(["kubectl", "--context", args.context, "-n", namespace, f"--as=system:serviceaccount:{namespace}:pgbk-ui", "create", "-f", "-"], input=json.dumps(malicious), text=True, capture_output=True)
        assert result.returncode and "denied" in result.stderr.lower()
        for resource in ("secrets", "pods/log", "leases.coordination.k8s.io"):
            result = subprocess.run(["kubectl", "--context", args.context, "-n", namespace,
                f"--as=system:serviceaccount:{namespace}:pgbk-ui", "auth", "can-i", "get", resource], capture_output=True, text=True)
            assert result.stdout.strip() == "no"
        passed("admission rejects injected Job; no Secret, log or Lease RBAC")
        # UI restart preserves both journal replay and PVC contents.
        kubectl("rollout", "restart", "deployment/pgbk-ui")
        kubectl("rollout", "status", "deployment/pgbk-ui", "--timeout=180s")
        pf.terminate(); pf.wait(); pf = forward()
        assert post().json()["jobUid"] == accepted["jobUid"]
        passed("Pod restart preserves journal and original Job identity")
        new_password = secrets.token_urlsafe(32)
        vault(secret_path, {"data": {"username": "administrator", "password": new_password}})
        assert browser.get(origin + "/api/v1/backups", verify=cert).status_code == 401
        login(new_password)
        (work / "administrator-password").write_text(new_password)
        passed("Vault rotation rejects old session and accepts new credentials")
        # While the UI is installed, schedules use the ordinary worker path.
        scheduled_copy(); passed("scheduled publication alongside UI")
        pf.terminate(); pf.wait()
        helm("uninstall", "pgbk-ui", "-n", namespace)
        assert core.read_namespaced_persistent_volume_claim("pgbk-ui-journal", namespace)
        scheduled_copy(); passed("UI uninstall preserves scheduled publication and journal PVC")
        install(); pf = forward(); login(new_password)
        assert post().json()["jobUid"] == accepted["jobUid"]
        passed("reinstall recovers journal replay")
        report["status"] = "passed"
        report["workDirectory"] = str(work)
        report["origin"] = origin
        report["limitations"] = ["Moto does not certify provider IAM", "local Vault dev mode behind a TLS fixture proxy", "cloud and ingress-controller tests not run"]
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        if args.serve:
            print(f"Browser QA ready: {origin}; ephemeral credentials in {work}/administrator-password", flush=True)
            while True: time.sleep(1)
    finally:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        for process in processes:
            if process.poll() is None: process.terminate()
        for server in servers: server.shutdown()
        try: run(["docker", "rm", "-f", container])
        except Exception: pass
        try: helm("uninstall", "pgbk-ui", "-n", namespace)
        except Exception: pass
        try: helm("uninstall", "pgbk", "-n", namespace)
        except Exception: pass
        try: core.delete_namespace(namespace)
        except Exception: pass
        try: client.RbacAuthorizationV1Api().delete_cluster_role_binding(namespace + "-reviewer")
        except Exception: pass
        try: run(["minikube", "-p", args.context, "ssh", "--", f"docker rm -f {registry}"])
        except Exception: pass


if __name__ == "__main__":
    main()
