# Complete pgbk recovery procedure

This guide covers pgbk 0.3.2 and artifact format v1, from choosing a backup through application validation. Commands are examples for a separate destination; replace paths, database names, users and versions. They are not intended for the live source.

## 1. Choose a recovery scenario and understand its scope

| Backup → destination | Procedure |
| --- | --- |
| Logical → local PostgreSQL | Verify files → prepare an empty database → restore roles if needed → pg_restore → validate. |
| Logical → managed PostgreSQL | Provision the cluster/users through the provider → check extensions → portable pg_restore → assign supported privileges → validate. |
| Physical → local PostgreSQL/VM/container | Prepare a compatible environment → verify and extract the entire cluster → pg_verifybackup → replace settings → start in isolation → validate. |
| Physical → managed PostgreSQL | Recover a compatible staging server → pg_dump the required databases → restore logically into managed. Direct PGDATA import is generally unavailable and is not implemented by pgbk. |

“Local” means a server under your control, including a VM, Docker or Kubernetes. In a managed service, the provider controls PostgreSQL's filesystem. A logical source can be managed or self-hosted; consider source and destination restrictions independently.

`pgbk restore` and the offline helper require the same PostgreSQL major as `manifest.json`; pgbk currently covers 14–18. A manual logical transfer to a newer major is a separate migration requiring compatibility validation; downgrades are outside this procedure. The client must support the archive's format/compression. Physical recovery requires the same major, compatible OS/build/architecture and extension binaries. Linux amd64 PGDATA cannot run in native PostgreSQL on macOS/ARM: use a compatible Linux VM/container, with emulation if needed, or stage a logical export.

Logical copies contain only the listed databases and selected scope. Different databases do not share a single consistent snapshot. A `filtered-database` archive is not a complete database and may require external prerequisites. Physical recovery restores the entire cluster, including databases, roles and password hashes. pgbk does not provide continuous WAL archiving/PITR here. The operator provisions service configuration, extensions, external certificates and integrations.

## 2. Choose, download and verify a backup

Check backup completion status and its restore verification report. A successful backup and a successful restore drill are separate results. To include roles in a logical run, enable this before taking the backup:

```yaml
profiles:
  example-logical:
    logical:
      databases: [application]
      globals: enabled
```

The default is `globals: disabled`. When enabled, `pg_dumpall --globals-only --no-role-passwords` exports a separate `globals.sql`: roles, memberships and global objects including tablespaces, without role passwords. Export permissions are required. Roles are not embedded in each database archive. Enabling the setting later does not change an existing copy without globals; obtain roles from another trusted source or use portable restore.

```sh
export PGBK_ARCHIVES=/srv/pgbk-restore/download
export PGBK_SOURCE=/opt/pgbk-source

pgbk --config values.local.yaml --namespace backups backup list --profile example-logical
pgbk --config values.local.yaml --namespace backups backup describe --profile example-logical --run-id RUN_ID
pgbk --config values.local.yaml --namespace backups backup download --profile example-logical --run-id RUN_ID --destination "$PGBK_ARCHIVES"
```

For physical copies, substitute the physical profile. The download directory must be new/empty. Omit `--database` when downloading a full logical run: that filter selects only one database archive, excluding globals. For subset downloads use `DOWNLOAD.json.downloadedFileIds`; the complete manifest can list files that were not downloaded.

A download contains `manifest.json`, `DOWNLOAD.json`, archives, short generic `RESTORE*.txt` notes and copy-specific `BACKUP-RESTORE.md` / `BACKUP-RESTORE.ru.md` instructions. For new backups the latter are stored in S3 before publication of the completion marker; their hashes/sizes are in `manifest.json.recoveryInstructions`. These companion files live under `recovery/`, separately from PostgreSQL files in `artifacts`. Full downloads return the frozen bytes. For older copies or single-database downloads, notes are rendered from the manifest and downloaded IDs; their origin and resulting hashes are recorded in `DOWNLOAD.json`. The original backup manifest is unchanged.

`pgbk backup download` checks hashes. Verify again after transferring files to another computer; this streaming example requires neither S3 nor Kubernetes:

