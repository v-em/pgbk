import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

from pgbk.catalog import read_json, timestamp
from pgbk.errors import Error
from pgbk.verification import state
from .common import Problem, identifier, project, utcnow


class MetadataIndex:
    def __init__(self, catalogs, journal):
        self.catalogs, self.journal = catalogs, journal
        self.lock = threading.Lock()
        self.errors = {}
        self.stop = threading.Event()

    def detail(self, profile, run_id):
        cat = self.catalogs[profile]
        try:
            manifest = cat.describe(identifier(run_id), verify=False, check_objects=False)
            reports, active = cat.verification_records(run_id)
            value = {**manifest, **state(manifest, reports, active)}
            value["manifest"] = manifest
            value["complete"] = read_json(cat.s3.get(cat.run_prefix(run_id) + "complete.json"))
            value["verificationRecords"] = {"attempts": reports, "activeAttempt": active}
            value["manifestVerified"] = True
            value["recoveryLanguages"] = ["en", "ru"] if "recoveryInstructions" in manifest else []
            value["fileCount"] = len(manifest["artifacts"])
            value["bytes"] = sum(a["size"] for a in manifest["artifacts"])
            value["trigger"] = {"schedule": "scheduled", "scheduled": "scheduled", "manual": "manual"}.get(manifest.get("trigger"), "unknown")
            value["redactions"] = "Internal endpoints, Secret references, selection/assertion SQL and unknown fields are redacted or omitted."
            # The stored manifest is projected independently of the derived view.
            projected = project(value)
            for field in ("manifest", "complete", "verificationRecords"):
                projected[field] = project(value[field])
            for field in ("manifestVerified", "recoveryLanguages", "fileCount", "redactions"):
                projected[field] = value[field]
            return projected
        except Error as error:
            if error.code in ("BACKUP_DELETING", "BACKUP_INCOMPLETE"):
                raise Problem("COPY_REMOVED_OR_UNAVAILABLE", 404) from None
            raise Problem(error.code, 422) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Problem("CATALOG_INVALID", 422) from None

    def recovery(self, profile, run_id, language):
        if language not in ("en", "ru"):
            raise Problem("NOT_FOUND", 404)
        detail = self.detail(profile, run_id)  # Fresh publication and deletion check.
        filename = "BACKUP-RESTORE.md" if language == "en" else "BACKUP-RESTORE.ru.md"
        notes = detail["manifest"].get("recoveryInstructions")
        if notes is None:
            raise Problem("RECOVERY_NOT_STORED", 404)
        note = next((n for n in notes if n["filename"] == filename), None)
        if not note:
            raise Problem("RECOVERY_INTEGRITY_ERROR", 422)
        cat = self.catalogs[profile]
        try:
            data = cat.s3.get(cat.run_prefix(run_id) + "recovery/" + filename)
        except (Error, Problem) as error:
            if error.code == "CATALOG_SIZE_LIMIT":
                raise Problem("RECOVERY_INTEGRITY_ERROR", 422) from None
            raise Problem("RECOVERY_TEMPORARILY_UNAVAILABLE") from None
        if data is None or len(data) > 8 * 1024 * 1024 or len(data) != note["size"] or hashlib.sha256(data).hexdigest() != note["sha256"]:
            raise Problem("RECOVERY_INTEGRITY_ERROR", 422)
        try:
            return {"language": language, "filename": filename, "text": data.decode("utf-8"), "sha256": note["sha256"]}
        except UnicodeDecodeError:
            raise Problem("RECOVERY_INTEGRITY_ERROR", 422) from None

    def scan(self, profile):
        cat = self.catalogs[profile]
        items, diagnostics, events = [], [], {}
        seen = set()
        for key in cat.s3.keys(cat.root + "runs/"):
            suffix = key[len(cat.root + "runs/"):].split("/")
            if len(suffix) != 2 or suffix[1] not in ("manifest.json", "complete.json") or suffix[0] in seen:
                continue
            run_id = suffix[0]
            seen.add(run_id)
            try:
                items.append(self.detail(profile, run_id))
            except Problem as error:
                if error.status == 503:
                    raise
                diagnostics.append({**cat.scope, "runId": run_id, "reasonCode": error.code})
        for key in cat.s3.keys(cat.root + "events/"):
            suffix = key[len(cat.root + "events/"):].split("/")
            if len(suffix) != 2 or not suffix[1].endswith(".json"):
                continue
            try:
                event = read_json(cat.s3.get(key))
                run_id = identifier(event["runId"])
                if run_id != suffix[0] or any(event.get(k, v) != v for k, v in cat.scope.items()):
                    raise Problem("EVENT_SCOPE_MISMATCH", 422)
                at = timestamp(event["at"])
                if run_id not in events or at > timestamp(events[run_id]["at"]):
                    events[run_id] = {**project(event), **cat.scope}
            except (Error, Problem, KeyError, TypeError) as error:
                if isinstance(error, Problem) and error.status == 503:
                    raise
                diagnostics.append({**cat.scope, "runId": suffix[0], "reasonCode": getattr(error, "code", "EVENT_INVALID")})
        data = {"items": items, "diagnostics": diagnostics, "events": list(events.values())}
        self.journal.cache(profile, data)
        with self.lock:
            self.errors.pop(profile, None)

    def refresh(self):
        def one(profile):
            try:
                self.scan(profile)
            except Exception as error:
                with self.lock:
                    self.errors[profile] = {"profileId": profile, "reasonCode": getattr(error, "code", "S3_UNAVAILABLE")}
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, self.catalogs))

    def start(self, seconds=30):
        def loop():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(seconds)
        thread = threading.Thread(target=loop, daemon=True, name="pgbk-ui-catalog")
        thread.start()
        return thread

    def snapshot(self):
        result = {"items": [], "diagnostics": [], "events": [], "refreshTime": None, "partialErrors": [], "partial": False}
        times = []
        with self.lock:
            result["partialErrors"] = list(self.errors.values())
        for profile in self.catalogs:
            data, at = self.journal.cached(profile)
            if data is None:
                result["partial"] = True
                continue
            times.append(at)
            for key in ("items", "diagnostics", "events"):
                result[key].extend(data[key])
        result["refreshTime"] = min(times) if times else None
        result["partial"] |= bool(result["partialErrors"])
        return result
