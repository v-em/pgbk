import copy
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import tarfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

from .catalog import now
from .config import canonical, profile, tcp_host
from .credentials import pg_credential_keys, pg_environment, pg_params, secret
from .errors import Error, require
from .postgres import check_tool, extract_physical, sanitize_physical
from .runner import repository_lease
from .selection import validate_query


@contextmanager
def logical_archive(item, archives, scratch, control):
    path = archives / item["filename"]
    if item["format"] == "postgres-custom":
        yield path
        return
    require(item["format"] == "postgres-directory-tar", "WRONG_BACKUP_FORMAT", "Unsupported logical format", 6)
    with tempfile.TemporaryDirectory(prefix="directory-restore-", dir=scratch) as temporary:
        directory = Path(temporary)
        seen = set()
        with tarfile.open(path, "r:") as bundle:
            for member in bundle:
                control.check()
                require(member.isfile() and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", member.name)
                        and member.name.casefold() not in seen,
                        "UNSAFE_ARCHIVE", "Directory dump contains an unsafe or duplicate member", 6)
                seen.add(member.name.casefold())
                with bundle.extractfile(member) as source, (directory / member.name).open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        control.check()
                        target.write(chunk)
        require("toc.dat" in seen, "DIRECTORY_TOC_MISSING", "Directory archive has no toc.dat", 6)
        yield directory


def load_destination(path):
    try:
        return load_destination_json(Path(path).read_text())
    except (OSError, ValueError):
        raise Error("INVALID_DESTINATION", "Cannot read destination JSON", 2) from None


def load_destination_json(data):
    try:
        value = json.loads(data)
    except (ValueError, TypeError):
        raise Error("INVALID_DESTINATION", "Cannot parse destination JSON", 2) from None
    permitted = {"host", "port", "serverMajor", "database", "credentialsSecretRef", "credentialsSecretKeys", "tls",
                 "acknowledgeIsolated", "destinationPolicy", "preserveOwnership"}
    require(isinstance(value, dict) and not set(value) - permitted and
            all(value.get(k) for k in ["host", "serverMajor", "database", "credentialsSecretRef"]),
            "INVALID_DESTINATION", "Destination requires explicit host, major, database and Secret reference")
    require(value.get("acknowledgeIsolated") is True, "DESTINATION_ACK_REQUIRED", "Explicit isolated destination acknowledgement is required", 3)
    require(isinstance(value["credentialsSecretRef"], str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value["credentialsSecretRef"]),
            "INVALID_DESTINATION", "Destination Secret reference is invalid")
    value["credentialsSecretKeys"] = pg_credential_keys(value)
    require(tcp_host(value["host"]) and
            isinstance(value["database"], str) and not any(c in value["database"] for c in "\x00\r\n"),
            "INVALID_DESTINATION", "Destination must use a TCP host and literal database name")
    value.setdefault("port", 5432)
    require(type(value["serverMajor"]) is int and value["serverMajor"] in range(14, 19) and
            type(value["port"]) is int and 1 <= value["port"] <= 65535 and
            type(value.get("preserveOwnership", False)) is bool,
            "INVALID_DESTINATION", "Invalid destination version, port or ownership mode")
    value.setdefault("tls", {"mode": "verify-full"})
    require(isinstance(value["tls"], dict) and set(value["tls"]) <= {"mode", "caSecretRef", "clientSecretRef"} and
            value["tls"].get("mode", "verify-full") in ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"],
            "INVALID_DESTINATION", "Destination TLS options are invalid")
    value["tls"].setdefault("mode", "verify-full")
    for key in ["caSecretRef", "clientSecretRef"]:
        if key in value["tls"]:
            require(isinstance(value["tls"][key], str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value["tls"][key]), "INVALID_DESTINATION", "Invalid TLS Secret reference")
    require(value.get("destinationPolicy", "empty") in ["empty", "prerequisites-only"], "INVALID_DESTINATION", "Unknown destination policy")
    return value


def addresses(host):
    try:
        return {r[4][0] for r in socket.getaddrinfo(host, None)}
    except OSError:
        return set()  # Restoring a downloaded archive must not require a reachable source.


