import json
import re
import shutil
import tarfile
import uuid
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

import psycopg

from .credentials import pg_environment, pg_params
from .errors import Error, require
from .selection import resolve, validate_toc
from .storage import hash_file


def connection(target, database, replication=False):
    params = pg_params(target, database)
    if replication:
        params["replication"] = "true"
    else:
        params["options"] = "-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=30000"
    try:
        return psycopg.connect(**params, autocommit=replication)
    except psycopg.Error:
        raise Error("POSTGRES_CONNECTION_FAILED", "PostgreSQL connection, TLS or authentication failed", 3) from None


def major(conn):
    return conn.info.server_version // 10000


def identity(target, control=None):
    with connection(target, target["maintenanceDatabase"], replication=True) as conn, (control.connection(conn) if control else nullcontext()):
        row = conn.execute("IDENTIFY_SYSTEM").fetchone()
        require(major(conn) == target["serverMajor"], "SERVER_MAJOR_MISMATCH", "Unexpected physical server major", 3)
        def text(value):
            return value.decode("ascii") if isinstance(value, bytes) else str(value)
        return {"systemIdentifier": text(row[0]), "timeline": int(row[1]), "lsn": text(row[2])}


def check_tool(control, tool, expected):
    version = control.run([tool, "--version"], capture=True).strip()
    match = re.search(r"\(PostgreSQL\) (\d+)", version)
    require(match and int(match[1]) == expected, "CLIENT_MAJOR_MISMATCH", "PostgreSQL client must match source major", 3)
    return version


def preflight(target, profile, control, scratch):
    test = scratch / ("write-test-" + uuid.uuid4().hex)
    try:
        test.write_bytes(b"pgbk")
        test.unlink()
    except OSError:
        raise Error("SCRATCH_UNWRITABLE", "Scratch storage is not writable", 3) from None
    tools = ["pg_dump", "pg_restore"] if profile["kind"] == "logical" else ["pg_basebackup", "pg_verifybackup"]
    if profile["kind"] == "logical" and profile["logical"]["globals"] == "enabled":
        tools.append("pg_dumpall")
    versions = {tool: check_tool(control, tool, target["serverMajor"]) for tool in tools}
    versions["wal-g"] = control.run(["wal-g", "--version"], capture=True).strip()
    if profile["kind"] == "physical-full":
        current = identity(target, control)
        require(current["systemIdentifier"] == target["expectedSystemIdentifier"],
                "SOURCE_IDENTITY_MISMATCH", "Physical system identifier differs from explicit configuration", 3)
        with connection(target, target["maintenanceDatabase"]) as conn, control.connection(conn):
            require(not conn.execute("SELECT pg_is_in_recovery()").fetchone()[0],
                    "PHYSICAL_NOT_PRIMARY", "Physical endpoint must be the intended primary", 3)
        return {"status": "unverified", "reasonCodes": ["BASEBACKUP_CAPACITY_UNVERIFIED"], "source": current, "tools": versions}
    metadata = {}
    for database in profile["logical"]["databases"]:
        with connection(target, database) as conn, control.connection(conn):
            require(major(conn) == target["serverMajor"], "SERVER_MAJOR_MISMATCH", "Unexpected logical server major", 3)
            metadata[database] = {"serverVersion": conn.info.server_version, "systemIdentifier": None,
                                  "extensions": None, "encodingLocale": None}
            for key, query in {
                "systemIdentifier": "SELECT system_identifier::text FROM pg_control_system()",
                "extensions": "SELECT extname,extversion FROM pg_extension ORDER BY extname",
                "encodingLocale": "SELECT pg_encoding_to_char(encoding),datcollate,datctype FROM pg_database WHERE datname=current_database()"
            }.items():
                try:
                    with conn.transaction():
                        rows = conn.execute(query).fetchall()
                        metadata[database][key] = rows[0][0] if key == "systemIdentifier" else rows
                except psycopg.Error:
                    pass  # Optional metadata is unknown; never deny an otherwise valid dump.
    return {"status": "unverified", "reasonCodes": ["FULL_EXPORT_PRIVILEGES_UNVERIFIED"], "source": metadata, "tools": versions}


