"""Actual Helm/API/Lease smoke; worker scheduling requires an amd64 node."""
import argparse
import copy
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import yaml
from kubernetes import client, config

from pgbk.errors import Error
from pgbk.kube import Kubernetes, lease_name
from pgbk.process import Control
from pgbk.config import normalize
from pgbk.catalog import Catalog
from pgbk.runner import repository_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    os.environ["KUBECONFIG"] = args.kubeconfig
    config.load_kube_config(config_file=args.kubeconfig)
    core = client.CoreV1Api()
    namespace = "pgbk-smoke-" + uuid.uuid4().hex[:8]
    core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace)))
    try:
        values = yaml.safe_load(Path("examples/values.yaml").read_text())
        values["coordinationNamespace"] = namespace
        values["rbac"] = {"create": True}
        values["coordination"] = {"createLeases": True}
        values["profiles"]["example-logical"]["serviceAccounts"] = {"create": True}
        values["profiles"]["example-logical"]["retention"]["schedule"] = {"enabled": False}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as file:
            yaml.safe_dump(values, file)
            file.flush()
            subprocess.run([args.helm, "upgrade", "--install", "pgbk", "charts/pgbk", "-n", namespace,
                            "--kubeconfig", args.kubeconfig, "-f", file.name], check=True, stdout=subprocess.DEVNULL)
        kube = Kubernetes(namespace)
        c = normalize(values)
        target = c["targets"]["example"]
        name = lease_name("source", target["concurrencyKey"])
        cases = []
        with kube.lease(name, Control(), "first"):
            try:
                with kube.lease(name, Control(), "second"):
                    raise AssertionError("Concurrent holder was admitted")
            except Error as e:
                assert e.exit_code == 4
        cases.append({"case": "real-lease-conflict-and-release", "status": "passed"})
        class Scope:
            config = c["storage"]["archive"]
            bucket = config["bucket"]
        catalog = Catalog(Scope(), c, "example-logical")
        repo_name = lease_name("repository", repository_key(catalog))
        kube.coordination.read_namespaced_lease(repo_name, namespace)
        cases.append({"case": "chart-and-worker-repository-hash-match", "status": "passed"})
        cron = kube.batch.read_namespaced_cron_job("pgbk-example-logical-backup", namespace)
        assert cron.spec.suspend and cron.spec.time_zone == "UTC"
        snapshot = cron.spec.job_template.spec.template.spec.volumes[0].config_map.name
        assert core.read_namespaced_config_map(snapshot, namespace).immutable
        created = kube.launch("example-logical", "backup")
        job = kube.batch.read_namespaced_job(created["job"], namespace)
        assert job.spec.template.spec.volumes[0].config_map.name == snapshot
        assert job.spec.backoff_limit == 0
        assert job.spec.template.spec.containers[0].security_context.read_only_root_filesystem
        kube.cancel(created["job"])
        assert kube.batch.read_namespaced_job(created["job"], namespace).spec.suspend
        cases.append({"case": "immutable-template-manual-job-cancellation", "status": "passed"})
        roles = client.RbacAuthorizationV1Api().list_namespaced_role(namespace)
        for role in roles.items:
            for rule in role.rules:
                assert "secrets" not in rule.resources and "pods/exec" not in rule.resources and "*" not in rule.verbs
        cases.append({"case": "chart-role-source-mutation-boundary", "status": "passed"})
        authorization = client.AuthorizationV1Api()
        def permitted(account, group, resource, verb, name=None):
            review = client.V1SubjectAccessReview(spec=client.V1SubjectAccessReviewSpec(
                user=f"system:serviceaccount:{namespace}:{account}",
                resource_attributes=client.V1ResourceAttributes(namespace=namespace, group=group, resource=resource, verb=verb, name=name)))
            return authorization.create_subject_access_review(review).status.allowed
        assert permitted("pgbk-example-logical", "coordination.k8s.io", "leases", "update", name)
        assert permitted("pgbk-example-logical", "batch", "jobs", "get", created["job"])
        assert not permitted("pgbk-example-logical-repository", "coordination.k8s.io", "leases", "update", name)
        for account in ["pgbk-example-logical", "pgbk-example-logical-repository"]:
            assert not permitted(account, "", "secrets", "get", "source-credentials")
            assert not permitted(account, "batch", "jobs", "create")
            assert not permitted(account, "apps", "statefulsets", "patch", "source")
        cases.append({"case": "actual-service-account-authorization-boundary", "status": "passed"})
        # Reusing accounts and shared pre-created Leases must not require creating those resources.
        reuse = copy.deepcopy(values)
        reuse["rbac"] = {"create": False}
        reuse["coordination"] = {"createLeases": False}
        reuse["profiles"]["example-logical"]["serviceAccounts"] = {"create": False,
            "backup": "pgbk-example-logical", "repository": "pgbk-example-logical-repository"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as file:
            yaml.safe_dump(reuse, file); file.flush()
            rendered = subprocess.check_output([args.helm, "template", "reuse", "charts/pgbk", "-n", namespace, "-f", file.name])
            objects = list(yaml.safe_load_all(rendered))
            assert not any(o and o["kind"] in ["Namespace", "ServiceAccount", "Lease", "Role", "RoleBinding"] for o in objects)
            subprocess.run([args.helm, "upgrade", "--install", "reuse", "charts/pgbk", "-n", namespace,
                            "--kubeconfig", args.kubeconfig, "-f", file.name], check=True, stdout=subprocess.DEVNULL)
        cases.append({"case": "existing-identities-leases-no-rbac-mutations-install", "status": "passed"})
        nodes = core.list_node().items
        architecture = nodes[0].status.node_info.architecture
        print(json.dumps({"schemaVersion": 1, "cases": cases, "kubernetesVersion": nodes[0].status.node_info.kubelet_version,
                          "nodeArchitecture": architecture, "workerJobs": "not-run: amd64 required" if architecture != "amd64" else "run separately"}))
    finally:
        core.delete_namespace(namespace)


if __name__ == "__main__":
    main()
