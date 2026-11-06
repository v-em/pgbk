import copy
import json
import re
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from pgbk.catalog import Catalog
from pgbk.config import canonical
from pgbk.kube import Kubernetes
from .common import Problem, identifier, next_occurrence, sha


def validate_config(c):
    identifier(c["installationId"])
    for key in ("release", "namespace"):
        identifier(c[key])
    if not c["origin"].startswith("https://") or c["origin"].endswith("/"):
        raise ValueError("origin must be an HTTPS origin without a trailing slash")
    if not c["vault"]["address"].startswith("https://"):
        raise ValueError("Vault requires HTTPS")
    if not c["profiles"] or not c["configMapNames"]:
        raise ValueError("Explicit profiles and ConfigMap names are required")
    for profile, p in c["profiles"].items():
        identifier(profile)
        identifier(p["sourceId"])
        if p["kind"] not in ("logical", "physical-full") or p["storage"] not in c["storage"]:
            raise ValueError("Invalid profile kind/storage")
    for storage in c["storage"].values():
        prefix = storage["prefix"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]*", prefix) or ".." in prefix.split("/") or prefix.endswith("/"):
            raise ValueError("Invalid allowed S3 prefix")
        if not storage["endpoint"].startswith("https://"):
            raise ValueError("S3 requires HTTPS")
    if c.get("manualLaunch") and not c.get("admission", {}).get("enabled"):
        raise ValueError("Manual launch requires the admission restriction")
    return c


class ReadOnlyS3:
    def __init__(self, config):
        self.config, self.bucket = config, config["bucket"]
        self.client = boto3.client("s3", endpoint_url=config["endpoint"], region_name=config["region"],
            aws_access_key_id=Path(config["accessKeyFile"]).read_text().strip(),
            aws_secret_access_key=Path(config["secretKeyFile"]).read_text().strip(),
            verify=config.get("caFile", True), config=Config(connect_timeout=3, read_timeout=5,
                retries={"max_attempts": 1}, s3={"addressing_style": config.get("addressingStyle", "path")}))

    def call(self, operation, **kwargs):
        try:
            return getattr(self.client, operation)(Bucket=self.bucket, **kwargs)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise Problem("S3_ACCESS_DENIED" if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch") else "S3_UNAVAILABLE") from None
        except BotoCoreError:
            raise Problem("S3_UNAVAILABLE") from None

    def get(self, key, limit=8 * 1024 * 1024):
        response = self.call("get_object", Key=key)
        if response is None:
            return None
        with response["Body"] as body:
            value = body.read(limit + 1)
        if len(value) > limit:
            raise Problem("CATALOG_SIZE_LIMIT", 422)
        return value

    def keys(self, prefix):
        token = None
        while True:
            page = self.call("list_objects_v2", Prefix=prefix, **({"ContinuationToken": token} if token else {}))
            for obj in page.get("Contents", []):
                yield obj["Key"]
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]


def catalogs(config, stores=None):
    stores = stores or {name: ReadOnlyS3(s) for name, s in config["storage"].items()}
    result = {}
    for name, p in config["profiles"].items():
        scoped = {"installationId": config["installationId"], "profiles": {name: {"target": name}},
                  "targets": {name: {"sourceId": p["sourceId"]}}}
        result[name] = Catalog(stores[p["storage"]], scoped, name)
    return result


