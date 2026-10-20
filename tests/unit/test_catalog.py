import hashlib
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