```sh
python3 - "$PGBK_ARCHIVES" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
download_path = root / "DOWNLOAD.json"
download = json.loads(download_path.read_text()) if download_path.exists() else {}
assert download.get("runId", manifest["runId"]) == manifest["runId"]
artifacts = manifest["artifacts"]
ids = set(download.get("downloadedFileIds", [a["fileId"] for a in artifacts]))
assert ids and ids <= {a["fileId"] for a in artifacts}
records = [a for a in artifacts if a["fileId"] in ids]
records += download.get("recoveryInstructions", manifest.get("recoveryInstructions", []))
for item in records:
    name = item["filename"]
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name), name
    path = root / name
    assert path.is_file() and not path.is_symlink(), name
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    assert (digest.hexdigest(), path.stat().st_size) == (item["sha256"], item["size"]), name
    print("OK", name, item.get("database", item.get("scope", "instructions")))
print("runId:", manifest["runId"], "kind:", manifest["kind"], "major:", manifest["serverMajor"])
PY
```

Do not use `python -O` for examples with assertions. Hashes establish agreement with a trusted manifest, not authenticity of a separately modified file set. If `DOWNLOAD.json` is missing, this example requires every manifest file. Keep the original download unchanged; store logs and adapted SQL separately. Once downloaded, native recovery tools need neither the source server, S3 nor Kubernetes. The helper requires the installed pgbk Python package/dependencies plus `scripts/restore_download.py` from the source distribution; the wheel alone does not install that script.

## 3. Prepare a logical archive and destination

Find the artifact by `database` in the manifest, rather than guessing its filename. pgbk archive names are normally opaque IDs. Check `format`, `scope`, `selection`, `archiveInventory` and available `source.<database>.extensions` / `encodingLocale`. Unknown metadata does not establish that no extensions exist. Install required extension binaries on self-hosted servers; for managed services check available versions and the provider's activation procedure.

Pass a `postgres-custom` file directly to pg_restore. A `postgres-directory-tar` file wraps a native directory dump; it is not pg_dump's tar format. Extract it to a new directory first, never pass the bundle directly to pg_restore. For example, after verifying the outer hash:

```sh
export PGBK_DUMP="$PGBK_ARCHIVES/ARCHIVE_FILE_FROM_MANIFEST"
export PGBK_LOGICAL_DIRECTORY=/srv/pgbk-restore/directory

python3 - "$PGBK_DUMP" "$PGBK_LOGICAL_DIRECTORY" <<'PY'
import re
import shutil
import sys
import tarfile
from pathlib import Path

destination = Path(sys.argv[2])
assert destination.is_absolute() and not destination.exists()
assert not any(p.is_symlink() for p in destination.parents)
destination.mkdir(mode=0o700)
seen = set()
with tarfile.open(sys.argv[1], "r:") as bundle:
    for member in bundle:
        assert member.isfile() and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", member.name)
        assert member.name.casefold() not in seen
        seen.add(member.name.casefold())
        with bundle.extractfile(member) as source, (destination / member.name).open("xb") as target:
            shutil.copyfileobj(source, target)
assert "toc.dat" in seen
PY

export PGBK_DUMP="$PGBK_LOGICAL_DIRECTORY"
pg_restore --list "$PGBK_DUMP"
```

Use that block only for directory bundles. For custom format, set `PGBK_DUMP` to the archive file and run `pg_restore --list`. After an extraction failure, use another new directory.

For `filtered-database`, prepare missing schemas, types, functions, extensions, FK targets and partition parents according to `selection.knownPrerequisites` before restoring; recursive dependency information can be incomplete. Do not precreate the tables being restored. Precreating an extension may create definitions already in the TOC; reconcile this with archive contents. For native restore, prepare a reviewed `pg_restore --list` / `--use-list` when necessary, excluding only definitions already provisioned while keeping required data/dependencies. In pgbk, `destinationPolicy: prerequisites-only` is available only for filtered archives.

## 4. Logical → local PostgreSQL

### 4.1. Portable restore without source roles

Use this for local testing or destinations with different users. The example assumes an existing local administrator named `postgres`. Provision a password file beforehand with mode `0600`, using `host:port:database:user:password` and escaping `:` and `\` in values. Do not supply passwords in command arguments or connection URIs.

```sh
export PGHOST=127.0.0.1
export PGPORT=5432
export PGUSER=postgres
export PGPASSFILE=/secure/restore.pgpass
export PGBK_DEST_DB=restored_application