class Cluster:
    def __init__(self, config, kube=None):
        self.config = config
        self.kube = kube or Kubernetes(config["namespace"])
        self.api = client.ApiClient()

    def serialize(self, obj):
        return self.api.sanitize_for_serialization(obj)

    def template(self, profile):
        c, p = self.config, self.config["profiles"][profile]
        try:
            cron_obj = self.kube.batch.read_namespaced_cron_job(f'{c["release"]}-{profile}-backup', c["namespace"], _request_timeout=5)
            cron = self.serialize(cron_obj)
            labels = cron["metadata"].get("labels", {})
            template = cron["spec"]["jobTemplate"]
            pod = template["spec"]["template"]
            for metadata in (cron["metadata"], template["metadata"], pod["metadata"]):
                labels = metadata.get("labels", {})
                if labels.get("pgbk.io/installation") != c["installationId"] or labels.get("pgbk.io/profile") != profile:
                    raise Problem("TEMPLATE_SCOPE_MISMATCH", 409)
            if pod["metadata"]["labels"].get("pgbk.io/operation") != "backup":
                raise Problem("TEMPLATE_OPERATION_MISMATCH", 409)
            config_name = next(v["configMap"]["name"] for v in pod["spec"]["volumes"] if v["name"] == "config")
            if config_name not in c["configMapNames"]:
                raise Problem("CONFIGURATION_NOT_ALLOWED", 409)
            cm = self.kube.core.read_namespaced_config_map(config_name, c["namespace"], _request_timeout=5)
            if not cm.immutable:
                raise Problem("CONFIGURATION_NOT_IMMUTABLE", 409)
            values = json.loads(cm.data["config.json"])
            actual_profile = values["profiles"][profile]
            target = values["targets"][actual_profile["target"]]
            storage = values["storage"][actual_profile["storage"]]
            expected_storage = c["storage"][p["storage"]]
            if values["installationId"] != c["installationId"] or target["sourceId"] != p["sourceId"] or actual_profile["kind"] != p["kind"] or any(
                    storage[k] != expected_storage[k] for k in ("bucket", "prefix", "endpoint")):
                raise Problem("PROFILE_CONFIGURATION_MISMATCH", 409)
            details = {"globals": actual_profile.get("logical", {}).get("globals", "disabled"),
                       "maintenanceDatabase": target.get("maintenanceDatabase", "postgres")}
            return cron_obj, cron, sha({"uid": cron["metadata"]["uid"], "version": cron["metadata"]["resourceVersion"], "template": template}), details
        except ApiException as error:
            raise Problem("TEMPLATE_MISSING" if error.status == 404 else "KUBERNETES_ACCESS_DENIED" if error.status in (401, 403) else "KUBERNETES_UNAVAILABLE",
                          409 if error.status == 404 else 503) from None
        except (KeyError, ValueError, StopIteration, TypeError):
            raise Problem("TEMPLATE_INVALID", 409) from None

    def schedules(self):
        items = []
        for name, p in self.config["profiles"].items():
            item = {"profileId": name, "sourceId": p["sourceId"], "kind": p["kind"], "launchAvailable": False}
            try:
                _, cron, version, details = self.template(name)
                spec = cron["spec"]
                item.update(schedule=spec["schedule"], timeZone=spec.get("timeZone", "UTC"), suspend=spec.get("suspend", False),
                    lastScheduleTime=cron.get("status", {}).get("lastScheduleTime"), templateVersion=version, **details)
                item["nextOccurrence"] = None if item["suspend"] else next_occurrence(item["schedule"], item["timeZone"])
                item["status"] = "Paused" if item["suspend"] else "Scheduled"
                item["launchAvailable"] = bool(self.config.get("manualLaunch"))
            except Problem as error:
                item.update(status="Unavailable", reasonCode=error.code)
            items.append(item)
        return items

    def find_job(self, name):
        try:
            return self.serialize(self.kube.batch.read_namespaced_job(name, self.config["namespace"], _request_timeout=5))
        except ApiException as error:
            if error.status == 404:
                return None
            raise Problem("KUBERNETES_ACCESS_DENIED" if error.status in (401, 403) else "KUBERNETES_UNAVAILABLE") from None

    def launch(self, profile, cron, name, request_id, template_version):
        return self.kube.launch(profile, "backup", release=self.config["release"], request_identity={
            "name": name, "requestId": request_id, "templateVersion": template_version}, trusted_cron=cron)

    def jobs(self, namespace):
        try:
            result = self.kube.batch.list_namespaced_job(namespace,
                label_selector="pgbk.io/installation=" + self.config["installationId"], _request_timeout=5)
            return [self.serialize(job) for job in result.items]
        except ApiException as error:
            raise Problem("KUBERNETES_ACCESS_DENIED" if error.status in (401, 403) else "KUBERNETES_UNAVAILABLE") from None


def job_summary(job):
    meta, spec, status = job["metadata"], job["spec"], job.get("status", {})
    pod_meta = spec["template"].get("metadata", {})
    labels = pod_meta.get("labels", {})
    conditions = {c["type"] for c in status.get("conditions", []) if c["status"] == "True"}
    state = "Complete" if "Complete" in conditions else "Failed" if "Failed" in conditions else "Suspended" if spec.get("suspend") else "Running" if status.get("active", 0) else "Pending"
    manual = meta.get("annotations", {}).get("pgbk.io/trigger") == "manual" and pod_meta.get("annotations", {}).get("pgbk.io/trigger") == "manual"
    owned = any(o["kind"] == "CronJob" for o in meta.get("ownerReferences", []))
    trigger = "manual" if manual and not owned else "scheduled" if owned and not manual else "unknown"
    return {"name": meta["name"], "namespace": meta["namespace"], "uid": meta["uid"],
            "profileId": labels.get("pgbk.io/profile"), "installationId": labels.get("pgbk.io/installation"),
            "operation": labels.get("pgbk.io/operation", "unknown"), "trigger": trigger,
            "createdAt": meta.get("creationTimestamp"), "startedAt": status.get("startTime"),
            "completedAt": status.get("completionTime"), "status": state}
