import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from pgbk.catalog import Catalog, retention_candidates
from pgbk.config import canonical, normalize
from pgbk.errors import Error


class MemoryS3:
    config = {"prefix": "postgres", "endpoint": "https://example.invalid"}
    bucket = "bucket"
    def __init__(self):
        self.objects = {}
        self.denied = False
        self.lost = False
    def get(self, key):
        return self.objects.get(key)
    def put(self, key, data):
        self.objects[key] = data
        if self.lost and key.endswith("complete.json"):
            self.lost = False
            raise Error("S3_RETRY_EXHAUSTED", "Lost response")
    def call(self, method, Key):
        return {"ContentLength": len(self.objects[Key])} if Key in self.objects else None
    def remote_hash(self, key):
        return hashlib.sha256(self.objects[key]).hexdigest(), len(self.objects[key])
    def keys(self, prefix):
        return iter([key for key in self.objects if key.startswith(prefix)])
    def delete(self, key):
        if self.denied:
            raise Error("S3_ACCESS_DENIED", "Denied", 8)
        self.objects.pop(key, None)
    def transfer(self, direction, key, path):
        assert direction == "get"
        path.write_bytes(self.objects[key])


class Held:
    def confirm(self):
        pass


@pytest.fixture
def repository(values):
    s3 = MemoryS3()
    return Catalog(s3, normalize(values), "example-logical")


