"""Read-only, synthetic HTTP preview for visual QA, never a deployment mode.

The production entry point always requires HTTPS and Vault. This fixture binds
only loopback, returns synthetic metadata and denies every write. No Kubernetes,
Vault, cloud account or operational credentials are used.
"""
import copy
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "unit"))
from test_catalog import Held, MemoryS3
from pgbk.catalog import Catalog
from pgbk.ui.index import MetadataIndex
from pgbk.ui.journal import Journal
from pgbk.ui.server import handler
from pgbk.ui.service import Service


class PreviewAuth:
    def authorize(self, _):
        return {"subject": "Preview administrator", "csrf": "preview"}


def main():
    profiles = {"daily-logical": {"sourceId": "orders-primary", "kind": "logical"},
                "weekly-physical": {"sourceId": "analytics-primary", "kind": "physical-full"}}
    config = {"installationId": "production-eu-01", "namespace": "backups", "origin": "https://preview.invalid", "manualLaunch": False, "profiles": profiles}
    journal, cats, jobs = Journal(":memory:"), {}, []
    for index, (profile, p) in enumerate(profiles.items()):
        cat = Catalog(MemoryS3(), {"installationId": config["installationId"], "profiles": {profile: {"target": "db"}}, "targets": {"db": {"sourceId": p["sourceId"]}}}, profile)
        cats[profile] = cat
        for i in range(3):
            at = datetime.now(timezone.utc) - timedelta(hours=i * 24 + index * 3)
            run = f"run-2026110{6-i}-0{index+1}00"
            filenames = ["orders.dump", "globals.sql"] if not index else ["base.tar", "pg_wal.tar", "backup_manifest"]
            artifacts = []
            for n, filename in enumerate(filenames):
                artifact = {"fileId": f"artifact-{n}", "filename": filename, "format": "postgres-custom" if not index and n == 0 else "postgres-globals-sql" if not index else "postgres-tar",
                    "size": 187423112 if n == 0 else 2468, "sha256": hashlib.sha256(filename.encode()).hexdigest(), "purpose": "Database archive" if n == 0 else "Recovery dependency"}
                if not index and n == 0:
                    artifact.update(database="orders", scope="full-database", snapshotAt=at.isoformat(), snapshotTimeBasis="backup-start-lower-bound", compression={"method": "gzip", "level": 6}, transferMode="staged", archiveInventory={"tables": 42, "indexes": 67, "sequences": 12}, selection={"mode": "all"})
                artifacts.append(artifact)
            manifest = {"schemaVersion": 1, **cat.scope, "runId": run, "kind": p["kind"], "serverMajor": 18, "architecture": "x86_64", "source": {"tablespaceOids": []},
                "sourceEndpoint": {"host": "synthetic.internal", "port": 5432}, "tools": {"pg_dump": "18.0"}, "trigger": "schedule", "startedAt": at.isoformat(),
                "completedAt": (at + timedelta(seconds=47)).isoformat(), "durationSeconds": 47, "workerImage": "pgbk-worker-18@sha256:" + "a" * 64, "artifacts": artifacts}
            cat.publish(manifest, Held())
            uid = f"synthetic-uid-{index}-{i}"
            job = {"metadata": {"name": f"pgbk-{profile}-{i}", "namespace": "backups", "uid": uid, "creationTimestamp": at.isoformat(), "ownerReferences": [{"kind": "CronJob"}]},
                "spec": {"template": {"metadata": {"labels": {"pgbk.io/installation": config["installationId"], "pgbk.io/profile": profile, "pgbk.io/operation": "backup"}}}},
                "status": {"startTime": at.isoformat(), "completionTime": manifest["completedAt"], "conditions": [{"type": "Complete", "status": "True"}]}}
            jobs.append(job)
            cat.event(run, "complete", {"phase": "Completed", "jobName": job["metadata"]["name"], "jobUid": uid, "namespace": "backups", "trigger": "scheduled"})
    class PreviewCluster:
        def schedules(self):
            return [{"profileId": profile, **p, "schedule": "0 2 * * *", "timeZone": "UTC" if i == 0 else "Europe/Moscow", "suspend": bool(i),
                "lastScheduleTime": "2026-11-06T02:00:00Z", "nextOccurrence": "2026-11-07T02:00:00Z" if not i else None,
                "status": "Scheduled" if not i else "Paused", "launchAvailable": False} for i, (profile, p) in enumerate(profiles.items())]
        def jobs(self, namespace): return copy.deepcopy(jobs)
    index = MetadataIndex(cats, journal)
    index.refresh()
    service = Service(config, PreviewCluster(), index, journal)
    server = ThreadingHTTPServer(("127.0.0.1", 18444), handler(service, PreviewAuth()))
    print("Synthetic, read-only browser preview: http://127.0.0.1:18444", flush=True)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == "__main__":
    main()
