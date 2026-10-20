import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from . import __version__
from .catalog import Catalog
from .config import canonical, load, profile
from .errors import Error, require
from .kube import Kubernetes
from .postgres import preflight
from .process import Control, install_signals
from .restore import drill, load_destination, load_destination_json, logical_restore, physical_restore
from .runner import backup, repository_lease
from .storage import S3


class Parser(argparse.ArgumentParser):
    def error(self, message):
        if "--output" in sys.argv and "json" in sys.argv:
            print(json.dumps({"schemaVersion": 1, "status": "failed", "reasonCode": "INVALID_ARGUMENTS", "message": "Invalid arguments; consult --help"}), file=sys.stderr)
        else:
            print("INVALID_ARGUMENTS: Invalid arguments; consult --help", file=sys.stderr)
        raise SystemExit(2)


def parser():
    root = Parser(prog="pgbk", description="PostgreSQL backups using pg_dump/pg_basebackup and WAL-G st. Artifacts use pg_restore or physical extraction, never wal-g backup-fetch.")
    root.add_argument("--version", action="version", version="pgbk " + __version__)
    root.add_argument("--config", default="/config/config.json")
    root.add_argument("--namespace", default=os.environ.get("PGBK_NAMESPACE", "backups"))
    root.add_argument("--context")
    root.add_argument("--timeout", type=int, default=21600)
    root.add_argument("--output", choices=["text", "json"], default="text")
    root.add_argument("--scratch", default=os.environ.get("PGBK_SCRATCH", "/scratch"))
    commands = root.add_subparsers(dest="command", required=True)
    cfg = commands.add_parser("config").add_subparsers(dest="operation", required=True)
    cfg.add_parser("validate")
    pf = commands.add_parser("preflight")
    pf.add_argument("--profile", required=True)
    backups = commands.add_parser("backup").add_subparsers(dest="operation", required=True)
    for operation in ["run", "list", "describe", "download", "cancel"]:
        item = backups.add_parser(operation)
        item.add_argument("--profile", required=True)
        if operation == "run":
            item.add_argument("--release", default="pgbk")
            item.add_argument("--local", action="store_true", help="Run directly using projected Secrets and Kubernetes leases")
        if operation in ["describe", "download"]:
            item.add_argument("--run-id", required=True)
        if operation == "download":
            item.add_argument("--destination", required=True)
            item.add_argument("--database")
            item.add_argument("--destination-pvc", help="Run a download Job into an explicitly labelled dedicated claim")
            item.add_argument("--acknowledge-dedicated-pvc", action="store_true")
            item.add_argument("--release", default="pgbk")
        if operation == "cancel":
            item.add_argument("--job", required=True)
    def restore_arguments(item, operation, worker=False):
        item.add_argument("--run-id", required=True)
        if operation == "logical":
            item.add_argument("--database", required=True)
            item.add_argument("--destination-json" if worker else "--destination-config", required=True)
        else:
            item.add_argument("--pgdata", required=True)
            item.add_argument("--major", required=True, type=int)
            item.add_argument("--tablespace-map", default="{}", help="JSON mapping from OID to absolute destination directory")
            item.add_argument("--acknowledge-isolated-stopped", action="store_true")
            item.add_argument("--destination-pvc")
        if not worker:
            item.add_argument("--as-job", action="store_true", help="Use the configured opt-in restore namespace and ServiceAccount")
            item.add_argument("--release", default="pgbk")
    restoration = commands.add_parser("restore").add_subparsers(dest="operation", required=True)
    for operation in ["logical", "physical"]:
        item = restoration.add_parser(operation)
        item.add_argument("--profile", required=True)
        restore_arguments(item, operation)
    verification = commands.add_parser("verify")
    verification.add_argument("--profile", required=True)
    verification.add_argument("--run-id")
    verification.add_argument("--release", default="pgbk")
    retention = commands.add_parser("retention").add_subparsers(dest="operation", required=True)
    item = retention.add_parser("run")
    item.add_argument("--profile", required=True)
    item.add_argument("--release", default="pgbk")
    item.add_argument("--local", action="store_true")
    workers = commands.add_parser("worker", help=argparse.SUPPRESS).add_subparsers(dest="operation", required=True)
    for operation in ["backup", "preflight", "retention", "verify", "download", "restore-logical", "restore-physical"]:
        item = workers.add_parser(operation)
        item.add_argument("--profile", required=True)
        if operation.startswith("restore-"):
            restore_arguments(item, operation.removeprefix("restore-"), worker=True)
        if operation == "verify":
            item.add_argument("--run-id")
        if operation == "download":
            item.add_argument("--destination-pvc", required=True)
            item.add_argument("--run-id", required=True)
            item.add_argument("--destination", required=True)
            item.add_argument("--database")
    return root