def completed(repository, run_id="run-1", at="2026-01-01T00:00:00+00:00", filename="a.dump"):
    data = b"test-archive"
    file_id = "opaque-id"
    repository.s3.objects[repository.run_prefix(run_id) + "artifacts/" + file_id] = data
    manifest = {"schemaVersion": 1, **repository.scope, "runId": run_id, "kind": "logical", "completedAt": at,
                "serverMajor": 17, "source": {}, "tools": {}, "sourceEndpoint": {"host": "source.invalid", "port": 5432},
                "artifacts": [{"fileId": file_id, "filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                               "format": "postgres-custom", "database": "fixture", "scope": "full-database"}]}
    repository.publish(manifest, Held())
    return manifest


def test_incomplete_is_not_usable(repository):
    repository.s3.put(repository.run_prefix("incomplete") + "manifest.json", b"{}")
    assert repository.list()["backups"] == []
    with pytest.raises(Error):
        repository.describe("incomplete")


def test_lost_marker_response_reconciles(repository):
    repository.s3.lost = True
    manifest = completed(repository)
    assert repository.describe("run-1") == manifest


def test_corruption_blocks_listing(repository):
    completed(repository)
    repository.s3.objects[repository.run_prefix("run-1") + "artifacts/opaque-id"] = b"bad-archive!"
    assert repository.list()["backups"] == []
    assert repository.list()["invalidRuns"][0]["reasonCode"].startswith("ARTIFACT_")


@pytest.mark.parametrize("filename", ["../a", "/a", "..", "a/b", "a\\b"])
def test_unsafe_download_mapping(repository, filename):
    completed(repository, filename=filename)
    with pytest.raises(Error):
        repository.describe("run-1")


def test_scope_tampering(repository):
    completed(repository)
    prefix = repository.run_prefix("run-1")
    repository.s3.objects[prefix + "manifest.json"] = b"{}"
    with pytest.raises(Error) as error:
        repository.describe("run-1")
    assert error.value.exit_code == 6


def test_retention_boundary_ties_minimum_and_protection():
    clock = datetime(2026, 10, 9, tzinfo=timezone.utc)
    runs = [{"runId": f"run-{i}", "completedAt": (clock - timedelta(days=age)).isoformat()}
            for i, age in enumerate([1, 40, 40, 40, 30, 70])]
    candidates = retention_candidates(runs, 30, 3, clock, protected={"run-5"})
    assert {r["runId"] for r in candidates} == {"run-1", "run-2"}
    assert retention_candidates(runs[:2], 30, 3, clock) == []


def test_delete_stays_within_scope_and_resumes(repository):
    for i in range(5):
        completed(repository, run_id=f"run-{i}")
    neighbor = repository.root + "runs/run-0-neighbor/artifacts/keep"
    repository.s3.objects[neighbor] = b"keep"
    result = repository.cleanup({"maxAgeDays": 30, "minSuccessfulBackups": 3}, Held())
    assert len(result["deletedRunIds"]) == 2
    assert repository.s3.objects[neighbor] == b"keep"
    assert len(repository.list()["backups"]) == 3


def test_delete_denial_does_not_mark_backup_failed(repository):
    for i in range(4):
        completed(repository, run_id=f"run-{i}")
    repository.s3.denied = True
    with pytest.raises(Error):
        repository.cleanup({"maxAgeDays": 30, "minSuccessfulBackups": 3}, Held())
    assert all(b"Failed" not in v for k, v in repository.s3.objects.items() if k.endswith("manifest.json"))


def test_recovery_instructions_are_published_before_marker_and_downloaded(repository, tmp_path):
    writes = []
    put = repository.s3.put
    def record(key, data):
        writes.append(key)
        put(key, data)
    repository.s3.put = record
    manifest = completed(repository)
    assert writes[-1].endswith("complete.json")
    assert len(manifest["recoveryInstructions"]) == 2
    output = tmp_path / "download"
    repository.download("run-1", output)
    assert json.loads((output / "DOWNLOAD.json").read_text())["recoveryInstructionsOrigin"] == "backup"
    for item in manifest["recoveryInstructions"]:
        content = (output / item["filename"]).read_bytes()
        assert content == repository.s3.get(repository.run_prefix("run-1") + "recovery/" + item["filename"])
        assert (hashlib.sha256(content).hexdigest(), len(content)) == (item["sha256"], item["size"])
    assert "a.dump" in (output / "BACKUP-RESTORE.md").read_text()


def test_corrupt_recovery_instructions_are_not_usable(repository):
    manifest = completed(repository)
    key = repository.run_prefix("run-1") + "recovery/" + manifest["recoveryInstructions"][0]["filename"]
    content = repository.s3.objects[key]
    repository.s3.objects[key] = b"X" + content[1:]
    with pytest.raises(Error) as error:
        repository.describe("run-1")
    assert error.value.code == "ARTIFACT_HASH_MISMATCH"


def test_bad_recovery_upload_prevents_completion(repository):
    remote_hash = repository.s3.remote_hash
    repository.s3.remote_hash = lambda key: ("0" * 64, 0) if "/recovery/" in key else remote_hash(key)
    with pytest.raises(Error) as error:
        completed(repository)
    assert error.value.code == "ARTIFACT_HASH_MISMATCH"
    assert repository.s3.get(repository.run_prefix("run-1") + "complete.json") is None


def test_subset_notes_warn_that_globals_were_not_downloaded(repository, tmp_path):
    manifest = completed(repository)
    prefix = repository.run_prefix("run-1")
    repository.s3.delete(prefix + "complete.json")  # Fixture setup, not a product update.
    content = b"-- globals"
    repository.s3.put(prefix + "artifacts/globals-id", content)
    manifest["artifacts"].append({"fileId": "globals-id", "filename": "globals.sql",
                                  "format": "postgres-globals-sql", "scope": "globals",
                                  "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    repository.publish(manifest, Held())
    output = tmp_path / "subset"
    repository.download("run-1", output, "fixture")
    assert not (output / "globals.sql").exists()
    assert "NOT downloaded" in (output / "BACKUP-RESTORE.md").read_text()
    download = json.loads((output / "DOWNLOAD.json").read_text())
    assert download["downloadedFileIds"] == ["opaque-id"]
    assert download["recoveryInstructionsOrigin"] == "download"
    assert repository.describe("run-1") == manifest


def test_old_backup_gets_notes_without_being_modified(repository, tmp_path):
    manifest = completed(repository)
    prefix = repository.run_prefix("run-1")
    notes = manifest.pop("recoveryInstructions")
    for item in notes:
        repository.s3.delete(prefix + "recovery/" + item["filename"])
    content = canonical(manifest)
    repository.s3.put(prefix + "manifest.json", content)
    marker = json.loads(repository.s3.get(prefix + "complete.json"))
    marker["manifestSha256"] = hashlib.sha256(content).hexdigest()
    repository.s3.put(prefix + "complete.json", canonical(marker))
    before = dict(repository.s3.objects)
    output = tmp_path / "old"
    repository.download("run-1", output)
    assert "no globals.sql" in (output / "BACKUP-RESTORE.md").read_text()
    assert repository.s3.objects == before


@pytest.mark.parametrize("filename", ["BACKUP-RESTORE.md", "backup-restore.RU.md"])
def test_archives_cannot_overwrite_recovery_notes(repository, filename):
    completed(repository, filename=filename)
    with pytest.raises(Error) as error:
        repository.describe("run-1")
    assert error.value.code == "UNSAFE_FILENAME"


def test_physical_notes_remain_separate_from_the_complete_postgres_file_set(repository, tmp_path):
    manifest = {"schemaVersion": 1, **repository.scope, "runId": "physical-1", "kind": "physical-full",
                "completedAt": "2026-01-01T00:00:00+00:00", "serverMajor": 17, "architecture": "x86_64",
                "source": {"tablespaceOids": ["16384"]}, "tools": {},
                "sourceEndpoint": {"host": "source.invalid", "port": 5432}, "artifacts": []}
    filenames = ["base.tar", "pg_wal.tar", "16384.tar", "backup_manifest"]
    for index, filename in enumerate(filenames):
        data = filename.encode()
        file_id = "physical-" + str(index)
        repository.s3.put(repository.run_prefix("physical-1") + "artifacts/" + file_id, data)
        manifest["artifacts"].append({"fileId": file_id, "filename": filename, "size": len(data),
                                      "sha256": hashlib.sha256(data).hexdigest(), "format": "postgres-tar"})
    repository.publish(manifest, Held())
    assert repository.describe("physical-1") == manifest
    output = tmp_path / "physical"
    repository.download("physical-1", output)
    assert {a["filename"] for a in manifest["artifacts"]} == set(filenames)
    assert "tablespace-16384" in (output / "BACKUP-RESTORE.md").read_text()
