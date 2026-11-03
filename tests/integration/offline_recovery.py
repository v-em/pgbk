"""Run in a fresh --network=none worker with downloaded /archives mounted read-only.

Stop the original minikube executor first. This exercises downloaded native
artifacts without S3, Kubernetes, the old source or any projected credentials.
"""
import json
import hashlib
import tempfile
import time
from pathlib import Path

import psycopg
from psycopg import sql

from pgbk.process import Control
from pgbk.restore import logical_archive, physical_restore, server
from pgbk.storage import hash_file


def main():
    # Linux may expose inactive tunnel devices even in Docker's "none" network.
    assert not Path("/proc/net/route").read_text().splitlines()[1:], "External IPv4 routes are present"
    for interface in Path("/sys/class/net").iterdir():
        if interface.is_dir() and interface.name != "lo":
            assert not int((interface / "flags").read_text(), 16) & 1, "An external interface is up"
    import pgbk
    source_hash = hashlib.sha256()
    for path in sorted(Path(pgbk.__file__).parent.glob("*")):
        if path.suffix in [".py", ".json", ".txt"]:
            source_hash.update(path.name.encode() + b"\0" + path.read_bytes())
    control = Control(1200)
    checks = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="recovery-", dir="/scratch") as temporary:
        root = Path(temporary)
        for kind in ["logical", "physical"]:
            archives = Path("/archives") / kind
            manifest = json.loads((archives / "manifest.json").read_text())
            for artifact in manifest["artifacts"]:
                assert hash_file(archives / artifact["filename"]) == (artifact["sha256"], artifact["size"])
            work = root / kind
            work.mkdir(mode=0o700)
            pgdata = work / "pgdata"
            if kind == "logical":
                control.run(["initdb", "-D", str(pgdata), "--auth=trust", "--username=postgres"], capture=True)
            else:
                mappings = {oid: str(work / ("tablespace-" + oid)) for oid in manifest["source"]["tablespaceOids"]}
                physical_restore(manifest, archives, pgdata, mappings, control, 18, True)
            with server(pgdata, control, work):
                for _ in range(120):
                    control.check()
                    try:
                        with psycopg.connect(host=str(work), port=15499, user="postgres", dbname="postgres", autocommit=True) as conn:
                            if kind == "logical":
                                for item in manifest["artifacts"]:
                                    if item.get("database") and item["database"] != "postgres":
                                        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(item["database"])))
                        break
                    except psycopg.OperationalError:
                        time.sleep(0.5)
                else:
                    raise AssertionError("Independent PostgreSQL failed to start")
                if kind == "logical":
                    for item in manifest["artifacts"]:
                        if not item.get("database"):
                            continue
                        with logical_archive(item, archives, work, control) as archive:
                            control.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--no-tablespaces",
                                         "-h", str(work), "-p", "15499", "-U", "postgres", "-d", item["database"], str(archive)])
                with psycopg.connect(host=str(work), port=15499, user="postgres", dbname="fixture") as conn:
                    assert conn.info.server_version // 10000 == 18
                    assert conn.execute("SELECT count(*) FROM items").fetchone() == (1000,)
                    assert conn.execute("SELECT count(*) FROM items WHERE value=md5(id::text)").fetchone() == (1000,)
                    assert conn.execute("SELECT nextval('items_id_seq')").fetchone() == (1001,)
                    assert conn.execute("SELECT * FROM reporting.parent ORDER BY id").fetchall() == [(42, 1), (43, 2)]
                    assert conn.execute("SELECT encode(lo_get(oid),'hex') FROM pg_largeobject_metadata").fetchall() == [("cafebabe",)]
                    assert conn.execute("SELECT NOT pg_is_in_recovery()").fetchone() == (True,)
                    assert conn.execute("SELECT count(*) FROM pg_constraint WHERE conrelid='items'::regclass").fetchone()[0] >= 2
                checks.append({"case": kind + "-native-recovery-with-original-executor-source-s3-unavailable",
                               "status": "passed", "runId": manifest["runId"], "rows": 1000,
                               "dataSequencePartitionConstraintsLargeObjects": "passed"})
    print(json.dumps({"schemaVersion": 1, "pgbkVersion": pgbk.__version__, "specificationVersion": "1.3", "status": "passed", "postgresMajor": 18,
                      "execution": "fresh isolated Docker native-restore destination; network none; read-only downloaded artifacts",
                      "originalExecutor": "minikube stopped by host before invocation",
                      "sourceSha256": source_hash.hexdigest(),
                      "s3KubernetesCredentials": "absent", "cases": checks, "elapsedSeconds": time.monotonic() - started}, indent=2))


if __name__ == "__main__":
    main()
