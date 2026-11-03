import hashlib
import copy
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from .errors import Error, require


def lease_name(kind, key):
    return "pgbk-" + kind + "-" + hashlib.sha256(key.encode()).hexdigest()[:24]


class Kubernetes:
    def __init__(self, namespace, context=None, coordination_namespace=None):
        try:
            if os.environ.get("KUBERNETES_SERVICE_HOST"):
                config.load_incluster_config()
            else:
                config.load_kube_config(config_file=os.environ.get("KUBECONFIG"), context=context)
        except Exception:
            raise Error("KUBERNETES_UNAVAILABLE", "Cannot load Kubernetes credentials", 3) from None
        self.namespace = namespace
        self.coordination_namespace = coordination_namespace or namespace
        self.coordination = client.CoordinationV1Api()
        self.batch = client.BatchV1Api()
        self.core = client.CoreV1Api()

    def lease(self, name, control, holder=None):
        return Lease(self.coordination, self.coordination_namespace, name, control, holder)

    def job_info(self):
        name = os.environ.get("PGBK_JOB_NAME")
        if not name:
            return {"jobName": None, "jobUid": None, "scheduledAt": None, "namespace": self.namespace}
        try:
            job = self.batch.read_namespaced_job(name, self.namespace, _request_timeout=5)
            return {"jobName": name, "jobUid": job.metadata.uid, "namespace": self.namespace,
                    "podUid": os.environ.get("PGBK_POD_UID"),
                    "scheduledAt": (job.metadata.annotations or {}).get("batch.kubernetes.io/cronjob-scheduled-timestamp")}
        except ApiException:
            return {"jobName": name, "jobUid": os.environ.get("PGBK_JOB_UID"), "namespace": self.namespace, "scheduledAt": None}

    def reconcile(self, event):
        if event.get("phase") in ["Completed", "Failed", "Cancelled", "SkippedBusy"] or not event.get("jobName"):
            return event
        try:
            job = self.batch.read_namespaced_job(event["jobName"], event["namespace"], _request_timeout=5)
            if job.metadata.uid != event.get("jobUid"):
                return {**event, "phase": "Unknown", "reasonCode": "JOB_IDENTITY_CHANGED"}
            if job.status.failed:
                return {**event, "phase": "Failed", "reasonCode": "JOB_FAILED_AFTER_LAST_EVENT"}
            if job.spec.suspend:
                return {**event, "phase": "Cancelled", "reasonCode": "JOB_SUSPENDED"}
            if job.status.succeeded:
                return {**event, "phase": "Unknown", "reasonCode": "JOB_SUCCEEDED_WITHOUT_VALID_MARKER"}
            return event
        except ApiException:
            return {**event, "phase": "Unknown", "reasonCode": "JOB_STATE_UNAVAILABLE"}

    def launch(self, profile, operation, release="pgbk", parameters=None, destination_pvc=None):
        cron_name = f"{release}-{profile}-{operation}"
        try:
            cron = self.batch.read_namespaced_cron_job(cron_name, self.namespace, _request_timeout=15)
            template = cron.spec.job_template
            metadata = client.V1ObjectMeta(generate_name=cron_name[:48] + "-", labels=template.metadata.labels)
            metadata.annotations = {"pgbk.io/trigger": "manual"}
            spec = template.spec
            if parameters:
                spec.template.spec.containers[0].args.extend(parameters)
            if destination_pvc:
                spec.template.spec.volumes.append(client.V1Volume(name="destination", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=destination_pvc)))
                spec.template.spec.containers[0].volume_mounts.append(client.V1VolumeMount(name="destination", mount_path="/destination"))
            spec.template.metadata.annotations = {**(spec.template.metadata.annotations or {}), "pgbk.io/trigger": "manual"}
            job = self.batch.create_namespaced_job(self.namespace, client.V1Job(metadata=metadata, spec=spec), _request_timeout=15)
            return {"schemaVersion": 1, "status": "Pending", "job": job.metadata.name,
                    "jobUid": job.metadata.uid, "namespace": self.namespace}
        except ApiException as e:
            raise Error("KUBERNETES_DENIED" if e.status in [401, 403] else "JOB_CREATE_FAILED",
                        "Cannot create Job from the configured CronJob template", 8 if e.status in [401, 403] else 5) from None

    def launch_verification(self, profile, manifest, environment, attempt_id, release="pgbk"):
        """Use the copy's retained immutable configuration and captured environment."""
        import json
        try:
            cron = self.batch.read_namespaced_cron_job(f"{release}-{profile}-backup", self.namespace, _request_timeout=15)
            spec = copy.deepcopy(cron.spec.job_template.spec)
            pod = spec.template.spec
            pod.service_account_name = environment["serviceAccountName"]
            worker = pod.containers[0]
            worker.image = environment["image"]
            worker.resources = client.V1ResourceRequirements(**environment["resources"])
            worker.args = ["--config", "/config/config.json", "--namespace", environment["namespace"],
                           "--scratch", "/scratch", "--timeout", str(environment["timeoutSeconds"]),
                           "--output", "json", "worker", "verify", "--profile", profile,
                           "--run-id", manifest["runId"], "--attempt-id", attempt_id,
                           "--environment-json", json.dumps(environment)]
            # Source credentials and volumes never enter the verification environment.
            config_volume = copy.deepcopy(next(v for v in pod.volumes if v.name == "config"))
            if manifest.get("configurationRef"):
                config_volume.config_map.name = manifest["configurationRef"]
            scratch = environment["scratch"]
            volume = client.V1Volume(name="scratch")
            if scratch["type"] == "emptyDir":
                volume.empty_dir = client.V1EmptyDirVolumeSource(size_limit=scratch["size"])
            elif scratch["type"] == "existingClaim":
                volume.persistent_volume_claim = client.V1PersistentVolumeClaimVolumeSource(claim_name=scratch["claimName"])
            else:
                volume.ephemeral = client.V1EphemeralVolumeSource(volume_claim_template=client.V1PersistentVolumeClaimTemplate(
                    metadata=client.V1ObjectMeta(labels={"pgbk.io/purpose": "scratch"}),
                    spec=client.V1PersistentVolumeClaimSpec(access_modes=["ReadWriteOnce"], storage_class_name=scratch.get("storageClassName"),
                        resources=client.V1VolumeResourceRequirements(requests={"storage": scratch["size"]}))))
            storage = manifest.get("storageSnapshot", {})
            refs = {storage["credentialsSecretRef"], environment["credentialsSecretRef"], environment["clientCredentialsSecretRef"]}
            if storage.get("tls", {}).get("caSecretRef"):
                refs.add(storage["tls"]["caSecretRef"])
            pod.volumes = [config_volume, volume] + [client.V1Volume(name="secret-" + ref,
                secret=client.V1SecretVolumeSource(secret_name=ref, default_mode=0o440)) for ref in sorted(refs)]
            worker.volume_mounts = [client.V1VolumeMount(name="config", mount_path="/config", read_only=True),
                                   client.V1VolumeMount(name="scratch", mount_path="/scratch")] + [
                client.V1VolumeMount(name="secret-" + ref, mount_path="/secrets/" + ref, read_only=True) for ref in sorted(refs)]
            for env in worker.env or []:
                if env.name == "PGBK_WORKER_IMAGE":
                    env.value = environment["image"]
            spec.active_deadline_seconds = environment["timeoutSeconds"]
            spec.ttl_seconds_after_finished = environment.get("failureDebugTtlSeconds", 86400)
            spec.template.metadata.labels["pgbk.io/operation"] = "verify"
            spec.template.metadata.annotations = {"pgbk.io/trigger": "manual"}
            metadata = client.V1ObjectMeta(name="pgbk-verify-" + attempt_id,
                labels={**(cron.spec.job_template.metadata.labels or {}), "pgbk.io/attempt": attempt_id})
            job = self.batch.create_namespaced_job(environment["namespace"], client.V1Job(metadata=metadata, spec=spec), _request_timeout=15)
            return {"schemaVersion": 1, "status": "Pending", "job": job.metadata.name,
                    "jobUid": job.metadata.uid, "namespace": environment["namespace"], "verificationId": attempt_id,
                    "runId": manifest["runId"]}
        except ApiException as e:
            raise Error("KUBERNETES_DENIED" if e.status in [401, 403] else "VERIFICATION_JOB_CREATE_FAILED",
                        "Cannot create verification Job in the selected environment", 8 if e.status in [401, 403] else 5) from None

    def terminal_verification(self, active):
        """Only reconcile proven stopped containers; API absence cannot fence a node."""
        if not active.get("jobName"):
            return None
        try:
            job = self.batch.read_namespaced_job(active["jobName"], active["namespace"], _request_timeout=10)
            if active.get("jobUid") and job.metadata.uid != active["jobUid"]:
                return None
            if (job.metadata.labels or {}).get("pgbk.io/attempt") != active["verificationId"]:
                return None
            terminal = any(c.status == "True" and c.type in ["Failed", "Complete"] for c in job.status.conditions or [])
            if not terminal:
                return None
            pods = self.core.list_namespaced_pod(active["namespace"],
                label_selector="batch.kubernetes.io/controller-uid=" + job.metadata.uid, _request_timeout=10).items
            if not pods or any(p.status.phase not in ["Succeeded", "Failed"] or p.status.reason == "NodeLost" or
                               any(s.state.running for s in p.status.container_statuses or []) for p in pods):
                return None
            reason = "VERIFICATION_REPORT_MISSING" if job.status.succeeded else "VERIFICATION_JOB_FAILED"
            waiting = [s.state.waiting.reason for p in pods for s in p.status.container_statuses or [] if s.state.waiting]
            if any(r in ["ErrImagePull", "ImagePullBackOff", "InvalidImageName"] for r in waiting):
                reason = "VERIFICATION_IMAGE_FAILED"
            elif any(p.status.reason == "DeadlineExceeded" for p in pods):
                reason = "VERIFICATION_JOB_DEADLINE_EXCEEDED"
            return reason
        except ApiException:
            return None

    def release_verification_slot(self, active):
        if not active.get("leaseName"):
            return
        lease = self.coordination.read_namespaced_lease(active["leaseName"], self.coordination_namespace, _request_timeout=5)
        if lease.spec.holder_identity == active.get("leaseHolder"):
            lease.spec.holder_identity = ""
            self.coordination.replace_namespaced_lease(active["leaseName"], self.coordination_namespace, lease, _request_timeout=5)

    def destination_claim(self, name, installation, acknowledged):
        require(acknowledged, "DESTINATION_ACK_REQUIRED", "Download PVC requires a dedicated stopped-volume acknowledgement", 3)
        self.check_scratch({"type": "existingClaim", "claimName": name}, installation, purpose="download")

    def cancel(self, job):
        try:
            # Suspension terminates running Pods gracefully and leaves Job status discoverable.
            self.batch.patch_namespaced_job(job, self.namespace, {"spec": {"suspend": True}}, _request_timeout=15)
            return {"schemaVersion": 1, "status": "cancellation-requested", "job": job}
        except ApiException:
            raise Error("CANCEL_FAILED", "Cannot suspend the selected Job", 5) from None

    def check_scratch(self, scratch, installation_id, purpose="scratch"):
        if scratch["type"] != "existingClaim":
            return
        name = scratch["claimName"]
        try:
            pvc = self.core.read_namespaced_persistent_volume_claim(name, self.namespace, _request_timeout=10)
            labels = pvc.metadata.labels or {}
            require(labels.get("pgbk.io/installation") == installation_id and labels.get("pgbk.io/purpose") == purpose,
                    "UNSAFE_PVC", "Claim must be owned and labelled as dedicated pgbk scratch", 3)
            # API absence alone never establishes node fencing. The source/repository lease
            # remains held across crashes; attachment checks add another prerequisite.
            pods = self.core.list_namespaced_pod(self.namespace, _request_timeout=10)
            own = os.environ.get("PGBK_POD_UID")
            for pod in pods.items:
                if pod.metadata.uid == own:
                    continue
                if pod.status.phase in ["Succeeded", "Failed"]:
                    continue
                for volume in pod.spec.volumes or []:
                    if volume.persistent_volume_claim and volume.persistent_volume_claim.claim_name == name:
                        raise Error("PVC_IN_USE", "Another Pod uses the selected scratch claim", 3)
        except ApiException:
            raise Error("PVC_OWNERSHIP_UNVERIFIED", "Cannot establish scratch ownership and attachment state", 3) from None