def artifact(path, format_name, **fields):
    sha256, size = hash_file(path)
    return {"fileId": uuid.uuid4().hex, "filename": path.name, "format": format_name,
            "sha256": sha256, "size": size, **fields}


def compression_args(logical, server_major):
    compression = logical.get("compression", {"method": "gzip", "level": 6})
    method, level = compression["method"], compression.get("level")
    if server_major < 16:
        return ["--compress=" + str(0 if method == "none" else (level if level is not None else 6))]
    return ["--compress=" + method + (":" + str(level) if level is not None else "")]


def logical_backup(target, profile, control, scratch, s3=None, artifact_prefix=None):
    result = []
    logical = profile["logical"]
    direct = logical.get("transfer", {}).get("mode") == "direct"
    directory = logical.get("format") == "directory"
    require(not direct or (s3 is not None and artifact_prefix), "STREAM_STORAGE_REQUIRED", "Direct backup requires S3")
    for database in logical["databases"]:
        control.check()
        work = scratch / uuid.uuid4().hex
        work.mkdir(mode=0o700)
        env = pg_environment(target, database, work)
        file_id = uuid.uuid4().hex
        path = scratch / (file_id + (".directory.tar" if directory else ".dump"))
        dump_path = work / "directory" if directory else path
        argv = ["pg_dump", "--format=" + ("directory" if directory else "custom"),
                "--no-password", "--lock-wait-timeout=30000", *compression_args(logical, target["serverMajor"])]
        if not direct:
            argv += ["--file=" + str(dump_path)]
        if directory:
            argv += ["--jobs=" + str(logical.get("jobs", 1))]
        streamed = None
        def dump(extra):
            nonlocal streamed
            if direct:
                key = artifact_prefix + file_id
                with control.output(argv + extra, env=env) as chunks:
                    streamed = s3.upload_stream(key, chunks, logical["transfer"]["partSizeMiB"] * 1024**2)
                return control.inspect_input(["pg_restore", "--list"], s3.object_chunks(key))
            control.run(argv + extra, env=env)
            return control.run(["pg_restore", "--list", str(dump_path)], capture=True)
        rule = logical["tableSelection"]["byDatabase"].get(database)
        info = None
        if rule:
            try:
                with connection(target, database) as conn, control.connection(conn):
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    args, info = resolve(conn, rule, logical["tableSelection"])
                    toc = dump(args)
                    validate_toc(toc, info)
            except psycopg.Error:
                raise Error("SELECTION_FAILED", "Snapshot, relation lock or selection query failed (SQL redacted)", 3) from None
        else:
            toc = dump([])
        inventory = {}
        for name, expression in {
            "tables": r"^\d+; 1259 \d+ TABLE (?!ATTACH )",
            "indexes": r"^\d+; 1259 \d+ INDEX ",
            "sequences": r"^\d+; 1259 \d+ SEQUENCE (?!SET |OWNED BY )",
            "constraints": r"^\d+; \d+ \d+ (?:FK )?CONSTRAINT "
        }.items():
            inventory[name] = len(re.findall(expression, toc, re.M))
        if directory:
            with tarfile.open(path, "w") as bundle:
                for member in sorted(dump_path.iterdir()):
                    control.check()
                    require(member.is_file() and not member.is_symlink(), "UNSAFE_ARCHIVE", "Unexpected directory dump member", 6)
                    class CheckedReader:
                        def __init__(self, source): self.source = source
                        def read(self, size):
                            control.check()
                            return self.source.read(size)
                    with member.open("rb") as source:
                        bundle.addfile(bundle.gettarinfo(str(member), arcname=member.name), CheckedReader(source))
        item = ({"fileId": file_id, "filename": path.name, "format": "postgres-custom",
                 "sha256": streamed[0], "size": streamed[1]} if direct else
                artifact(path, "postgres-directory-tar" if directory else "postgres-custom"))
        item.update(database=database, scope="filtered-database" if rule else "full-database",
                    selection=info, archiveInventory=inventory,
                    compression=logical.get("compression", {"method": "gzip", "level": 6}),
                    dumpJobs=logical.get("jobs", 1), transferMode="direct" if direct else "staged")
        result.append(item)
        shutil.rmtree(work)
    if logical["globals"] == "enabled":
        work = scratch / uuid.uuid4().hex
        work.mkdir(mode=0o700)
        env = pg_environment(target, target["maintenanceDatabase"], work)
        path = scratch / "globals.sql"
        argv = ["pg_dumpall", "--globals-only", "--no-role-passwords", "--no-password"]
        if direct:
            file_id = uuid.uuid4().hex
            with control.output(argv, env=env) as chunks:
                sha, size = s3.upload_stream(artifact_prefix + file_id, chunks, logical["transfer"]["partSizeMiB"] * 1024**2)
            result.append({"fileId": file_id, "filename": path.name, "format": "postgres-globals-sql",
                           "scope": "globals", "sha256": sha, "size": size})
        else:
            control.run(argv + ["--file=" + str(path)], env=env)
            result.append(artifact(path, "postgres-globals-sql", scope="globals"))
        shutil.rmtree(work)
    return result


