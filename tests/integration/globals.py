"""Isolated PostgreSQL globals regression; run in a matching worker, network=none."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg

from pgbk import __version__
from pgbk.errors import Error
from pgbk.postgres import logical_backup
from pgbk.process import Control
from pgbk.restore import server


class MemoryArtifacts:
    def __init__(self):
        self.objects = {}

    def upload_stream(self, key, chunks, part_size):
        data = b"".join(chunks)
        self.objects[key] = data
        return hashlib.sha256(data).hexdigest(), len(data)

    def object_chunks(self, key):
        yield self.objects[key]


class RecordingControl(Control):
    def __init__(self):
        super().__init__(90)
        self.tools = []

    def run(self, argv, **kwargs):
        self.tools.append(argv[0])
        return super().run(argv, **kwargs)

    def output(self, argv, **kwargs):
        self.tools.append(argv[0])
        return super().output(argv, **kwargs)


def main():
    control = Control(90)
    cases = []
    with tempfile.TemporaryDirectory(prefix="globals-regression-", dir="/scratch") as temporary:
        root = Path(temporary)
        pgdata = root / "pgdata"
        control.run(["initdb", "-D", str(pgdata), "--auth=trust", "--username=fixture_admin"])
        with server(pgdata, control, root, port=15499):
            for _ in range(100):
                try:
                    conn = psycopg.connect(host=str(root), port=15499, dbname="postgres", user="fixture_admin", autocommit=True)
                    break
                except psycopg.OperationalError:
                    time.sleep(.1)
            else:
                raise AssertionError("Fixture startup failed")
            with conn:
                conn.execute("CREATE ROLE backup_reader LOGIN")
                conn.execute("CREATE DATABASE application")
                conn.execute('CREATE DATABASE "maintenance db=literal"')
                conn.execute("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC")
                conn.execute("REVOKE CONNECT ON DATABASE template1 FROM PUBLIC")
            with psycopg.connect(host=str(root), port=15499, dbname="application", user="fixture_admin", autocommit=True) as app:
                app.execute("CREATE TABLE items (id integer PRIMARY KEY)")
                app.execute("INSERT INTO items VALUES (1)")
                app.execute("GRANT SELECT ON items TO backup_reader")
            env = {**os.environ, "PGHOST": str(root), "PGPORT": "15499", "PGUSER": "backup_reader",
                   "PGDATABASE": "application", "PGCONNECT_TIMEOUT": "5", "LC_ALL": "C"}
            argv = ["pg_dumpall", "--globals-only", "--no-role-passwords", "--no-password"]
            previous = subprocess.run(argv, env=env, capture_output=True, timeout=20)
            assert previous.returncode == 1 and b"permission denied" in previous.stderr
            cases.append("PGDATABASE alone reproduces failed default-database connection")

            secrets = root / "secrets" / "fixture"
            secrets.mkdir(parents=True)
            (secrets / "username").write_text("backup_reader")
            (secrets / "password").write_text("synthetic-fixture-password")
            os.environ["PGBK_SECRET_ROOT"] = str(secrets.parent)
            target = {"host": "127.0.0.1", "port": 15499, "serverMajor": 18, "credentialsSecretRef": "fixture",
                      "maintenanceDatabase": "maintenance db=literal", "tls": {"mode": "disable"}}
            for mode in ["direct", "staged"]:
                logical = {"databases": ["application"], "globals": "enabled", "format": "custom", "jobs": 1,
                           "transfer": {"mode": mode, "partSizeMiB": 5}, "tableSelection": {"byDatabase": {}}}
                scratch = root / mode
                scratch.mkdir()
                store = MemoryArtifacts()
                execution = RecordingControl()
                items = logical_backup(target, {"logical": logical}, execution, scratch, store, "artifacts/")
                globals_item = next(a for a in items if a.get("scope") == "globals")
                data = (store.objects["artifacts/" + globals_item["fileId"]] if mode == "direct" else
                        (scratch / globals_item["filename"]).read_bytes())
                assert b"CREATE ROLE backup_reader" in data
                assert b"synthetic-fixture-password" not in data
                assert (hashlib.sha256(data).hexdigest(), len(data)) == (globals_item["sha256"], globals_item["size"])
                assert any(a.get("database") == "application" for a in items)
                assert execution.tools.index("pg_dumpall") < execution.tools.index("pg_dump")
                cases.append(mode + ": restricted non-superuser exports globals using literal maintenance database")

                denied = root / (mode + "-denied")
                denied.mkdir()
                execution = RecordingControl()
                try:
                    logical_backup({**target, "maintenanceDatabase": "postgres"}, {"logical": logical},
                                   execution, denied, MemoryArtifacts(), "denied/")
                except Error as error:
                    # pg_dumpall can suppress libpq's connection error when -l
                    # is explicit. Do not infer permissions from generic text.
                    assert error.code in {"ACCESS_DENIED", "TOOL_FAILED"}, error.report()
                    assert error.details["tool"] == "pg_dumpall"
                    assert error.details["toolExitCode"] == 1
                    assert "pg_dump" not in execution.tools
                else:
                    raise AssertionError("Denied globals were accepted")
                cases.append(mode + ": denied globals fail before database dump; native diagnostics remain redacted")
    print(json.dumps({"pgbkVersion": __version__, "status": "passed", "postgresMajor": 18,
                      "environment": "isolated temporary PostgreSQL; network none; synthetic credentials",
                      "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
