import copy
import json

import pytest

from pgbk.config import normalize
from pgbk.credentials import pg_environment, pg_params
from pgbk.errors import Error
from pgbk.restore import load_destination_json


def project(monkeypatch, tmp_path, data):
    monkeypatch.setenv("PGBK_SECRET_ROOT", str(tmp_path))
    root = tmp_path / "database-credentials"
    root.mkdir()
    for key, value in data.items():
        (root / key).write_text(value)


def test_shared_secret_selects_each_target_pair(values, tmp_path, monkeypatch):
    project(monkeypatch, tmp_path, {
        "application-username": "application-reader", "application-password": "first:password",
        "reporting-username": "reporting-reader", "reporting-password": "second-password",
    })
    for label in ("application", "reporting"):
        target = copy.deepcopy(values["targets"]["example"])
        target.update(credentialsSecretRef="database-credentials", tls={"mode": "disable"},
                      credentialsSecretKeys={"username": label + "-username", "password": label + "-password"})
        values["targets"][label] = target
    config = normalize(values)
    for label, password in (("application", "first:password"), ("reporting", "second-password")):
        target = config["targets"][label]
        params = pg_params(target, label)
        assert params["user"] == label + "-reader"
        assert params["password"] == password
        scratch = tmp_path / label
        scratch.mkdir()
        env = pg_environment(target, label, scratch)
        assert env["PGUSER"] == params["user"]
        assert "PGPASSWORD" not in env
        assert (scratch / "pgpass").read_text() == "*:*:*:*:" + password.replace(":", "\\:") + "\n"
        assert (scratch / "pgpass").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("keys", [None, {}, {"password": "custom-password"}])
def test_omitted_keys_keep_defaults(values, tmp_path, monkeypatch, keys):
    project(monkeypatch, tmp_path, {
        "username": "legacy-reader", "password": "legacy-password", "custom-password": "custom-value",
    })
    target = values["targets"]["example"]
    target.update(credentialsSecretRef="database-credentials", tls={"mode": "disable"})
    if keys is not None:
        target["credentialsSecretKeys"] = keys
    target = normalize(values)["targets"]["example"]
    params = pg_params(target, "application")
    assert params["user"] == "legacy-reader"
    assert params["password"] == ("custom-value" if keys else "legacy-password")


def test_missing_explicit_key_does_not_fall_back(values, tmp_path, monkeypatch):
    project(monkeypatch, tmp_path, {"username": "legacy-reader", "password": "legacy-password"})
    target = values["targets"]["example"]
    target.update(credentialsSecretRef="database-credentials", credentialsSecretKeys={"password": "missing"})
    with pytest.raises(Error) as error:
        pg_params(normalize(values)["targets"]["example"], "application")
    assert error.value.code == "SECRET_UNAVAILABLE"
    assert "legacy-password" not in str(error.value.report())


@pytest.mark.parametrize("keys", [
    None, [], "username", {"unknown": "field"}, {"username": None},
    *({"password": key} for key in ("", ".", "..", "..data", "../password", "/password", "a/b", "a\\b", "a\n", "a" * 254)),
])
def test_invalid_key_mappings_rejected_for_targets_and_destinations(values, keys):
    values["targets"]["example"]["credentialsSecretKeys"] = keys
    with pytest.raises(Error):
        normalize(values)
    destination = {"host": "restore.example", "serverMajor": 17, "database": "application",
                   "credentialsSecretRef": "database-credentials", "credentialsSecretKeys": keys,
                   "acknowledgeIsolated": True}
    with pytest.raises(Error):
        load_destination_json(json.dumps(destination))


@pytest.mark.parametrize("keys", [None, {"username": "reporting-username", "password": "reporting-password"}])
def test_restore_destination_keys(keys):
    destination = {"host": "restore.example", "serverMajor": 17, "database": "reporting",
                   "credentialsSecretRef": "database-credentials", "acknowledgeIsolated": True}
    if keys is not None:
        destination["credentialsSecretKeys"] = keys
    assert load_destination_json(json.dumps(destination))["credentialsSecretKeys"] == (
        keys if keys is not None else {"username": "username", "password": "password"})