def extract_tar(path, destination, tablespace_oids=()):
    """Never delegate untrusted archive paths to tar.extractall."""
    with tarfile.open(path, "r:") as tar:
        for item in tar:
            name = PurePosixPath(item.name)
            require(not name.is_absolute() and ".." not in name.parts and "\\" not in item.name and "\x00" not in item.name,
                    "UNSAFE_ARCHIVE_PATH", "Physical archive has an unsafe path", 6)
            if str(name) == ".":
                continue
            if item.issym() and len(name.parts) == 2 and name.parts[0] == "pg_tblspc" and name.parts[1] in tablespace_oids:
                continue  # Rebuilt solely from an explicit destination mapping.
            require(item.isfile() or item.isdir(), "UNSAFE_ARCHIVE_LINK", "Physical archive has an unapproved link or special file", 6)
            output = destination.joinpath(*name.parts)
            require(not any(p.is_symlink() for p in [output, *output.parents]),
                    "UNSAFE_ARCHIVE_LINK", "Extraction would follow a symlink", 6)
            if item.isdir():
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                require(not output.exists(), "ARCHIVE_DUPLICATE_PATH", "Physical archive contains a repeated file", 6)
                with tar.extractfile(item) as stream, output.open("xb") as target:
                    shutil.copyfileobj(stream, target, 1024 * 1024)
                output.chmod(0o600)


def extract_physical(archives, pgdata, tablespace_map):
    pgdata = Path(pgdata)
    require(not pgdata.is_symlink() and (not pgdata.exists() or not any(pgdata.iterdir())),
            "DESTINATION_NOT_EMPTY", "Physical PGDATA must be a separate empty directory", 3)
    require(not any(p.is_symlink() for p in pgdata.parents), "UNSAFE_DESTINATION", "Destination parents cannot be symlinks", 3)
    oids = {p.stem for p in archives.glob("*.tar") if p.stem.isdigit()}
    require(set(tablespace_map) == oids, "TABLESPACE_MAPPING_REQUIRED", "Every tablespace requires an explicit mapping", 3)
    destinations = [pgdata, *(Path(p) for p in tablespace_map.values())]
    for i, path in enumerate(destinations):
        require(path.is_absolute() and not path.is_symlink() and (not path.exists() or not any(path.iterdir())),
                "DESTINATION_NOT_EMPTY", "Physical destinations must be absolute empty directories", 3)
        require(not any(p.is_symlink() for p in path.parents), "UNSAFE_DESTINATION", "Destination parents cannot be symlinks", 3)
        for other in destinations[i + 1:]:
            require(path != other and path not in other.parents and other not in path.parents,
                    "UNSAFE_TABLESPACE_MAPPING", "Physical destinations cannot overlap", 3)
    pgdata.mkdir(parents=True, mode=0o700, exist_ok=True)
    pgdata.chmod(0o700)
    require((archives / "base.tar").is_file() and (archives / "pg_wal.tar").is_file() and
            (archives / "backup_manifest").is_file(), "PHYSICAL_SET_INCOMPLETE", "Physical artifact set lacks data, WAL or manifest", 6)
    extract_tar(archives / "base.tar", pgdata, oids)
    (pgdata / "pg_wal").mkdir(exist_ok=True, mode=0o700)
    extract_tar(archives / "pg_wal.tar", pgdata / "pg_wal")
    (pgdata / "pg_tblspc").mkdir(exist_ok=True, mode=0o700)
    for oid, destination in tablespace_map.items():
        destination = Path(destination)
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        extract_tar(archives / (oid + ".tar"), destination)
        (pgdata / "pg_tblspc" / oid).symlink_to(destination)
    shutil.copyfile(archives / "backup_manifest", pgdata / "backup_manifest")


