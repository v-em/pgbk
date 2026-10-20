"""Disposable real-tool integration; run inside a matching-major worker image.

Kubernetes Lease API is replaced here only. Test real Leases in tests/kubernetes.
The fixture harness may provision sources; the product never provisions them.
"""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

from pgbk.catalog import Catalog
from pgbk.config import normalize
from pgbk.errors import Error
from pgbk.postgres import identity, logical_backup
from pgbk.process import Control
from pgbk.restore import drill, logical_restore, physical_restore, server, logical_archive
from pgbk.runner import backup, repository_lease
from pgbk.storage import S3


class FixtureLease:
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def confirm(self):
        pass


class FixtureKube:
    namespace = "verification"
    def lease(self, *args):
        return FixtureLease()
    def check_scratch(self, *args):
        pass


def expect_error(fn, code=None):
    try:
        fn()
    except Error as e:
        if code:
            assert e.code == code, e.report()
        return e.code
    raise AssertionError("Operation unexpectedly succeeded")


def main():
    work = Path(tempfile.mkdtemp(prefix="pgbk-integration-"))
    reports = []
    started = time.monotonic()
    control = Control(1800)
    kube = FixtureKube()
    try:
        secrets = work / "secrets"
        for ref, values in {"postgres": {"username": "postgres", "password": "synthetic-canary"},
                            "restricted": {"username": "restricted", "password": "synthetic-canary"},
                            "s3": {"accessKeyId": "fixture-access", "secretAccessKey": "fixture-secret-only"}}.items():
            (secrets / ref).mkdir(parents=True)
            for name, value in values.items():
                (secrets / ref / name).write_text(value)
        os.environ["PGBK_SECRET_ROOT"] = str(secrets)
        pgdata = work / "source"
        subprocess.run(["initdb", "-D", str(pgdata), "--auth=trust"], check=True, stdout=subprocess.DEVNULL)
        values = {
            "installationId": "test-" + uuid.uuid4().hex[:12],
            "storage": {"archive": {"bucket": "pgbk-test", "prefix": "integration", "endpoint": os.environ.get("PGBK_TEST_S3", "http://host.docker.internal:19000"),
                                      "region": "us-east-1", "credentialsSecretRef": "s3"}},
            "targets": {"fixture": {"sourceId": "fixture", "concurrencyKey": "fixture", "host": "127.0.0.1", "port": 15499,
                                      "serverMajor": int(os.environ.get("PG_MAJOR", 17)), "credentialsSecretRef": "restricted", "tls": {"mode": "disable"}}},
            "profiles": {"logical": {"target": "fixture", "kind": "logical", "storage": "archive", "serviceAccounts": {"create": True},
                                       "logical": {"databases": ["fixture", "second db=literal"]},
                                       "execution": {"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}},
                                       "scratch": {"type": "emptyDir", "size": "2Gi"}}}}
        mode=os.environ.get("PGBK_TEST_MODE", "staged")
        logical=values["profiles"]["logical"]["logical"]
        logical.update(format="directory" if mode=="directory" else "custom", jobs=2 if mode=="directory" else 1,
                       transfer={"mode":"direct" if mode=="direct" else "staged", "partSizeMiB":5},
                       compression={"method":os.environ.get("PGBK_TEST_COMPRESSION","gzip")})
        if logical["compression"]["method"] in ["gzip","zstd"]: logical["compression"]["level"]=3
        with server(pgdata, control, work):
            for _ in range(60):
                try:
                    with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname="postgres"):
                        break
                except psycopg.OperationalError:
                    control.stop.wait(0.5)
            else:
                raise AssertionError("Fixture server failed to start")
            cancellation = Control(0.5)
            cancel_start = time.monotonic()
            def sleeping_query():
                with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname="postgres") as conn, cancellation.connection(conn):
                    conn.execute("SELECT pg_sleep(20)")
            expect_error(sleeping_query, "DEADLINE_EXCEEDED")
            assert time.monotonic() - cancel_start < 5
            reports.append({"case": "blocking-libpq-query-deadline-cancellation", "status": "passed"})
            with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname="postgres", autocommit=True) as conn:
                for command in ['CREATE ROLE restricted LOGIN', 'CREATE DATABASE fixture', 'CREATE DATABASE "second db=literal"']:
                    conn.execute(command)
                tablespace = work / "source-tablespace"
                tablespace.mkdir()
                conn.execute(sql.SQL("CREATE TABLESPACE fixture_space LOCATION {}").format(sql.Literal(str(tablespace))))
            for db in ["fixture", "second db=literal"]:
                with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname=db, autocommit=True) as conn:
                    conn.execute("CREATE TABLE public.items(id integer PRIMARY KEY, value text)")
                    conn.execute("INSERT INTO public.items SELECT i,md5(i::text) FROM generate_series(1,10000) i")
                    conn.execute("CREATE TABLE public.secret(id integer)")
                    conn.execute("CREATE TABLE public.tablespace_items(id integer) TABLESPACE fixture_space")
                    conn.execute("INSERT INTO public.tablespace_items VALUES(73)")
                    conn.execute('CREATE TABLE public."literal.*?[] quote"".Юникод"(id integer)')
                    conn.execute("CREATE SCHEMA reporting")
                    conn.execute("CREATE TABLE reporting.parent(id integer,month integer) PARTITION BY RANGE(month)")
                    conn.execute("CREATE TABLE reporting.jan PARTITION OF reporting.parent FOR VALUES FROM(1) TO(2)")
                    conn.execute("CREATE TABLE reporting.feb PARTITION OF reporting.parent FOR VALUES FROM(2) TO(3)")
                    conn.execute("INSERT INTO reporting.parent VALUES(42,1),(43,2)")
                    conn.execute("GRANT USAGE ON SCHEMA public,reporting TO restricted")
                    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public,reporting TO restricted")
            if mode == "direct":
                # More than one 5-MiB part even after gzip, in the real S3 emulator.
                with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname="fixture", autocommit=True) as conn:
                    conn.execute("CREATE TABLE stream_payloads AS SELECT g, md5(g::text) AS payload FROM generate_series(1,400000) g")
                    conn.execute("GRANT SELECT ON stream_payloads TO restricted")
            config = normalize(values)
            result = backup(config, "logical", kube, Control(600), work)
            catalog = Catalog(S3(config["storage"]["archive"], control), config, "logical")
            assert catalog.describe(result["runId"])["artifacts"][1]["database"] == "second db=literal"
            reports.append({"case": "multi-database-restricted-backup-s3", "status": "passed"})
            if mode == "direct":
                assert result["manifest"]["artifacts"][0]["size"] > 5*1024**2
                assert not catalog.s3.client.list_multipart_uploads(Bucket="pgbk-test", Prefix=catalog.run_prefix(result["runId"])).get("Uploads")
                reports.append({"case":"real-multipart-direct-complete","status":"passed"})
            downloaded = work / "downloaded"
            manifest = catalog.download(result["runId"], downloaded)
            for i, item in enumerate(manifest["artifacts"]):
                with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname="postgres", autocommit=True) as conn:
                    conn.execute(f"CREATE DATABASE restored_{i}")
                with logical_archive(item, downloaded, work, control) as archive:
                    subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-h", "127.0.0.1", "-p", "15499",
                                    "-U", "postgres", "-d", f"restored_{i}", str(archive)], check=True)
                with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname=f"restored_{i}") as conn:
                    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 10000
            reports.append({"case": "native-logical-restore", "status": "passed"})
            selection = {"mode": "selected", "include": {"tables": [{"schema": "reporting", "table": "parent"}],
                "query": 'SELECT \'public\'::text AS schema_name, \'literal.*?[] quote".Юникод\'::text AS table_name'},
                "exclude": {"tables": [{"schema": "reporting", "table": "feb"}]}}
            filtered = copy.deepcopy(values)
            filtered["profiles"]["logical"]["logical"]["databases"] = ["fixture"]
            filtered["profiles"]["logical"]["logical"]["tableSelection"] = {"byDatabase": {"fixture": selection}}
            filtered_config = normalize(filtered)
            selected = backup(filtered_config, "logical", kube, Control(600), work)
            effective = selected["manifest"]["artifacts"][0]["selection"]["effective"]
            assert {(r["schema"], r["table"]) for r in effective} == {("reporting", "parent"), ("reporting", "jan"), ("public", 'literal.*?[] quote".Юникод')}
            reports.append({"case": "snapshot-query-exact-pattern-partition-exclusion", "status": "passed"})
            all_mode = copy.deepcopy(filtered)
            all_mode["profiles"]["logical"]["logical"]["tableSelection"]["byDatabase"]["fixture"] = {"mode": "all", "exclude": {"tables": [{"schema": "public", "table": "secret"}]}}
            backup(normalize(all_mode), "logical", kube, Control(600), work)
            reports.append({"case": "all-mode-exact-exclusion", "status": "passed"})
            empty = copy.deepcopy(filtered)
            empty["profiles"]["logical"]["logical"]["tableSelection"]["byDatabase"]["fixture"]["include"] = {"query": "SELECT 'public'::text AS schema_name,'items'::text AS table_name WHERE false"}
            expect_error(lambda: backup(normalize(empty), "logical", kube, Control(600), work), "EMPTY_TABLE_SELECTION")
            cap = copy.deepcopy(filtered)
            cap["profiles"]["logical"]["logical"]["tableSelection"].update(maxQueryRows=1)
            cap["profiles"]["logical"]["logical"]["tableSelection"]["byDatabase"]["fixture"]["include"] = {"query": "SELECT 'public'::text AS schema_name,'items'::text AS table_name FROM generate_series(1,2)"}
            expect_error(lambda: backup(normalize(cap), "logical", kube, Control(600), work), "QUERY_ROW_LIMIT")
            reports.append({"case": "empty-selection-and-raw-row-overflow-fail-closed", "status": "passed"})
            with psycopg.connect(host="127.0.0.1", port=15499, user="postgres", dbname="fixture", autocommit=True) as conn:
                conn.execute("ALTER TABLE secret ENABLE ROW LEVEL SECURITY")
            expect_error(lambda: backup(config, "logical", kube, Control(600), work))
            reports.append({"case": "restricted-rls-fails-without-marker", "status": "passed"})
            physical_values = copy.deepcopy(values)
            physical_values["targets"]["fixture"]["credentialsSecretRef"] = "postgres"
            physical_values["targets"]["fixture"]["maintenanceDatabase"] = "postgres"
            physical_values["targets"]["fixture"]["expectedSystemIdentifier"] = identity(normalize(physical_values)["targets"]["fixture"])["systemIdentifier"]
            physical_values["targets"]["fixture"]["physicalEndpoint"] = "direct-primary"
            physical_values["profiles"]["physical"] = copy.deepcopy(physical_values["profiles"]["logical"])
            physical_values["profiles"]["physical"].pop("logical")
            physical_values["profiles"]["physical"]["kind"] = "physical-full"
            physical_config = normalize(physical_values)
            physical_result = backup(physical_config, "physical", kube, Control(600), work)
            physical_catalog = Catalog(S3(physical_config["storage"]["archive"], control), physical_config, "physical")
            physical_download = work / "physical-download"
            physical_manifest = physical_catalog.download(physical_result["runId"], physical_download)
        # Original server stopped; restoring physical bytes must not use it.
        restored = work / "physical-restore"
        mappings = {oid: str(work / ("restored-tablespace-" + oid)) for oid in physical_manifest["source"]["tablespaceOids"]}
        assert mappings
        report = physical_restore(physical_manifest, physical_download, restored, mappings, control, physical_manifest["serverMajor"], True)
        assert report["status"] == "prepared"
        with server(restored, control, work):
            for _ in range(60):
                try:
                    with psycopg.connect(host=str(work), port=15499, user="postgres", dbname="fixture") as conn:
                        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 10000
                        assert conn.execute("SELECT id FROM tablespace_items").fetchone()[0] == 73
                    break
                except psycopg.OperationalError:
                    control.stop.wait(0.5)
            else:
                raise AssertionError("Restored server did not start")
        reports.append({"case": "s3-physical-offline-restore", "status": "passed"})
        destination_data = work / "logical-destination"
        subprocess.run(["initdb", "-D", str(destination_data), "--auth=trust"], check=True, stdout=subprocess.DEVNULL)
        destination = {"host": "127.0.0.1", "port": 15500, "serverMajor": config["targets"]["fixture"]["serverMajor"],
                       "credentialsSecretRef": "postgres", "tls": {"mode": "disable"}, "acknowledgeIsolated": True}
        with server(destination_data, control, work, port=15500):
            for _ in range(60):
                try:
                    with psycopg.connect(host="127.0.0.1", port=15500, user="postgres", dbname="postgres"):
                        break
                except psycopg.OperationalError:
                    control.stop.wait(0.5)
            else:
                raise AssertionError("Logical destination did not start")
            for i, item in enumerate(manifest["artifacts"]):
                destdb = f"isolated_{i}"
                with psycopg.connect(host="127.0.0.1", port=15500, user="postgres", dbname="postgres", autocommit=True) as conn:
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(destdb)))
                destination["database"] = destdb
                assert logical_restore(config, manifest, downloaded, item["database"], destination, control, work)["status"] == "completed"
                with psycopg.connect(host="127.0.0.1", port=15500, user="postgres", dbname=destdb) as conn:
                    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 10000
                expect_error(lambda: logical_restore(config, manifest, downloaded, item["database"], destination, control, work), "DESTINATION_NOT_EMPTY")
            bad_destination = {**destination, "port": 15499}
            expect_error(lambda: logical_restore(config, manifest, downloaded, manifest["artifacts"][0]["database"], bad_destination, control, work), "RESTORE_SOURCE_REFUSED")
        reports.append({"case": "product-logical-offline-restore-empty-and-source-guards", "status": "passed"})
        verification = {"enabled": True, "schedule": {"enabled": False}, "namespace": "verification",
                        "image": "example/worker@sha256:" + "0" * 64,
                        "resources": values["profiles"]["logical"]["execution"]["resources"],
                        "scratch": {"type": "emptyDir", "size": "2Gi"}, "timeoutSeconds": 600,
                        "credentialsSecretRef": "postgres", "clientCredentialsSecretRef": "postgres",
                        "assertions": ["SELECT NOT pg_is_in_recovery()"]}
        config["profiles"]["logical"]["verification"] = verification
        physical_config["profiles"]["physical"]["verification"] = verification
        assert drill(config, "logical", catalog, kube, Control(600), work, result["runId"])["status"] == "passed"
        assert drill(physical_config, "physical", physical_catalog, kube, Control(600), work, physical_result["runId"])["status"] == "passed"
        reports.append({"case": "logical-and-physical-automated-offline-drills", "status": "passed"})
        print(json.dumps({"schemaVersion": 1, "postgresMajor": values["targets"]["fixture"]["serverMajor"], "logicalMode": mode, "compression": logical["compression"], "cases": reports,
                          "tools": physical_result["manifest"]["tools"], "fixture": "10000-items-two-databases-partitions-tablespace",
                          "runIds": {"logical": result["runId"], "filtered": selected["runId"], "physical": physical_result["runId"]},
                          "physicalBytes": physical_result["manifest"]["bytes"], "elapsedSeconds": time.monotonic() - started,
                          "kubernetes": "stubbed; test separately", "s3": "Moto emulator", "catalogPrefix": catalog.root}))
    finally:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