psql -X --set=ON_ERROR_STOP=on --dbname=postgres --command='SELECT version(), current_user, inet_server_addr(), inet_server_port();'
createdb --template=template0 --owner="$PGUSER" "$PGBK_DEST_DB"
pg_restore --exit-on-error --no-owner --no-acl --no-tablespaces --dbname="$PGBK_DEST_DB" "$PGBK_DUMP"
```

Add explicit source encoding/locale options to `createdb` when needed and available on the destination. Otherwise template0 defaults apply, which need not match the source. Do not use an existing populated database. The flags omit source ownership, GRANT/REVOKE and tablespace placement. Restored objects belong to the restoring role; references to roles in RLS policies, SQL code, SECURITY DEFINER functions and external connections still require review. `--no-owner --no-acl` do not rewrite those references.

### 4.2. Preserve source roles, ownership and ACLs

1. Confirm that globals exist and were downloaded. Use a separate cluster with administrative privileges.
2. Make a working copy of `globals.sql`. Reconcile existing roles, including the bootstrap administrator `postgres`; omit conflicting CREATE ROLE statements and separately decide which ALTER ROLE statements are appropriate. Do not automatically turn managed provider roles into privileged local roles.
3. Retain the required application-role memberships/attributes. For tablespaces, provision separate destination directories owned by the correct OS user and adapt LOCATION in the working SQL. If placement is unnecessary, omit the corresponding tablespace definitions and pass `--no-tablespaces` to pg_restore.
4. Apply reviewed SQL before restoring the database, stopping on errors. Set passwords separately: newly created globals roles cannot authenticate by password until a password is set. Interactive `\password role_name` avoids putting the password in a shell SQL argument.

```sh
psql -X --set=ON_ERROR_STOP=on --dbname=postgres --file=/srv/pgbk-restore/globals.reviewed.sql
```

For a full database whose original name is unused in the separate cluster, native `--create` also restores database creation, its owner, settings and database ACLs:

```sh
pg_restore --exit-on-error --create --dbname=postgres "$PGBK_DUMP"
```

Here `--dbname=postgres` is only the initial maintenance connection; the destination name comes FROM THE ARCHIVE. The command does not use `PGBK_DEST_DB`. Do not add `--clean` automatically. If the original name exists, is `postgres`/`template1`, a rename is required, or the archive is filtered, precreate a separate empty database with the correct owner and use the following command; transfer database properties/ACLs and ALTER ROLE IN DATABASE separately using reviewed SQL:

```sh
pg_restore --exit-on-error --dbname="$PGBK_DEST_DB" "$PGBK_DUMP"
```

Repeat for each saved database. pgbk logical backups do not restore original passwords, external authentication, certificates or infrastructure settings.

## 5. Logical → managed PostgreSQL

1. Create a new cluster/database through supported provider interfaces. Prepare networking, CA, writer endpoint, a restoring user with the necessary privileges and an application user. pgbk has no universal managed-cluster provisioning API.
2. Choose the same major for pgbk recovery. Check encoding/locale, extensions/versions, connection/storage limits, available languages and settings. Enable preload dependencies through provider mechanisms before restoring.
3. Map source application roles to permitted destination roles. Do not execute all globals: SUPERUSER/REPLICATION/BYPASSRLS, service roles, tablespaces and some global GRANT statements may be unavailable. Create users through console/API/SQL as required by the service. Set passwords separately.
4. Connect with TLS verification and restore into the prepared empty database in portable mode:

```sh
export PGHOST=DESTINATION_WRITER_FQDN
export PGPORT=6432
export PGUSER=restore_owner
export PGSSLMODE=verify-full
export PGSSLROOTCERT=/secure/destination-ca.crt
export PGPASSFILE=/secure/managed-restore.pgpass
export PGBK_DEST_DB=restored_application

