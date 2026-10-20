import json
import os
import platform
import shutil
import sys
import tempfile
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog, now
from .config import digest, profile
from .errors import Error, require
from .kube import lease_name
from .postgres import logical_backup, physical_backup, preflight
from .storage import S3


def repository_key(catalog):
    return catalog.s3.config["endpoint"].rstrip("/") + "/" + catalog.s3.bucket + "/" + catalog.root


def repository_lease(kube, catalog, control):
    return kube.lease(lease_name("repository", repository_key(catalog)), control)


def backup(config, profile_name, kube, control, scratch_root):
    p, target, storage = profile(config, profile_name)
    kube.check_scratch(p["scratch"], config["installationId"])
    run_id = os.environ.get("PGBK_JOB_UID", "local")[:36].lower() + "-" + uuid.uuid4().hex
    run_id = "run-" + run_id.replace("_", "-")
    scratch_root = Path(scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix=run_id + "-", dir=scratch_root))
    s3 = S3(storage, control)
    catalog = Catalog(s3, config, profile_name)
    started = now()
    sequence = 0
    phase = "Pending"
    job_info = kube.job_info() if hasattr(kube, "job_info") else {}
    def state(name, **fields):
        nonlocal sequence, phase
        phase = name
        sequence += 1
        event = {"phase": name, "profileId": profile_name, "sourceId": target["sourceId"],
                 "runId": run_id, "trigger": os.environ.get("PGBK_TRIGGER") or "manual",
                 "durationSeconds": (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds(),
                 **job_info, **fields}
        print(json.dumps({"at": now(), **event}), file=sys.stderr, flush=True)
        catalog.event(run_id, f"{sequence:06d}", event)
    try:
        with ExitStack() as stack:
            source_lease = stack.enter_context(kube.lease(lease_name("source", target["concurrencyKey"]), control))
            repository = stack.enter_context(repository_lease(kube, catalog, control))
            state("Preflight")
            info = preflight(target, p, control, scratch)
            # Validate storage permissions with a unique per-run event, not bucket mutation.
            state("BackingUp")
            if p["kind"] == "logical":
                artifacts = logical_backup(target, p, control, scratch, s3, catalog.run_prefix(run_id) + "artifacts/")
                source = info["source"]
            else:
                artifacts, source = physical_backup(target, control, scratch)
            state("VerifyingArtifacts", bytes=sum(a["size"] for a in artifacts))
            if not (p["kind"] == "logical" and p["logical"]["transfer"]["mode"] == "direct"):
                state("Uploading")
                for item in artifacts:
                    s3.transfer("put", catalog.run_prefix(run_id) + "artifacts/" + item["fileId"], scratch / item["filename"])
            state("VerifyingRemote")
            for item in artifacts:
                require(s3.remote_hash(catalog.run_prefix(run_id) + "artifacts/" + item["fileId"]) == (item["sha256"], item["size"]),
                        "ARTIFACT_HASH_MISMATCH", "Remote artifact does not match original bytes", 6)
            manifest = {"schemaVersion": 1, **catalog.scope, "runId": run_id, "kind": p["kind"],
                        "serverMajor": target["serverMajor"], "architecture": platform.machine(),
                        "startedAt": started, "completedAt": now(), "tools": info["tools"], "source": source,
                        "sourceEndpoint": {"host": target["host"], "port": target["port"]},
                        "configDigest": digest(config), "artifacts": artifacts,
                        "trigger": os.environ.get("PGBK_TRIGGER") or "manual",
                        "scheduledAt": job_info.get("scheduledAt"),
                        "durationSeconds": (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds(),
                        "bytes": sum(a["size"] for a in artifacts),
                        "workerImage": os.environ.get("PGBK_WORKER_IMAGE"),
                        "restoreVerification": {"status": "not-run"}}
            source_lease.confirm()
            catalog.publish(manifest, repository)
            # Publication is authoritative. A later event/log failure cannot retract it.
            try:
                state("Completed", bytes=sum(a["size"] for a in artifacts))
            except Exception:
                print(json.dumps({"phase": "Completed", "runId": run_id, "reasonCode": "POST_PUBLICATION_EVENT_FAILED"}), file=sys.stderr, flush=True)
            return {"schemaVersion": 1, "status": "Completed", "runId": run_id, "manifest": manifest}
    except Exception as error:
        e = error if isinstance(error, Error) else Error(
            "SCRATCH_IO_FAILED" if isinstance(error, OSError) else "BACKUP_EXECUTION_FAILED",
            "Backup failed; sensitive diagnostics redacted", 5, errorType=type(error).__name__)
        terminal = "SkippedBusy" if e.exit_code == 4 else "Cancelled" if e.exit_code == 7 else "Failed"
        print(json.dumps({"schemaVersion": 1, "phase": terminal, "failedPhase": phase,
                          "runId": run_id, "profileId": profile_name, "sourceId": target["sourceId"],
                          "durationSeconds": (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds(),
                          "reasonCode": e.code}), file=sys.stderr, flush=True)
        try:
            # All child process/connection contexts have terminated before reaching here.
            from .process import Control
            s3.control = Control(30)
            catalog.event(run_id, f"{sequence + 1:06d}", {"phase": terminal, "failedPhase": phase,
                          "reasonCode": e.code, "writerStopped": True, **job_info})
        except Exception:
            pass
        raise e from None
    finally:
        # Generic ephemeral PVCs are owned by the Pod; existing claims are never deleted.
        if not p["scratch"].get("retainFailed") or phase == "Completed":
            shutil.rmtree(scratch, ignore_errors=True)
