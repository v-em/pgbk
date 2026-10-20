import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import canonical
from .errors import Error, require
from .storage import hash_file


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value),
            "UNSAFE_RUN_ID", "Run identity is invalid", 2)
    return value


def read_json(data):
    try:
        def unique(items):
            value = {}
            for key, item in items:
                require(key not in value, "CATALOG_INVALID", "Catalog has duplicate JSON keys", 6)
                value[key] = item
            return value
        value = json.loads(data, object_pairs_hook=unique)
        require(isinstance(value, dict), "CATALOG_INVALID", "Catalog object must be a JSON object", 6)
        return value
    except (ValueError, TypeError):
        raise Error("CATALOG_INVALID", "Catalog JSON is invalid", 6) from None


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        require(result.tzinfo is not None, "CATALOG_INVALID", "Catalog timestamps require a time zone", 6)
        return result
    except (ValueError, TypeError):
        raise Error("CATALOG_INVALID", "Catalog timestamp is invalid", 6) from None


def retention_candidates(runs, max_age_days, minimum, clock=None, protected=()):
    clock = clock or datetime.now(timezone.utc)
    require(max_age_days > 0 and minimum > 0, "INVALID_RETENTION", "Retention limits must be positive")
    ordered = sorted(runs, key=lambda r: (timestamp(r["completedAt"]), r["runId"]), reverse=True)
    newest = {r["runId"] for r in ordered[:minimum]}
    return [r for r in ordered if r["runId"] not in newest and r["runId"] not in protected
            and timestamp(r["completedAt"]) < clock - timedelta(days=max_age_days)]