psql -X --set=ON_ERROR_STOP=on --dbname="$PGBK_DEST_DB" --command='SELECT version(), current_user, current_database();'
pg_restore --exit-on-error --no-owner --no-acl --no-tablespaces --dbname="$PGBK_DEST_DB" "$PGBK_DUMP"
```

Port `6432` is illustrative; use your service's documented endpoint/port, preferably a direct endpoint for administrative operations. If the provider precreates extensions or schemas, reconcile the TOC as in section 3 instead of ignoring all errors. An unavailable extension/language/function can require schema changes; importing unsupported objects successfully is not guaranteed.

5. Explicitly assign application privileges. This example assumes an existing `app_login` role, the `public` schema and execution as the owner of restored objects. Repeat for other schemas/owners. Default privileges affect only future objects created by the defining role; configure them separately for a different migration owner.

```sh
psql -X --set=ON_ERROR_STOP=on --dbname="$PGBK_DEST_DB" --set=target_db="$PGBK_DEST_DB" <<'SQL'
GRANT CONNECT ON DATABASE :"target_db" TO app_login;
GRANT USAGE ON SCHEMA public TO app_login;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_login;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_login;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_login;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_login;
SQL
```

This illustrates an access model, rather than reproducing source ACLs. Review function EXECUTE privileges, large-object access, RLS and other schemas separately. To retain source ACLs, precreate all permitted roles and use reviewed native restore flags; preserving every source privilege may be impossible in managed services. pgbk `preserveOwnership` retains ownership, ACL and tablespace requirements together; use pg_restore to choose those parameters independently.

For Yandex Managed Service for PostgreSQL, follow the [official logical migration procedure](https://yandex.cloud/en/docs/managed-postgresql/tutorials/data-migration). Its native backups and restore API are a separate mechanism documented under [backup management](https://yandex.cloud/en/docs/managed-postgresql/operations/cluster-backups); pgbk files cannot serve as a provider backup ID. Current local pgbk reports do not certify a live managed scenario; validate the particular service and restrictions at recovery time.

## 6. Logical recovery using pgbk

The native commands above do not require Kubernetes after downloading. The pgbk command uses the S3 catalog and Kubernetes coordination:

```sh
pgbk --config values.local.yaml --namespace backups restore logical --profile example-logical --run-id RUN_ID --database application --destination-config /secure/destination.json
```

Here `--database` is the SOURCE database in the manifest. Destination JSON's `database` field names the new destination database, which must exist beforehand. Example for an isolated local TCP server:

```json
{
  "host": "127.0.0.1",
  "port": 5432,
  "serverMajor": 18,
  "database": "restored_application",
  "credentialsSecretRef": "restore-postgres",
  "tls": {"mode": "disable"},
  "acknowledgeIsolated": true,
  "destinationPolicy": "empty",
  "preserveOwnership": false
}
```

For managed services, replace host/port and use TLS `{"mode":"verify-full","caSecretRef":"restore-ca"}`. The restoring user comes from the Secret; JSON contains no password. Use `preserveOwnership: true` only after preparing required roles/ACLs/tablespaces. Globals are never executed automatically. Source/destination endpoint/IP or detected system-identifier matches are refused. `acknowledgeIsolated` acknowledges actual isolation; it does not bypass checks.

The source distribution's offline helper uses the same algorithm, including selected-database hash checks, without S3/Kubernetes. It extracts directory archives automatically. Logical recovery requires writable archives storage for temporary files. Credentials come from a local projection: `username` and `password` files in `$PGBK_SECRET_ROOT/restore-postgres/`, and CA in `$PGBK_SECRET_ROOT/restore-ca/ca.crt`. Prepare these files with restricted access and keep them out of Git.

```sh
export PGBK_SECRET_ROOT=/secure/pgbk-secrets
python3 "$PGBK_SOURCE/scripts/restore_download.py" --archives "$PGBK_ARCHIVES" logical --database application --destination-config /secure/destination.json
```

The Job variant (`--as-job`) requires namespaces, Secrets, resources and scratch beforehand; `127.0.0.1` inside a Job means that Pod itself. Use an isolated endpoint reachable from the Pod. See [operations](operations.md).

## 7. Physical → local PostgreSQL, VM or container

### 7.1. Preparation and extraction

1. Check manifest major, architecture, system identifier, timeline and `source.tablespaceOids`. Download ALL `base.tar`, `pg_wal.tar`, `OID.tar` and `backup_manifest` files and verify hashes. This is an entire separate cluster, not input to pg_restore.
2. Prepare a compatible environment containing PostgreSQL and matching-major `pg_verifybackup`/`pg_waldump`, extension libraries, locales and required preload libraries. Physical recovery cannot cross architectures; the helper also checks architecture.
3. Stop only the dedicated destination; confirm that no other server uses its directories. Use new empty absolute PGDATA and separate nonoverlapping directories for EVERY tablespace. Symlinks in parent paths are refused. Do not run initdb. Allow space for archives, extracted files, WAL and subsequent operations.
4. Run the helper as the non-root OS user that will run PostgreSQL and own its directories. Example without tablespaces:

```sh
export PGBK_PGDATA=/srv/pgbk-restore/pgdata
export PGBK_SOCKET=/srv/pgbk-restore/socket

