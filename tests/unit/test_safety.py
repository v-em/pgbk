import copy
import io
import tarfile
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from pgbk.errors import Error
from pgbk.kube import Lease
from pgbk.postgres import extract_tar, sanitize_physical
from pgbk.process import Control


@pytest.mark.parametrize("name,link", [("../escape", None), ("/absolute", None), ("safe", "/etc/passwd"), ("safe", "../../escape")])
def test_tar_traversal_and_links(tmp_path, name, link):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as t:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            t.addfile(info)
        else:
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(Error) as error:
        extract_tar(archive, destination)
    assert error.value.exit_code == 6


def test_source_recovery_configuration_is_replaced(tmp_path):
    (tmp_path / "postgresql.auto.conf").write_text("primary_conninfo='secret-canary'\n")
    (tmp_path / "postgresql.conf").write_text("include='/source/config'\narchive_command='bad'\n")
    (tmp_path / "standby.signal").touch()
    changed = sanitize_physical(tmp_path)
    assert "postgresql.auto.conf" in changed
    assert "secret-canary" not in (tmp_path / "postgresql.auto.conf").read_text()
    assert not (tmp_path / "standby.signal").exists()
    assert "archive_mode = off" in (tmp_path / "postgresql.conf").read_text()


class LeaseAPI:
    def __init__(self, holder=""):
        self.value = SimpleNamespace(spec=SimpleNamespace(holder_identity=holder), metadata=SimpleNamespace(resource_version="1"))
    def read_namespaced_lease(self, *_args, **_kwargs):
        return copy.deepcopy(self.value)
    def replace_namespaced_lease(self, name, namespace, body, **kwargs):
        if body.metadata.resource_version != self.value.metadata.resource_version:
            raise ApiException(status=409)
        body.metadata.resource_version = str(int(body.metadata.resource_version) + 1)
        self.value = copy.deepcopy(body)


def test_expired_holder_is_never_stolen():
    api = LeaseAPI("orphan-on-partitioned-node")
    with pytest.raises(Error) as error:
        with Lease(api, "backups", "source", Control(), "next"):
            pytest.fail("A dump must never start")
    assert error.value.exit_code == 4
    assert api.value.spec.holder_identity == "orphan-on-partitioned-node"


def test_release_requires_ownership():
    api = LeaseAPI()
    control = Control()
    with Lease(api, "backups", "source", control, "worker"):
        assert api.value.spec.holder_identity == "worker"
    assert not api.value.spec.holder_identity


def test_cancelled_process_does_not_run():
    control = Control()
    control.cancel()
    with pytest.raises(Error) as error:
        control.run(["must-not-execute"])
    assert error.value.exit_code == 7


def test_lost_confirmation_cancels_and_preserves_orphan():
    api = LeaseAPI()
    control = Control()
    with pytest.raises(Error) as error:
        with Lease(api, "backups", "source", control, "worker") as lease:
            api.value.spec.holder_identity = "fenced-replacement"
            lease.confirm()
    assert error.value.code == "LEASE_LOST"
    assert control.stop.is_set()
    assert api.value.spec.holder_identity == "fenced-replacement"


def test_blocked_connection_receives_cancellation():
    import threading
    control = Control(0.05)
    called = threading.Event()
    conn = SimpleNamespace(cancel_safe=lambda **kwargs: called.set())
    with pytest.raises(Error) as error:
        with control.connection(conn):
            assert called.wait(2)
    assert error.value.code == "DEADLINE_EXCEEDED"