class Lease:
    """Fail closed: a nonempty holder is never stolen, regardless of expiry."""
    def __init__(self, api, namespace, name, control, holder=None):
        self.api, self.namespace, self.name, self.control = api, namespace, name, control
        self.holder = holder or os.environ.get("PGBK_POD_UID", "cli") + "-" + uuid.uuid4().hex
        self.finished = threading.Event()
        self.last_renewed = time.monotonic()
        self.thread = None

    def read(self):
        return self.api.read_namespaced_lease(self.name, self.namespace, _request_timeout=5)

    def check(self):
        if time.monotonic() - self.last_renewed >= 15:
            self.control.cancel("LEASE_LOST")
            raise Error("LEASE_LOST", "Lease renewal failed; all source work has stopped", 5)

    def __enter__(self):
        try:
            lease = self.read()
            require(not lease.spec.holder_identity, "SOURCE_BUSY", "Coordination lease is held; expired holders require fencing", 4)
            lease.spec.holder_identity = self.holder
            lease.spec.lease_duration_seconds = 30
            lease.spec.acquire_time = datetime.now(timezone.utc)
            lease.spec.renew_time = datetime.now(timezone.utc)
            self.api.replace_namespaced_lease(self.name, self.namespace, lease, _request_timeout=5)
        except ApiException as e:
            raise Error("SOURCE_BUSY" if e.status == 409 else "LEASE_UNAVAILABLE",
                        "Cannot acquire the pre-created coordination lease", 4 if e.status == 409 else 5) from None
        self.last_renewed = time.monotonic()
        self.control.guards.append(self.check)
        self.thread = threading.Thread(target=self.renew, daemon=True)
        self.thread.start()
        return self

    def renew(self):
        while not self.finished.wait(5):
            try:
                lease = self.read()
                if lease.spec.holder_identity != self.holder:
                    self.control.cancel("LEASE_LOST")
                    return
                lease.spec.renew_time = datetime.now(timezone.utc)
                self.api.replace_namespaced_lease(self.name, self.namespace, lease, _request_timeout=5)
                self.last_renewed = time.monotonic()
            except Exception:
                if time.monotonic() - self.last_renewed >= 15:
                    self.control.cancel("LEASE_LOST")
                    return

    def confirm(self):
        self.control.check()
        try:
            lease = self.read()
            if lease.spec.holder_identity != self.holder:
                self.control.cancel("LEASE_LOST")
                self.control.check()
        except Exception:
            self.control.cancel("LEASE_LOST")
            raise Error("LEASE_LOST", "Cannot confirm ownership before publication", 5) from None

    def __exit__(self, *_):
        self.finished.set()
        if self.thread:
            self.thread.join(timeout=6)
        self.control.guards.remove(self.check)
        # On loss, preserve orphan state; only a fenced operator can unlock it.
        if self.control.reason == "LEASE_LOST" and self.control.stop.is_set():
            return
        try:
            lease = self.read()
            if lease.spec.holder_identity == self.holder:
                lease.spec.holder_identity = ""
                lease.spec.renew_time = datetime.now(timezone.utc)
                self.api.replace_namespaced_lease(self.name, self.namespace, lease, _request_timeout=5)
        except Exception:
            pass  # Safe busy orphan; never attempt an unconditional patch.