def logical_restore(config, manifest, archives, database, destination, control, scratch):
    require(manifest["kind"] == "logical", "WRONG_BACKUP_KIND", "Logical restore requires a logical backup")
    item = next((a for a in manifest["artifacts"] if a.get("database") == database), None)
    require(item is not None, "DATABASE_NOT_FOUND", "Selected database is absent from backup")
    require(destination["serverMajor"] == manifest["serverMajor"], "RESTORE_MAJOR_MISMATCH", "Destination major must match backup", 3)
    source_endpoints = [manifest["sourceEndpoint"], *config["targets"].values()]
    for source in source_endpoints:
        require(not (source["port"] == destination["port"] and
                     (source["host"] == destination["host"] or addresses(source["host"]) & addresses(destination["host"]))),
                "RESTORE_SOURCE_REFUSED", "Destination matches a configured or detected source endpoint", 3)
    check_tool(control, "pg_restore", manifest["serverMajor"])
    try:
        with psycopg.connect(**pg_params(destination, destination["database"])) as conn, control.connection(conn):
            require(conn.info.server_version // 10000 == manifest["serverMajor"], "RESTORE_MAJOR_MISMATCH", "Actual destination major differs", 3)
            known = manifest.get("source", {}).get(database, {}).get("systemIdentifier")
            detected = None
            try:
                with conn.transaction():
                    detected = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
            except psycopg.Error:
                pass
            require(not (known and known == detected), "RESTORE_SOURCE_REFUSED", "Destination system identifier matches the source", 3)
            user_objects = conn.execute("""SELECT n.nspname,c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg\\_%'
                AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','v','m','S','f')""").fetchall()
            policy = destination.get("destinationPolicy", "empty")
            if (item.get("selection") or {}).get("mode") == "selected":
                known_prerequisites = item["selection"].get("knownPrerequisites", {})
                schemas = {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace").fetchall()}
                types = set(conn.execute("SELECT n.nspname,t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace").fetchall())
                require(all(s in schemas for s in known_prerequisites.get("schemas", [])) and
                        all((t["schema"], t["name"]) in types for t in known_prerequisites.get("types", [])) and
                        all((t["schema"], t["table"]) in set(user_objects) for t in
                            [*known_prerequisites.get("referencedTables", []), *known_prerequisites.get("parents", [])]),
                        "RESTORE_PREREQUISITE_MISSING", "Known schemas, types, parent or referenced tables are missing", 3)
            if policy == "empty":
                funcs = conn.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'""").fetchone()[0]
                require(not user_objects and not funcs, "DESTINATION_NOT_EMPTY", "Logical destination must be empty", 3)
            else:
                require(item.get("scope") == "filtered-database", "INVALID_DESTINATION_POLICY", "Prerequisites-only requires a filtered archive", 3)
                restored_names = {(r["schema"], r["table"]) for r in item["selection"]["effective"]}
                require(not (set(user_objects) & restored_names), "DESTINATION_RELATION_EXISTS", "A selected destination relation already exists", 3)
                if item["selection"]["mode"] == "selected":
                    schemas = {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace").fetchall()}
                    require(all(s in schemas for s, _ in restored_names), "RESTORE_PREREQUISITE_MISSING", "Create selected table schemas before filtered restore", 3)
    except psycopg.Error:
        raise Error("RESTORE_PREFLIGHT_FAILED", "Cannot establish destination identity or emptiness", 3) from None
    work = scratch / uuid.uuid4().hex
    work.mkdir(mode=0o700)
    env = pg_environment(destination, destination["database"], work)
    argv = ["pg_restore", "--exit-on-error", "--no-password", "--dbname=" + destination["database"]]
    # --dbname uses libpq connstring expansion; pass literal dbname only through PGDATABASE.
    argv[-1] = "--dbname=" + psycopg.conninfo.make_conninfo(dbname=destination["database"])
    if not destination.get("preserveOwnership"):
        argv += ["--no-owner", "--no-acl", "--no-tablespaces"]
    try:
        with logical_archive(item, archives, work, control) as archive:
            control.run(argv + [str(archive)], env=env)
    except Error as e:
        raise Error("RESTORE_PARTIAL", "Restore failed; destination may contain partial objects and must be re-emptied", e.exit_code,
                    cause=e.code) from None
    finally:
        shutil.rmtree(work)
    return {"schemaVersion": 1, "status": "completed", "runId": manifest["runId"], "database": database,
            "scope": item["scope"], "destinationIdentity": "detected" if detected else "operator-acknowledged"}


def physical_restore(manifest, archives, pgdata, tablespace_map, control, expected_major, isolated):
    require(isolated, "DESTINATION_ACK_REQUIRED", "A stopped isolated destination acknowledgement is required", 3)
    require(manifest["kind"] == "physical-full", "WRONG_BACKUP_KIND", "Physical restore requires a physical backup")
    require(expected_major == manifest["serverMajor"], "RESTORE_MAJOR_MISMATCH", "Destination major must match backup", 3)
    arch = platform.machine()
    require(manifest["architecture"] == arch, "RESTORE_ARCHITECTURE_MISMATCH", "Physical restore architecture differs", 3)
    check_tool(control, "pg_verifybackup", expected_major)
    extract_physical(archives, pgdata, tablespace_map)
    require((Path(pgdata) / "PG_VERSION").read_text().strip() == str(expected_major),
            "RESTORE_MAJOR_MISMATCH", "PGDATA major differs from manifest", 6)
    control.run(["pg_verifybackup", str(pgdata)])
    changed = sanitize_physical(Path(pgdata))
    return {"schemaVersion": 1, "status": "prepared", "runId": manifest["runId"],
            "major": expected_major, "changedFiles": changed, "serverStarted": False,
            "prerequisites": ["Compatible PostgreSQL build and extension binaries", "Operator-owned isolated deployment"]}


@contextmanager
def server(pgdata, control, work, port=15499):
    logfile = (work / "server.log").open("wb")
    proc = subprocess.Popen(["postgres", "-D", str(pgdata), "-c", "unix_socket_directories=" + str(work),
                             "-c", "listen_addresses=127.0.0.1", "-c", "port=" + str(port)],
                            stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile, start_new_session=True)
    try:
        yield proc
    finally:
        # Always stop the detached server before releasing its repository pin, including cancellation.
        if proc.poll() is None:
            import signal
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        logfile.close()


def drill(config, profile_name, catalog, kube, control, scratch_root, run_id=None):
    p, target, _ = profile(config, profile_name)
    verification = p["verification"]
    require(verification["enabled"], "VERIFICATION_DISABLED", "Verification must be explicitly configured", 3)
    require(kube.namespace == verification["namespace"], "VERIFICATION_NAMESPACE_REQUIRED", "Drills run only in the configured verification namespace", 3)
    kube.check_scratch(verification["scratch"], config["installationId"])
    report_id = uuid.uuid4().hex
    with repository_lease(kube, catalog, control):
        if run_id is None:
            runs = catalog.list()["backups"]
            require(runs, "NO_COMPLETED_BACKUP", "No completed backup is available", 3)
            run_id = runs[0]["runId"]
        manifest = catalog.describe(run_id)
        report = {"schemaVersion": 1, "runId": run_id, "verificationId": report_id,
                  "startedAt": now(), "status": "failed", "image": verification["image"], "assertions": []}
        work = Path(tempfile.mkdtemp(prefix="drill-", dir=scratch_root))
        try:
            archives = work / "archives"
            catalog.download(run_id, archives)
            pgdata = work / "pgdata"
            username = secret(verification["clientCredentialsSecretRef"], "username")
            if manifest["kind"] == "logical":
                require(username == secret(verification["credentialsSecretRef"], "username"),
                        "VERIFICATION_CREDENTIAL_MISMATCH", "Drill server and client usernames must match", 3)
                password_file = work / "initdb-password"
                password_file.write_text(secret(verification["credentialsSecretRef"], "password"))
                password_file.chmod(0o600)
                control.run(["initdb", "-D", str(pgdata), "--auth-local=trust", "--auth-host=scram-sha-256",
                             "--username=" + username, "--pwfile=" + str(password_file)])
                password_file.unlink()
            else:
                mappings = {oid: str(work / ("tablespace-" + oid)) for oid in manifest["source"]["tablespaceOids"]}
                physical_restore(manifest, archives, pgdata, mappings, control, manifest["serverMajor"], True)
            with server(pgdata, control, work) as proc:
                conn = None
                while conn is None:
                    control.check()
                    require(proc.poll() is None, "VERIFICATION_STARTUP_FAILED", "Disposable PostgreSQL failed to start", 5)
                    try:
                        conn = psycopg.connect(host=str(work), port=15499, user=username, dbname="postgres", autocommit=True, connect_timeout=2)
                    except psycopg.Error:
                        control.stop.wait(0.5)
                with conn, control.connection(conn):
                    require(conn.info.server_version // 10000 == manifest["serverMajor"], "RESTORE_MAJOR_MISMATCH", "Drill server major differs", 3)
                    if manifest["kind"] == "logical":
                        databases = [a["database"] for a in manifest["artifacts"] if a.get("database")]
                        for database in databases:
                            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
                    else:
                        databases = [r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate").fetchall()]
                checks = []
                for database in databases:
                    if manifest["kind"] == "logical":
                        item = next(a for a in manifest["artifacts"] if a.get("database") == database)
                        if item.get("selection") and item["selection"]["mode"] == "selected":
                            with psycopg.connect(host=str(work), port=15499, user=username, dbname=database, autocommit=True) as dest, control.connection(dest):
                                for schema in {r["schema"] for r in item["selection"]["effective"]} - {"public"}:
                                    dest.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                        env = {"PATH": os.environ["PATH"], "PGHOST": str(work), "PGPORT": "15499", "PGUSER": username, "PGDATABASE": database}
                        with logical_archive(item, archives, work, control) as archive:
                            control.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--no-tablespaces",
                                         "--dbname=" + psycopg.conninfo.make_conninfo(dbname=database), str(archive)], env=env)
                    with psycopg.connect(host=str(work), port=15499, user=username, dbname=database) as restored, control.connection(restored):
                        restored.execute("SET TRANSACTION READ ONLY")
                        restored.execute("SELECT set_config('statement_timeout','30000',true)")
                        checks.append({"database": database, "catalogCounts": restored.execute("""SELECT
                          (SELECT count(*) FROM pg_namespace), (SELECT count(*) FROM pg_class WHERE relkind IN ('r','p')),
                          (SELECT count(*) FROM pg_index), (SELECT count(*) FROM pg_constraint),
                          (SELECT count(*) FROM pg_class WHERE relkind='S'), (SELECT count(*) FROM pg_largeobject_metadata)""").fetchone()})
                        tables = restored.execute("""SELECT n.nspname,c.relname FROM pg_class c
                          JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p')
                          AND n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'""").fetchall()
                        checks[-1]["tableRowCounts"] = []
                        for schema, table in tables:
                            count = restored.execute(sql.SQL("SELECT count(*) FROM ONLY {}").format(sql.Identifier(schema, table))).fetchone()[0]
                            checks[-1]["tableRowCounts"].append({"schema": schema, "table": table, "rows": count})
                        if manifest["kind"] == "logical" and item.get("selection"):
                            actual_names = set(tables)
                            selected_names = {(r["schema"], r["table"]) for r in item["selection"]["effective"]}
                            excluded_names = {(r["schema"], r["table"]) for r in item["selection"]["excluded"]}
                            require(selected_names <= actual_names and not actual_names & excluded_names,
                                    "VERIFICATION_SCOPE_MISMATCH", "Restored table scope differs from the selected archive", 6)
                        for assertion in verification.get("assertions", []):
                            validate_query(assertion)
                            rows = restored.execute(assertion).fetchmany(2)
                            require(rows == [(True,)], "VERIFICATION_ASSERTION_FAILED", "Read-only assertion must return one true boolean", 5)
                            report["assertions"].append({"database": database, "digest": __import__("hashlib").sha256(assertion.encode()).hexdigest(), "passed": True})
                report.update(status="passed", checks=checks, sourceConnections=0,
                              tools={tool: check_tool(control, tool, manifest["serverMajor"])
                                     for tool in ["postgres", "pg_restore", "pg_verifybackup"]})
        except Exception as e:
            report["reasonCode"] = e.code if isinstance(e, Error) else "VERIFICATION_FAILED"
            raise
        finally:
            report["finishedAt"] = now()
            # A fresh reporting budget allows a failure/cancellation report without resuming work.
            from .process import Control
            old = catalog.s3.control
            catalog.s3.control = Control(30)
            try:
                catalog.s3.put(catalog.root + "verification/" + run_id + "/" + report_id + ".json", canonical(report))
            finally:
                catalog.s3.control = old
                if report["status"] == "passed" or not verification["scratch"].get("retainFailed"):
                    shutil.rmtree(work)
        return report