python3 "$PGBK_SOURCE/scripts/restore_download.py" --archives "$PGBK_ARCHIVES" physical --pgdata "$PGBK_PGDATA" --major 18 --tablespace-map '{}' --acknowledge-isolated-stopped
```

If the backup includes OID `16384`, for example, replace the empty map with a map of every OID. Mapped paths are new destination paths; original LOCATION values are not reused:

```sh
python3 "$PGBK_SOURCE/scripts/restore_download.py" --archives "$PGBK_ARCHIVES" physical --pgdata "$PGBK_PGDATA" --major 18 --tablespace-map '{"16384":"/srv/pgbk-restore/tablespace-16384"}' --acknowledge-isolated-stopped
```

Choose ONE command matching the copy. The helper checks outer hashes, safely extracts data/WAL, recreates only mapped `pg_tblspc/OID` links, places `backup_manifest`, runs `pg_verifybackup` and then replaces source configuration. If it fails, do not start the result; prepare fresh empty directories before retrying.

For manual extraction without the helper, preserve this order: outer hash verification → safe base extraction → WAL into PGDATA/pg_wal → separate tablespace directories and explicit links → upstream backup_manifest → ownership/mode 0700 → pg_verifybackup → configuration replacement. Do not allow arbitrary tar links/path traversal; unchecked `tar -xf` is insufficient. Do not modify data files or `backup_label`, or remove required WAL. Do not use `--no-parse-wal` to bypass a missing-WAL error. Verify BEFORE changing configuration and starting the server: after startup, the original backup_manifest no longer describes working files.

### 7.2. Configuration and first startup

Helper output `status: prepared` means prepared files, not a running database. pgbk removes source recovery/standby signal files and stale PID/options, replaces postgresql.conf, postgresql.auto.conf, pg_hba.conf and pg_ident.conf, and disables source archive/restore commands and primary_conninfo/slot. Generated settings are minimal: deliberately supply required compatibility settings and `shared_preload_libraries`. Do not restore source replication, external subscriptions/FDW/user mappings or scheduler jobs without reviewing them; isolate the staging server's network.

Generated HBA includes `local all all trust`, intended for a dedicated OS user and private socket in an isolated environment. Before opening networking, configure production authentication, TLS, HBA and secrets. For the first check, disable TCP and use a private socket directory, running every command as the same non-root OS user:

```sh
chmod 700 "$PGBK_PGDATA"
mkdir -m 700 "$PGBK_SOCKET"
pg_ctl -D "$PGBK_PGDATA" -l /srv/pgbk-restore/server.log -o "-c listen_addresses='' -c unix_socket_directories=$PGBK_SOCKET -p 55432" -w start

