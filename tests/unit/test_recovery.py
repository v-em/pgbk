import json
import re
import shlex

from pgbk.recovery import recovery_notes


def logical(format_name="postgres-custom", scope="full-database"):
    return {"runId": "run-example", "profileId": "application", "kind": "logical", "serverMajor": 18,
            "source": {}, "storageSnapshot": {"secret": "NEVER_INCLUDE_THIS"},
            "artifacts": [{"fileId": "db-id", "filename": "db.dump", "database": "приложение```<x>",
                           "format": format_name, "scope": scope}]}


def test_logical_notes_use_actual_archive_and_do_not_expose_config():
    note = recovery_notes(logical())["BACKUP-RESTORE.ru.md"].decode()
    assert "./db.dump" in note and "приложение" in note
    assert "```" not in note and "<x>" not in note
    assert "NEVER_INCLUDE_THIS" not in note
    assert "нет globals.sql" in note
    assert "--no-owner --no-acl --no-tablespaces" in note


def test_filtered_directory_notes_do_not_treat_bundle_as_custom_dump():
    notes = recovery_notes(logical("postgres-directory-tar", "filtered-database"))
    note = notes["BACKUP-RESTORE.md"].decode()
    assert "FILTERED backup" in note and "knownPrerequisites" in note
    assert '"$PGBK_LOGICAL_DIRECTORY"' in note
    assert "--dbname=\"$PGBK_DEST_DB\" ./db.dump" not in note


def test_physical_notes_use_major_and_exact_tablespace_oids():
    manifest = {"runId": "run-physical", "kind": "physical-full", "serverMajor": 17, "architecture": "x86_64",
                "source": {"tablespaceOids": ["16384", "24576"]},
                "artifacts": [{"fileId": "base-id", "filename": "base.tar", "size": 42, "sha256": "a" * 64}]}
    note = recovery_notes(manifest)["BACKUP-RESTORE.md"].decode()
    command = re.search(r"~~~sh\n(.*?)\n~~~", note, re.S)[1]
    args = shlex.split(command)
    assert args[args.index("--major") + 1] == "17"
    assert json.loads(args[args.index("--tablespace-map") + 1]) == {
        "16384": "/srv/pgbk-restore/tablespace-16384", "24576": "/srv/pgbk-restore/tablespace-24576"}
    assert "does NOT start PostgreSQL" in note
    assert "staging server" in note
    assert "base.tar" in note and "a" * 64 in note