def execute(args):
    config = load(args.config)
    if args.command == "config":
        return {"schemaVersion": 1, "status": "valid", "profiles": sorted(config["profiles"])}
    p, target, storage = profile(config, args.profile)
    require(args.timeout > 0, "INVALID_TIMEOUT", "Timeout must be positive")
    restore_worker = args.command == "worker" and args.operation.startswith("restore-")
    restore_operation = args.operation.removeprefix("restore-") if restore_worker else args.operation if args.command == "restore" else None
    if restore_worker:
        require(p["restore"]["enabled"], "RESTORE_JOBS_DISABLED", "Restore Jobs are disabled", 3)
    if args.command == "worker" and args.operation == "verify":
        require(p["verification"]["enabled"], "VERIFICATION_DISABLED", "Verification is disabled", 3)
    operation_limit = p["restore"]["timeoutSeconds"] if restore_worker else p["verification"]["timeoutSeconds"] if args.command == "worker" and args.operation == "verify" else p["execution"]["activeDeadlineSeconds"]
    control = Control(min(args.timeout, operation_limit))
    install_signals(control)
    kube = Kubernetes(args.namespace, args.context, config.get("coordinationNamespace"))
    if args.command == "backup" and args.operation == "run" and not args.local:
        return kube.launch(args.profile, "backup", args.release)
    if args.command == "backup" and args.operation == "download" and args.destination_pvc:
        require(Path(args.destination).is_absolute() and Path("/destination") in Path(args.destination).parents,
                "UNSAFE_DESTINATION", "Job download path must be below /destination")
        require(args.destination_pvc in p["download"]["allowedDestinationClaims"], "DOWNLOAD_PVC_NOT_ALLOWED", "Download PVC must be explicitly allowlisted", 3)
        kube.destination_claim(args.destination_pvc, config["installationId"], args.acknowledge_dedicated_pvc)
        parameters = ["--run-id", args.run_id, "--destination", args.destination, "--destination-pvc", args.destination_pvc]
        if args.database:
            parameters += ["--database", args.database]
        return kube.launch(args.profile, "download", args.release, parameters, args.destination_pvc)
    if args.command == "backup" and args.operation == "cancel":
        job = kube.batch.read_namespaced_job(args.job, args.namespace, _request_timeout=10)
        labels = job.metadata.labels or {}
        require(labels.get("pgbk.io/profile") == args.profile and labels.get("pgbk.io/installation") == config["installationId"],
                "UNOWNED_JOB", "Cancellation only accepts a Job owned by this installation and profile", 3)
        return kube.cancel(args.job)
    if args.command == "retention" and not args.local:
        return kube.launch(args.profile, "retention", args.release)
    if args.command == "verify":
        verification = p["verification"]
        require(verification["enabled"], "VERIFICATION_DISABLED", "Verification must be explicitly configured", 3)
        kube.namespace = verification["namespace"]
        params = ["--run-id", args.run_id] if args.run_id else None
        return kube.launch(args.profile, "verify", args.release, params)
    if args.command == "restore" and (args.as_job or getattr(args, "destination_pvc", None)):
        settings = p["restore"]
        require(settings["enabled"], "RESTORE_JOBS_DISABLED", "Restore Jobs require explicit profile configuration", 3)
        kube.namespace = settings["namespace"]
        parameters = ["--run-id", args.run_id]
        claim = None
        if args.operation == "logical":
            destination = load_destination(args.destination_config)
            refs = [destination["credentialsSecretRef"], *[destination["tls"][key] for key in ["caSecretRef", "clientSecretRef"] if key in destination["tls"]]]
            require(all(ref in settings["credentialsSecretRefs"] for ref in refs), "RESTORE_SECRET_NOT_ALLOWED", "Destination Secret references must be explicitly mounted in restore Jobs", 3)
            parameters += ["--database", args.database, "--destination-json", canonical(destination).decode()]
        else:
            claim = args.destination_pvc
            require(claim in settings["allowedDestinationClaims"], "RESTORE_PVC_NOT_ALLOWED", "Restore requires an explicitly allowlisted destination PVC", 3)
            require(args.acknowledge_isolated_stopped, "DESTINATION_ACK_REQUIRED", "Stopped isolated destination acknowledgement is required", 3)
            require(Path("/destination") in Path(args.pgdata).parents, "UNSAFE_DESTINATION", "Restore PGDATA must be below /destination")
            kube.check_scratch({"type": "existingClaim", "claimName": claim}, config["installationId"], purpose="restore")
            parameters += ["--pgdata", args.pgdata, "--major", str(args.major), "--tablespace-map", args.tablespace_map,
                           "--destination-pvc", claim, "--acknowledge-isolated-stopped"]
        return kube.launch(args.profile, "restore-" + args.operation, args.release, parameters, claim)
    if restore_worker:
        require(p["restore"]["enabled"] and kube.namespace == p["restore"]["namespace"],
                "RESTORE_NAMESPACE_REQUIRED", "Restore worker requires the configured isolated namespace", 3)
        kube.check_scratch(p["restore"]["scratch"], config["installationId"])
        if restore_operation == "physical":
            require(args.destination_pvc in p["restore"]["allowedDestinationClaims"], "RESTORE_PVC_NOT_ALLOWED", "Destination PVC must be allowlisted", 3)
            kube.check_scratch({"type": "existingClaim", "claimName": args.destination_pvc}, config["installationId"], purpose="restore")
    if args.command == "worker" and args.operation == "download":
        require(args.destination_pvc in p["download"]["allowedDestinationClaims"], "DOWNLOAD_PVC_NOT_ALLOWED", "Download PVC must be allowlisted", 3)
        kube.check_scratch({"type": "existingClaim", "claimName": args.destination_pvc}, config["installationId"], purpose="download")
    if not restore_worker and not (args.command == "worker" and args.operation == "verify"):
        kube.check_scratch(p["scratch"], config["installationId"])
    scratch_root = Path(args.scratch)
    scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (args.command == "worker" and args.operation == "backup") or (args.command == "backup" and args.operation == "run"):
        return backup(config, args.profile, kube, control, scratch_root)
    s3 = S3(storage, control)
    catalog = Catalog(s3, config, args.profile)
    if args.command == "preflight" or (args.command == "worker" and args.operation == "preflight"):
        with repository_lease(kube, catalog, control):
            work = Path(tempfile.mkdtemp(prefix="preflight-", dir=scratch_root))
            try:
                result = preflight(target, p, control, work)
                key = catalog.root + "events/preflight/" + uuid.uuid4().hex + ".json"
                s3.put(key, canonical({"schemaVersion": 1, "operation": "preflight"}))
                require(s3.get(key) is not None, "S3_READ_UNVERIFIED", "Cannot read the S3 probe", 3)
                s3.delete(key)
                return {"schemaVersion": 1, **result}
            finally:
                shutil.rmtree(work)
    if args.command == "worker" and args.operation == "verify":
        return drill(config, args.profile, catalog, kube, control, scratch_root, args.run_id)
    with repository_lease(kube, catalog, control) as lease:
        if args.command == "worker" and args.operation == "download":
            require(Path("/destination") in Path(args.destination).parents, "UNSAFE_DESTINATION", "Job destination must be under /destination")
            manifest = catalog.download(args.run_id, args.destination, args.database)
            return {"schemaVersion": 1, "status": "downloaded", "runId": args.run_id, "destination": args.destination}
        if args.command == "backup":
            if args.operation == "list":
                result = catalog.list()
                for manifest in result["backups"]:
                    manifest["restoreVerification"] = catalog.verification_status(manifest["runId"])
                result["diagnosticRuns"] = [kube.reconcile(event) for event in result["diagnosticRuns"]]
                return result
            if args.operation == "describe":
                result = catalog.describe(args.run_id)
                result["restoreVerification"] = catalog.verification_status(args.run_id)
                return result
            if args.operation == "download":
                manifest = catalog.download(args.run_id, args.destination, args.database)
                return {"schemaVersion": 1, "status": "downloaded", "runId": args.run_id,
                        "destination": args.destination, "artifacts": [a for a in manifest["artifacts"]
                          if args.database is None or a.get("database") == args.database]}
        if args.command in ["retention", "worker"] and args.operation == "retention" or args.command == "retention":
            report_id = uuid.uuid4().hex
            try:
                result = catalog.cleanup(p["retention"], lease)
                catalog.event("cleanup", report_id, result)
                return result
            except Error as e:
                try:
                    catalog.event("cleanup", report_id, e.report())
                except Error:
                    pass
                raise
        if args.command == "restore" or restore_worker:
            work = Path(tempfile.mkdtemp(prefix="restore-", dir=scratch_root))
            try:
                manifest = catalog.download(args.run_id, work / "archives",
                                            args.database if restore_operation == "logical" else None)
                if restore_operation == "logical":
                    return logical_restore(config, manifest, work / "archives", args.database,
                                           load_destination_json(args.destination_json) if restore_worker else load_destination(args.destination_config), control, work)
                try:
                    mappings = json.loads(args.tablespace_map)
                    require(isinstance(mappings, dict) and all(re.fullmatch("[0-9]+", oid) and isinstance(path, str) for oid, path in mappings.items()),
                            "INVALID_TABLESPACE_MAP", "Tablespace map must map OIDs to absolute paths")
                except ValueError:
                    raise Error("INVALID_TABLESPACE_MAP", "Cannot parse tablespace mapping", 2) from None
                if restore_worker:
                    require(Path("/destination") in Path(args.pgdata).parents and
                            all(Path("/destination") in Path(path).parents for path in mappings.values()),
                            "UNSAFE_DESTINATION", "Restore Job paths must be below /destination")
                return physical_restore(manifest, work / "archives", Path(args.pgdata), mappings, control,
                                        args.major, args.acknowledge_isolated_stopped)
            finally:
                shutil.rmtree(work)
    raise Error("UNSUPPORTED_OPERATION", "Unsupported operation", 3)


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
        if args.output == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif "backups" in result:
            for item in result["backups"]:
                print(f"{item['runId']}  {item['kind']}  {item['completedAt']}  verification={item['restoreVerification']['status']}")
            for item in result["invalidRuns"]:
                print(f"{item['runId']}  unusable  {item['reasonCode']}")
            for item in result["diagnosticRuns"]:
                print(f"{item['runId']}  {item.get('phase', 'Unknown')}  {item.get('reasonCode', '')}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Error as error:
        report = error.report()
        if (args.command == "preflight" or (args.command == "worker" and args.operation == "preflight")) and error.exit_code in [3, 8]:
            report.update(status="unsupported", reasonCodes=[error.code])
        print(json.dumps(report) if args.output == "json" else f"{error.code}: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code)
    except KeyboardInterrupt:
        raise SystemExit(7)
    except Exception as error:
        result = {"schemaVersion": 1, "status": "failed", "reasonCode": "EXECUTION_FAILED",
                  "message": "Operation failed; sensitive diagnostics redacted", "errorType": type(error).__name__}
        print(json.dumps(result), file=sys.stderr)
        raise SystemExit(5)


if __name__ == "__main__":
    main()