export PGHOST="$PGBK_SOCKET"
export PGPORT=55432
export PGUSER=postgres
psql -X --set=ON_ERROR_STOP=on --dbname=postgres --command='SELECT version(), pg_is_in_recovery();'
```

Example paths contain no spaces. The `postgres` role must exist in the SOURCE cluster; otherwise use a known source administrator. Physical copies already contain roles/password hashes: do not reapply globals. Container `POSTGRES_USER`/`POSTGRES_PASSWORD` variables do not recreate roles in existing PGDATA. Wait for WAL recovery and readiness; if a standalone recovery remains in recovery or requires missing WAL, inspect logs and backup completeness instead of enabling source replication.

For containers, use compatible Linux/architecture and an image with required extensions. Mount PGDATA and every tablespace at the same paths used in the map, with matching file ownership/UID/GID. Run existing PGDATA as PostgreSQL without automatic initialization. Do not publish ports before configuration review. On Apple Silicon, Linux amd64 requires a suitable VM/emulation; having Docker does not make that PGDATA compatible with ARM PostgreSQL.

After validation, stop the dedicated destination with:

```sh
pg_ctl -D "$PGBK_PGDATA" -m fast -w stop
```

`pgbk restore physical` can also prepare the directory; it does not start the server. See [operational requirements](operations.md) for Jobs/PVCs.

## 8. Physical → managed through a staging server

Direct installation of pgbk physical files into managed PostgreSQL is not implemented. Even when a provider uses WAL-G internally, pgbk `st put/get` format is not its backup-push repository; `wal-g backup-fetch` is inapplicable. pgbk also refuses physical backups from Yandex managed sources.

1. Complete section 7 on a compatible staging server and validate data using section 9. Do not attach the production application or change data between exports when multi-database consistency is needed.
2. As the source administrator, dump each required database in custom format. Optionally export roles without passwords as input for mapping to managed roles:

```sh
pg_dump --format=custom --file=/srv/pgbk-restore/application.dump --dbname=application
pg_dumpall --globals-only --no-role-passwords --file=/srv/pgbk-restore/staging-globals.sql
```

3. Record new-file sizes/hashes, database name, versions and export provenance. These are NEW logical dumps, not original physical-manifest files; do not replace original artifacts with them.
4. Move files to a machine with managed access, prepare the cluster/extensions/roles and complete section 5. Do not execute all of `staging-globals.sql` in managed. Restore every exported database and account for external dependencies between databases.
5. After functional validation, switch application connectivity in a separate operational action. Until then, retain the original backup and staging server for repeat exports.

## 9. Validate and bring the destination into service

Error-free completion is necessary but insufficient. Retain the command, tool versions, run ID, timestamps and complete log with restricted access. In each restored database, analyze and inspect catalogs:

```sh
vacuumdb --analyze-in-stages --dbname="$PGBK_DEST_DB"
psql -X --set=ON_ERROR_STOP=on --dbname="$PGBK_DEST_DB" <<'SQL'
SELECT current_database(), current_user, version();
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT n.nspname, c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT i.indisvalid OR NOT i.indisready;
SELECT conrelid::regclass, conname FROM pg_constraint WHERE NOT convalidated;
\dn+
\dp
\ddp
SQL
```

For physical recovery, set `PGBK_DEST_DB` to the required recovered database and repeat for others. Compare expected tables/partitions/indexes/constraints, large objects, ownership and ACLs against backup scope and source reference data. Existing NOT VALID constraints do not necessarily indicate restore failure; compare with original state. Check critical row counts and business aggregates. For sequences compare `last_value`/`is_called`, increment/cycle parameters and maximum keys; do not call `nextval` just to inspect state. Sequence repair is a separate coordinated change accounting for application behavior.

Test authentication as the actual application role, reads and an appropriate test write. Account for RLS, function/sequence/large-object privileges and SECURITY DEFINER ownership. Configure authentication, certificates, jobs, FDW and external integrations before opening networking and switching connectivity. Retain the previous connection/rollback plan through validation; pgbk does not switch applications automatically.

## 10. Common errors and retries

| Message/situation | Action |
| --- | --- |
| `creating ACL ... role ... does not exist` | A source role is absent. Use `--no-owner --no-acl` for portable copies; prepare roles first for source ACLs. ACL-only errors do not establish completeness of other data. |
| Missing role in CREATE POLICY or a function | Portable flags do not rewrite RLS/code. Prepare a permitted role or an approved definition change. |
| `role already exists` in globals | Adapt working SQL for existing roles; do not ignore all file errors. |
| `relation already exists`, duplicate key, `RESTORE_PARTIAL` | Destination can be partial. Retain logs and retry in a fresh empty destination. A full retry over partial data is not a resume. |
| Unavailable managed extension/privilege | Check provider capabilities and prepare extensions/TOC/schema. A complete transfer can require application changes. |
| `RESTORE_MAJOR_MISMATCH`, `CLIENT_MAJOR_MISMATCH` | Use the required client/destination major. Perform version migration separately. |
| `RESTORE_ARCHITECTURE_MISMATCH` | Recover physical copies in a compatible environment; use logical export for another architecture. |
| Hash, backup_manifest or WAL checks fail | Stop before startup; inspect completeness and download again. Do not bypass verification or remove backup_label. |
| `RESTORE_SOURCE_REFUSED`, `DESTINATION_NOT_EMPTY` | Prepare a genuinely separate empty destination. Do not disable isolation for a retry. |

## 11. Further reading

- [Operations, Jobs, PVCs, Secrets and recovery](operations.md).
- [Configuration](configuration.md), [logical archive scope](logical-modes.md), [backup verification](verification.md), [validated compatibility limits](compatibility.md).
- PostgreSQL: [pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html), [pg_dumpall](https://www.postgresql.org/docs/18/app-pg-dumpall.html), [SQL dump](https://www.postgresql.org/docs/18/backup-dump.html), [pg_verifybackup](https://www.postgresql.org/docs/18/app-pgverifybackup.html). Choose the corresponding documentation version for another major.
