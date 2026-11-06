import copy

from .common import Problem, sha


def normalized_spec(spec, name):
    spec = copy.deepcopy(spec)
    # Defaults assigned by the Job API, absent in CronJob's embedded jobTemplate.
    defaults = {"parallelism": 1, "completions": 1, "backoffLimit": 6, "completionMode": "NonIndexed",
                "suspend": False, "manualSelector": False, "podReplacementPolicy": "TerminatingOrFailed",
                "managedBy": "kubernetes.io/job-controller"}
    for key, value in defaults.items():
        if spec.get(key) == value:
            spec.pop(key)
    pod = spec["template"]
    labels = pod.setdefault("metadata", {}).setdefault("labels", {})
    uid = labels.get("batch.kubernetes.io/controller-uid", labels.get("controller-uid"))
    if uid:
        if spec.get("selector") != {"matchLabels": {"batch.kubernetes.io/controller-uid": uid}}:
            raise Problem("ADMISSION_SELECTOR_DENIED", 403)
        spec.pop("selector")
        for key, expected in (("batch.kubernetes.io/controller-uid", uid), ("controller-uid", uid),
                              ("batch.kubernetes.io/job-name", name), ("job-name", name)):
            if key in labels:
                if labels[key] != expected:
                    raise Problem("ADMISSION_LABEL_DENIED", 403)
                labels.pop(key)
    # The API populates creationTimestamp:null on embedded ObjectMeta.
    if pod["metadata"].get("creationTimestamp") is None:
        pod["metadata"].pop("creationTimestamp", None)
    return spec


def validate(service, review):
    request = review["request"]
    uid = request["uid"]
    response = {"uid": uid, "allowed": False}
    try:
        c = service.config
        expected_user = f'system:serviceaccount:{c["uiNamespace"]}:{c["serviceAccount"]}'
        if request["userInfo"]["username"] != expected_user or request["operation"] != "CREATE" or request["resource"]["resource"] != "jobs" or request["namespace"] != c["namespace"]:
            raise Problem("ADMISSION_SCOPE_DENIED", 403)
        job = request["object"]
        meta = job["metadata"]
        profile = meta["labels"]["pgbk.io/profile"]
        service.profile(profile)
        prefix = meta["labels"]["pgbk.io/request"]
        with service.journal.lock:
            row = service.journal.db.execute("SELECT * FROM requests WHERE substr(id, 1, 63)=?", (prefix,)).fetchone()
        if not row or row["state"] != "reserved" or row["profile"] != profile or row["name"] != meta["name"]:
            raise Problem("ADMISSION_REQUEST_DENIED", 403)
        _, cron, version, _ = service.cluster.template(profile)
        if version != row["template"] or meta["annotations"]["pgbk.io/template-version"] != version:
            raise Problem("TEMPLATE_CHANGED", 409)
        template = copy.deepcopy(cron["spec"]["jobTemplate"])
        expected_labels = {**template["metadata"].get("labels", {}), "pgbk.io/request": prefix}
        expected_annotations = {**template["metadata"].get("annotations", {}), "pgbk.io/trigger": "manual", "pgbk.io/template-version": version}
        if meta.get("labels") != expected_labels or meta.get("annotations") != expected_annotations or meta.get("ownerReferences") or meta.get("finalizers") or meta.get("generateName"):
            raise Problem("ADMISSION_METADATA_DENIED", 403)
        spec = template["spec"]
        spec["template"]["metadata"]["annotations"] = {**spec["template"]["metadata"].get("annotations", {}), "pgbk.io/trigger": "manual"}
        if normalized_spec(job["spec"], meta["name"]) != normalized_spec(spec, meta["name"]):
            raise Problem("ADMISSION_TEMPLATE_DENIED", 403)
        if request.get("dryRun"):
            response["allowed"] = True
        else:
            response["allowed"] = True
    except Exception as error:
        response["status"] = {"code": 403, "message": getattr(error, "code", "ADMISSION_DENIED")}
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": response}
