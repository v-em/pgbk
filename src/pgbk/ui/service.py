import base64
import hmac
import json
from datetime import datetime, timezone

from pgbk.catalog import timestamp
from pgbk.errors import Error
from .adapters import job_summary
from .common import Problem, identifier, sha, utcnow


class Service:
    def __init__(self, config, cluster, index, journal):
        self.config, self.cluster, self.index, self.journal = config, cluster, index, journal
        self.job_cache = {}

    def profile(self, profile, source=None):
        p = self.config["profiles"].get(profile)
        if p is None or (source is not None and p["sourceId"] != source):
            raise Problem("NOT_FOUND", 404)
        return p

    def schedules(self):
        items = self.cluster.schedules()
        try:
            with self.journal.transaction() as db:
                db.execute("SELECT 1")
        except Problem:
            for item in items:
                item.update(launchAvailable=False, reasonCode="JOURNAL_UNAVAILABLE")
        return items

    def page(self, items, query, subject, collection, extra=None):
        try:
            limit = min(100, max(1, int(query.get("limit", "25"))))
        except ValueError:
            raise Problem("INVALID_PAGE_SIZE", 400) from None
        filters = {k: v for k, v in query.items() if k != "cursor"}
        binding = sha({"filters": filters, "subject": subject, "installation": self.config["installationId"],
                       "profiles": self.config["profiles"], "namespaces": self.namespaces(), "collection": collection})
        offset = 0
        cursor = query.get("cursor")
        if cursor:
            try:
                raw, signature = cursor.split(".")
                if not hmac.compare_digest(hmac.digest(self.journal.cursor_key.encode(), raw.encode(), "sha256").hex(), signature):
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                if data["binding"] != binding or data["offset"] < 0:
                    raise ValueError()
                offset = data["offset"]
            except (ValueError, KeyError, TypeError):
                raise Problem("CURSOR_INVALID", 400) from None
        next_cursor = None
        if offset + limit < len(items):
            raw = base64.urlsafe_b64encode(json.dumps({"binding": binding, "offset": offset + limit}).encode()).decode().rstrip("=")
            next_cursor = raw + "." + hmac.digest(self.journal.cursor_key.encode(), raw.encode(), "sha256").hex()
        return {"items": items[offset:offset + limit], "nextCursor": next_cursor, "refreshTime": utcnow(), "partialErrors": [], **(extra or {})}

    def backups(self, query, subject):
        snapshot = self.index.snapshot()
        items = snapshot.pop("items")
        snapshot.pop("events")
        for field, key in (("profile", "profileId"), ("source", "sourceId"), ("kind", "kind")):
            if query.get(field):
                items = [item for item in items if item.get(key) == query[field]]
        try:
            for field, compare in (("from", lambda a, b: a >= b), ("to", lambda a, b: a <= b)):
                if query.get(field):
                    boundary = timestamp(query[field])
                    items = [item for item in items if compare(timestamp(item["completedAt"]), boundary)]
        except Error:
            raise Problem("TIME_FILTER_INVALID", 400) from None
        items.sort(key=lambda item: (timestamp(item["completedAt"]), item["runId"], item["installationId"], item["sourceId"], item["profileId"]), reverse=True)
        # List pages carry summaries; full projected JSON is available on direct reads.
        fields = {"installationId", "profileId", "sourceId", "runId", "kind", "completedAt", "trigger", "bytes", "restoreVerification", "manifestVerified", "fileCount"}
        return self.page([{k: v for k, v in item.items() if k in fields} for item in items], query, subject, "backups", snapshot)

    def detail(self, profile, source, run):
        self.profile(profile, source)
        detail = self.index.detail(profile, run)
        events = [e for e in self.index.snapshot()["events"] if e["profileId"] == profile and e["sourceId"] == source and e["runId"] == run and e.get("jobUid") and e.get("namespace") in self.namespaces()]
        detail["job"] = {"uid": events[0]["jobUid"], "namespace": events[0]["namespace"], "name": events[0].get("jobName")} if events else None
        return detail

    def issue(self, profile, subject):
        self.profile(profile)
        if not self.config.get("manualLaunch"):
            raise Problem("LAUNCH_FORBIDDEN", 403)
        _, _, version, details = self.cluster.template(profile)
        return {"key": self.journal.issue(subject, profile, version), "profileId": profile,
                "sourceId": self.profile(profile)["sourceId"], "kind": self.profile(profile)["kind"],
                "expiresInSeconds": 86400, **details}

    def identity(self, job, row, profile):
        summary = job_summary(job)
        labels = job["metadata"].get("labels", {})
        if summary["installationId"] != self.config["installationId"] or summary["profileId"] != profile or summary["operation"] != "backup" or labels.get("pgbk.io/request") != row["id"][:63] or job["metadata"].get("annotations", {}).get("pgbk.io/template-version") != row["template"]:
            raise Problem("JOB_IDENTITY_CONFLICT", 409)
        return {"namespace": summary["namespace"], "job": summary["name"], "jobUid": summary["uid"], "status": summary["status"]}

    def launch(self, profile, key, subject, request_id):
        self.profile(profile)
        if not self.config.get("manualLaunch"):
            raise Problem("LAUNCH_FORBIDDEN", 403)
        if not key or len(key) > 128:
            raise Problem("IDEMPOTENCY_KEY_REQUIRED", 400)
        cron, _, version, _ = self.cluster.template(profile)
        row, owner = self.journal.reserve(key, subject, profile, version)
        if not owner:
            job = self.cluster.find_job(row["name"])
            if job:
                result = self.identity(job, row, profile)
                if row["result"] and json.loads(row["result"])["jobUid"] != result["jobUid"]:
                    raise Problem("JOB_IDENTITY_CONFLICT", 409)
                self.journal.finish(row["id"], result, subject, profile)
                return 200, result
            if row["result"]:
                return 200, {**json.loads(row["result"]), "status": "Job removed"}
            self.journal.audit(subject, profile, "OUTCOME_UNKNOWN", request_id)
            return 202, {"status": "Outcome unknown", "reasonCode": "LAUNCH_OUTCOME_UNKNOWN", "job": row["name"],
                         "namespace": self.config["namespace"], "requestId": request_id}
        try:
            result = self.cluster.launch(profile, cron, row["name"], row["id"], version)
            self.journal.finish(row["id"], result, subject, profile)
            return 201, result
        except (Error, Problem) as error:
            if getattr(error, "code", None) == "KUBERNETES_DENIED":
                self.journal.audit(subject, profile, "KUBERNETES_OPERATION_FORBIDDEN", request_id)
                raise Problem("KUBERNETES_OPERATION_FORBIDDEN", 403) from None
            # The create response may be lost. Never submit a second create for this key.
            self.journal.audit(subject, profile, "RECONCILIATION_PENDING", request_id)
            job = self.cluster.find_job(row["name"])
            if job:
                result = self.identity(job, row, profile)
                self.journal.finish(row["id"], result, subject, profile)
                return 200, result
            return 202, {"status": "Outcome unknown", "reasonCode": "LAUNCH_OUTCOME_UNKNOWN", "job": row["name"], "namespace": self.config["namespace"]}

    def namespaces(self):
        return [self.config["namespace"], *self.config.get("jobNamespaces", [])]

    def jobs(self, query, subject):
        snapshot = self.index.snapshot()
        items, errors, successful = [], list(snapshot["partialErrors"]), set()
        for namespace in self.namespaces():
            try:
                jobs = self.cluster.jobs(namespace)
                current = [job_summary(job) for job in jobs]
                current = [job for job in current if job["profileId"] in self.config["profiles"] and job["installationId"] == self.config["installationId"]]
                self.job_cache[namespace] = (current, utcnow())
                successful.add(namespace)
            except Problem as error:
                errors.append({"namespace": namespace, "reasonCode": error.code})
            cached, at = self.job_cache.get(namespace, ([], None))
            items.extend({**job, "lastSuccessfulRead": at} for job in cached)
        by_uid = {(item["namespace"], item["uid"]): item for item in items}
        events = snapshot["events"]
        for event in events:
            ns, uid = event.get("namespace"), event.get("jobUid")
            if ns not in self.namespaces() or not uid:
                continue
            job = by_uid.get((ns, uid))
            if job is None:
                job = {"namespace": ns, "uid": uid, "name": event.get("jobName"), "profileId": event["profileId"],
                       "operation": event.get("operation", "backup"), "trigger": event.get("trigger", "unknown"),
                       "status": "Job removed" if ns in successful else "Unknown", "createdAt": event["at"]}
                items.append(job)
                by_uid[(ns, uid)] = job
            if job["profileId"] != event["profileId"]:
                continue
            if not job.get("worker") or timestamp(event["at"]) > timestamp(job["worker"]["at"]):
                job["worker"] = event
                job["sourceId"] = event["sourceId"]
            try:
                self.index.detail(event["profileId"], event["runId"])
                job["backup"] = {key: event[key] for key in ("profileId", "sourceId", "runId")}
            except Problem:
                pass
        for row in self.journal.history():
            if row["profile"] not in self.config["profiles"]:
                continue
            if not row["result"]:
                items.append({"namespace": self.config["namespace"], "uid": "request-" + row["id"], "name": row["name"],
                    "profileId": row["profile"], "sourceId": self.profile(row["profile"])["sourceId"], "operation": "backup", "trigger": "manual",
                    "status": "Outcome unknown", "reasonCode": "LAUNCH_OUTCOME_UNKNOWN", "createdAt": datetime.fromtimestamp(row["issued"], timezone.utc).isoformat()})
                continue
            record = json.loads(row["result"])
            identity = (record["namespace"], record["jobUid"])
            if identity not in by_uid:
                items.append({"namespace": record["namespace"], "uid": record["jobUid"], "name": record["job"],
                    "profileId": row["profile"], "sourceId": self.profile(row["profile"])["sourceId"], "operation": "backup", "trigger": "manual",
                    "status": "Job removed" if record["namespace"] in successful else "Unknown", "createdAt": datetime.fromtimestamp(row["issued"], timezone.utc).isoformat()})
        for job in items:
            job.setdefault("sourceId", self.profile(job["profileId"])["sourceId"])
            if job.get("startedAt"):
                job["durationSeconds"] = (timestamp(job["completedAt"]) if job.get("completedAt") else datetime.now(timezone.utc)).timestamp() - timestamp(job["startedAt"]).timestamp()
        if query.get("operation", "backup") != "all":
            items = [job for job in items if job["operation"] == query.get("operation", "backup")]
        for field, key in (("profile", "profileId"), ("status", "status")):
            if query.get(field):
                items = [job for job in items if job[key] == query[field]]
        items.sort(key=lambda job: (job.get("createdAt") or "", job["namespace"], job["uid"]), reverse=True)
        return self.page(items, query, subject, "jobs", {"partialErrors": errors, "partial": bool(errors), "refreshTime": min((at for _, at in self.job_cache.values()), default=None)})

    def job(self, namespace, uid, subject):
        if namespace not in self.namespaces():
            raise Problem("NOT_FOUND", 404)
        # Direct reads must search beyond the first list page.
        page = self.jobs({"operation": "all", "limit": "100"}, subject)
        while True:
            for job in page["items"]:
                if job["namespace"] == namespace and job["uid"] == uid:
                    return {**job, "partialErrors": page["partialErrors"], "refreshTime": page["refreshTime"]}
            if not page["nextCursor"]:
                break
            page = self.jobs({"operation": "all", "limit": "100", "cursor": page["nextCursor"]}, subject)
        if page["partialErrors"]:
            raise Problem("KUBERNETES_UNAVAILABLE")
        raise Problem("NOT_FOUND", 404)