class Catalog:
    def __init__(self, s3, config, profile_name):
        self.s3 = s3
        p = config["profiles"][profile_name]
        target = config["targets"][p["target"]]
        self.scope = {"installationId": config["installationId"], "sourceId": target["sourceId"], "profileId": profile_name}
        self.root = s3.config["prefix"] + "/v1/" + "/".join(self.scope.values()) + "/"

    def run_prefix(self, run_id):
        return self.root + "runs/" + safe_id(run_id) + "/"

    def event(self, run_id, event_id, data):
        key = self.root + "events/" + safe_id(run_id) + "/" + safe_id(event_id) + ".json"
        self.s3.put(key, canonical({"schemaVersion": 1, "runId": run_id, "at": now(), **data}))

    def publish(self, manifest, lease):
        run_id = manifest["runId"]
        prefix = self.run_prefix(run_id)
        require(self.s3.get(prefix + "complete.json") is None, "RUN_ALREADY_COMPLETED", "Completed runs are immutable", 6)
        data = canonical(manifest)
        self.s3.put(prefix + "manifest.json", data)
        require(self.s3.get(prefix + "manifest.json") == data, "MANIFEST_MISMATCH", "Stored manifest differs from local bytes", 6)
        lease.confirm()
        marker = canonical({"schemaVersion": 1, "runId": run_id, "manifestSha256": hashlib.sha256(data).hexdigest(),
                            "completedAt": manifest["completedAt"]})
        # PUT is idempotent for this immutable unique marker; reconcile a lost final response.
        try:
            self.s3.put(prefix + "complete.json", marker)
        except Error:
            if self.s3.get(prefix + "complete.json") != marker:
                raise
        require(self.s3.get(prefix + "complete.json") == marker, "MARKER_MISMATCH", "Completion publication is unverified", 6)

    def describe(self, run_id, verify=True):
        prefix = self.run_prefix(run_id)
        require(self.s3.get(prefix + "deletion.json") is None, "BACKUP_DELETING", "Run is being deleted", 6)
        marker_bytes = self.s3.get(prefix + "complete.json")
        manifest_bytes = self.s3.get(prefix + "manifest.json")
        require(marker_bytes is not None and manifest_bytes is not None, "BACKUP_INCOMPLETE", "Run is not complete", 6)
        marker, manifest = read_json(marker_bytes), read_json(manifest_bytes)
        require(marker.get("schemaVersion") == manifest.get("schemaVersion") == 1 and
                marker.get("runId") == manifest.get("runId") == run_id and
                marker.get("manifestSha256") == hashlib.sha256(manifest_bytes).hexdigest(),
                "MANIFEST_MISMATCH", "Marker or manifest integrity is invalid", 6)
        require(all(manifest.get(k) == v for k, v in self.scope.items()), "CATALOG_SCOPE_MISMATCH", "Manifest scope differs from repository", 6)
        require(manifest.get("kind") in ["logical", "physical-full"] and
                marker.get("completedAt") == manifest.get("completedAt"), "CATALOG_INVALID", "Manifest kind or completion time is invalid", 6)
        timestamp(manifest["completedAt"])
        require(type(manifest.get("serverMajor")) is int and manifest["serverMajor"] in range(14, 19) and
                isinstance(manifest.get("source"), dict) and isinstance(manifest.get("tools"), dict) and
                isinstance(manifest.get("sourceEndpoint"), dict) and
                isinstance(manifest["sourceEndpoint"].get("host"), str) and
                type(manifest["sourceEndpoint"].get("port")) is int,
                "CATALOG_INVALID", "Manifest lacks valid restore identity or tool metadata", 6)
        if manifest["kind"] == "physical-full":
            require(manifest.get("architecture") in ["x86_64", "aarch64"] and
                    isinstance(manifest["source"].get("tablespaceOids"), list) and
                    all(isinstance(oid, str) and re.fullmatch("[0-9]+", oid) for oid in manifest["source"]["tablespaceOids"]),
                    "CATALOG_INVALID", "Invalid physical restore metadata", 6)
        artifacts = manifest.get("artifacts")
        require(isinstance(artifacts, list) and artifacts, "CATALOG_INVALID", "Manifest requires artifacts", 6)
        seen, filenames, databases = set(), set(), set()
        for item in artifacts:
            require(isinstance(item, dict), "CATALOG_INVALID", "Invalid artifact record", 6)
            file_id = safe_id(item.get("fileId"))
            require(file_id not in seen, "CATALOG_INVALID", "Duplicate artifact identity", 6)
            seen.add(file_id)
            filename = item.get("filename")
            require(isinstance(filename, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", filename) and
                    filename.casefold() not in filenames and filename.casefold() not in
                    ["manifest.json", "download.json", "restore.txt", "restore.ru.txt"],
                    "UNSAFE_FILENAME", "Artifact filename is unsafe or repeated", 6)
            filenames.add(filename.casefold())
            if manifest["kind"] == "logical":
                require(item.get("format") in ["postgres-custom", "postgres-directory-tar", "postgres-globals-sql"], "CATALOG_INVALID", "Unknown logical artifact format", 6)
                if item["format"] in ["postgres-custom", "postgres-directory-tar"]:
                    db = item.get("database")
                    require(isinstance(db, str) and db and db not in databases and
                            item.get("scope") in ["full-database", "filtered-database"],
                            "CATALOG_INVALID", "Invalid or repeated logical database artifact", 6)
                    databases.add(db)
                    require(item["scope"] != "filtered-database" or isinstance(item.get("selection"), dict),
                            "CATALOG_INVALID", "Filtered archive requires frozen selection metadata", 6)
            else:
                require(filename in ["base.tar", "pg_wal.tar", "backup_manifest"] or
                        filename in [oid + ".tar" for oid in manifest["source"]["tablespaceOids"]],
                        "CATALOG_INVALID", "Physical artifact filename is outside the declared set", 6)
            require(type(item.get("size")) is int and item["size"] >= 0 and
                    re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")), "CATALOG_INVALID", "Artifact size/hash is invalid", 6)
            key = prefix + "artifacts/" + file_id
            head = self.s3.call("head_object", Key=key)
            require(head is not None and head["ContentLength"] == item["size"], "ARTIFACT_SIZE_MISMATCH", "Stored artifact has wrong size", 6)
            if verify:
                require(self.s3.remote_hash(key) == (item["sha256"], item["size"]),
                        "ARTIFACT_HASH_MISMATCH", "Stored artifact SHA-256 differs", 6)
        if manifest["kind"] == "physical-full":
            require(filenames == {"base.tar", "pg_wal.tar", "backup_manifest", *[oid + ".tar" for oid in manifest["source"]["tablespaceOids"]]},
                    "PHYSICAL_SET_INCOMPLETE", "Physical manifest must contain the entire declared artifact set", 6)
        return manifest

    def list(self):
        result, invalid = [], []
        for key in self.s3.keys(self.root + "runs/"):
            if key.endswith("/complete.json"):
                run_id = key[len(self.root + "runs/"):].split("/")[0]
                try:
                    result.append(self.describe(run_id))
                except Error as e:
                    if e.code != "BACKUP_DELETING":
                        invalid.append({"runId": run_id, "reasonCode": e.code})
        result.sort(key=lambda r: (r["completedAt"], r["runId"]), reverse=True)
        events = {}
        for key in self.s3.keys(self.root + "events/"):
            event = read_json(self.s3.get(key))
            run_id = event.get("runId")
            if run_id:
                if run_id not in events or timestamp(event["at"]) > timestamp(events[run_id]["at"]):
                    events[run_id] = event
        complete = {m["runId"] for m in result}
        return {"schemaVersion": 1, "backups": result, "invalidRuns": invalid,
                "diagnosticRuns": [e for run_id, e in sorted(events.items()) if run_id not in complete and run_id != "cleanup"],
                "lastCleanup": events.get("cleanup")}

    def verification_status(self, run_id):
        reports = []
        for key in self.s3.keys(self.root + "verification/" + safe_id(run_id) + "/"):
            report = read_json(self.s3.get(key))
            reports.append(report)
        return max(reports, key=lambda r: r["finishedAt"]) if reports else {"status": "not-run"}

    def download(self, run_id, destination, database=None):
        manifest = self.describe(run_id)
        destination = Path(destination)
        require(not destination.is_symlink(), "UNSAFE_DESTINATION", "Download destination cannot be a symlink")
        require(not any(parent.is_symlink() for parent in destination.absolute().parents),
                "UNSAFE_DESTINATION", "Download destination parents cannot be symlinks")
        require(not destination.exists() or (destination.is_dir() and not any(destination.iterdir())),
                "DESTINATION_NOT_EMPTY", "Download destination must be empty")
        items = [a for a in manifest["artifacts"] if database is None or a.get("database") == database]
        require(items, "DATABASE_NOT_FOUND", "No archive for the selected database", 2)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in items:
            path = destination / item["filename"]
            self.s3.transfer("get", self.run_prefix(run_id) + "artifacts/" + item["fileId"], path)
            require(hash_file(path) == (item["sha256"], item["size"]), "ARTIFACT_HASH_MISMATCH", "Downloaded SHA-256 differs", 6)
        (destination / "manifest.json").write_bytes(canonical(manifest))
        (destination / "DOWNLOAD.json").write_bytes(canonical({"schemaVersion": 1, "runId": run_id,
                                                               "downloadedFileIds": [a["fileId"] for a in items]}))
        from importlib.resources import files
        (destination / "RESTORE.txt").write_text(files("pgbk").joinpath("native-restore.txt").read_text())
        (destination / "RESTORE.ru.txt").write_text(files("pgbk").joinpath("native-restore.ru.txt").read_text())
        return manifest

    def cleanup(self, policy, lease):
        removed = []
        # Resume withdrawn deletions before selecting new candidates.
        prefixes = []
        for key in self.s3.keys(self.root + "runs/"):
            if key.endswith("/deletion.json"):
                relative = key[len(self.root + "runs/"):]
                run_id = relative.split("/")[0]
                record = read_json(self.s3.get(key))
                require(record.get("runId") == run_id and record.get("schemaVersion") == 1,
                        "CATALOG_INVALID", "Deletion record is invalid", 6)
                prefixes.append(run_id)
        runs = self.list()["backups"]
        prefixes.extend(r["runId"] for r in retention_candidates(runs, policy["maxAgeDays"], policy["minSuccessfulBackups"]))
        for run_id in dict.fromkeys(prefixes):
            prefix = self.run_prefix(run_id)
            lease.confirm()
            self.s3.put(prefix + "deletion.json", canonical({"schemaVersion": 1, "runId": run_id, "startedAt": now()}))
            self.s3.delete(prefix + "complete.json")
            require(self.s3.get(prefix + "complete.json") is None, "DELETE_DENIED", "Completion marker withdrawal failed", 5)
            keys = list(self.s3.keys(prefix))
            for key in keys:
                if key != prefix + "deletion.json":
                    lease.confirm()
                    self.s3.delete(key)
                    require(self.s3.call("head_object", Key=key) is None, "DELETE_DENIED", "Object deletion was denied", 5)
            self.s3.delete(prefix + "deletion.json")
            removed.append(run_id)
        incomplete = set()
        abandoned = []
        for key in self.s3.keys(self.root + "runs/"):
            relative = key[len(self.root + "runs/"):]
            run_id = relative.split("/")[0]
            safe_id(run_id)
            incomplete.add(run_id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=policy.get("incompleteGraceDays", 7))
        for run_id in sorted(incomplete):
            prefix = self.run_prefix(run_id)
            if self.s3.get(prefix + "complete.json") is not None or self.s3.get(prefix + "deletion.json") is not None:
                continue
            events = [read_json(self.s3.get(key)) for key in self.s3.keys(self.root + "events/" + run_id + "/")]
            if not events:
                continue
            final = max(events, key=lambda e: timestamp(e["at"]))
            if final.get("writerStopped") is not True or final.get("phase") not in ["Failed", "Cancelled", "SkippedBusy"] or timestamp(final["at"]) >= cutoff:
                continue
            lease.confirm()
            for key in list(self.s3.keys(prefix)):
                self.s3.delete(key)
                require(self.s3.call("head_object", Key=key) is None, "DELETE_DENIED", "Incomplete object deletion denied", 5)
            abandoned.append(run_id)
        return {"schemaVersion": 1, "status": "completed", "deletedRunIds": removed,
                "deletedIncompleteRunIds": abandoned,
                "orphanPolicy": "No cleanup without confirmed writer termination"}