def sanitize_physical(pgdata):
    changed = []
    for filename in ["standby.signal", "recovery.signal", "recovery.conf", "postmaster.pid", "postmaster.opts"]:
        path = pgdata / filename
        if path.exists():
            path.unlink()
            changed.append(filename)
    replacements = {
        "postgresql.auto.conf": "# Source recovery settings removed by pgbk\n",
        "postgresql.conf": "listen_addresses = '127.0.0.1'\narchive_mode = off\narchive_command = ''\nrestore_command = ''\nprimary_conninfo = ''\nprimary_slot_name = ''\nhot_standby = off\n",
        "pg_hba.conf": "local all all trust\nhost all all 127.0.0.1/32 scram-sha-256\n",
        "pg_ident.conf": "# No source identity mappings\n"}
    for filename, contents in replacements.items():
        (pgdata / filename).write_text(contents)
        changed.append(filename)
    return changed


def physical_backup(target, control, scratch):
    before = identity(target, control)
    work = scratch / "basebackup"
    auth = scratch / "physical-auth"
    auth.mkdir(mode=0o700)
    env = pg_environment(target, target["maintenanceDatabase"], auth)
    control.run(["pg_basebackup", "--format=tar", "--wal-method=stream", "--checkpoint=fast",
                 "--manifest-checksums=SHA256", "--no-password", "--pgdata=" + str(work)], env=env)
    after = identity(target, control)
    require(before["systemIdentifier"] == after["systemIdentifier"] == target["expectedSystemIdentifier"] and
            before["timeline"] == after["timeline"], "SOURCE_IDENTITY_CHANGED", "Physical source identity or timeline changed", 6)
    upstream = json.loads((work / "backup_manifest").read_text())
    wal = upstream.get("WAL-Ranges", [])
    require(wal and all(r["Timeline"] == before["timeline"] for r in wal),
            "SOURCE_TIMELINE_CHANGED", "Backup WAL ranges do not match the source timeline", 6)
    validation = scratch / "validation"
    tablespaces = {p.stem: str(scratch / ("tablespace-" + p.stem)) for p in work.glob("*.tar") if p.stem.isdigit()}
    extract_physical(work, validation, tablespaces)
    control.run(["pg_verifybackup", str(validation)])
    shutil.rmtree(validation)
    for path in tablespaces.values():
        shutil.rmtree(path)
    result = []
    for path in sorted(work.iterdir()):
        require(path.name == "backup_manifest" or path.suffix == ".tar", "PHYSICAL_FORMAT_UNSUPPORTED", "Unexpected physical output format", 6)
        result.append(artifact(path, "postgres-manifest" if path.name == "backup_manifest" else "postgres-tar"))
        shutil.move(path, scratch / path.name)
    shutil.rmtree(work)
    shutil.rmtree(auth)
    return result, {**before, "endLsn": after["lsn"], "walRanges": wal, "tablespaceOids": sorted(tablespaces)}
