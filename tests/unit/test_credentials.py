import copy
import json

import pytest

from pgbk.config import normalize
from pgbk.credentials import pg_environment, pg_params, resolve_target
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


def secret_target(values, label="application"):
    target = copy.deepcopy(values["targets"]["example"])
    target.pop("host")
    target.pop("port", None)
    target.update(sourceId=label, concurrencyKey=label, credentialsSecretRef="database-credentials",
                  tls={"mode": "disable"},
                  credentialsSecretKeys={key: label + "-" + key for key in ("host", "port", "username", "password")})
    return target


def test_one_secret_resolves_all_connection_fields_for_each_target(values, tmp_path, monkeypatch):
    project(monkeypatch, tmp_path, {
        "application-host": "primary.example.invalid", "application-port": "6432",
        "application-username": "application-reader", "application-password": "first:password",
        "reporting-host": "reporting.svc.cluster.local", "reporting-port": "5433",
        "reporting-username": "reporting-reader", "reporting-password": "second-password",
    })
    for label in ("application", "reporting"):
        values["targets"][label] = secret_target(values, label)
    config = normalize(values)
    original = copy.deepcopy(config)
    for label, host, port in (("application", "primary.example.invalid", 6432),
                              ("reporting", "reporting.svc.cluster.local", 5433)):
        target = config["targets"][label]
        params = pg_params(target, label)
        assert (params["host"], params["port"], params["user"]) == (host, port, label + "-reader")
        scratch = tmp_path / label
        scratch.mkdir()
        env = pg_environment(target, label, scratch)
        assert (env["PGHOST"], env["PGPORT"], env["PGUSER"]) == (host, str(port), label + "-reader")
        assert "host" not in target and "port" not in target
    assert config == original


def test_secret_endpoint_validation_does_not_read_secret(values, monkeypatch):
    import pgbk.credentials
    monkeypatch.setattr(pgbk.credentials, "secret", lambda *args: pytest.fail("validation read Secret"))
    values["targets"]["example"] = secret_target(values)
    assert "host" not in normalize(values)["targets"]["example"]


def test_helm_null_overrides_clear_inherited_endpoints(values):
    target = secret_target(values)
    target.update(host=None, port=None)
    values["targets"]["example"] = target
    target = normalize(values)["targets"]["example"]
    assert "host" not in target and "port" not in target


def test_selected_source_refusal_works_without_source_secret(values, tmp_path):
    from pgbk.restore import logical_restore
    from pgbk.process import Control
    values["targets"]["example"] = secret_target(values)
    manifest = {"kind": "logical", "serverMajor": 17, "artifacts": [{"database": "fixture"}],
                "sourceEndpoint": {"host": "source.example.invalid", "port": 5432}}
    destination = {"serverMajor": 17, "host": "source.example.invalid", "port": 5432}
    with pytest.raises(Error) as error:
        logical_restore(normalize(values), manifest, tmp_path, "fixture", destination, Control(), tmp_path)
    assert error.value.code == "RESTORE_SOURCE_REFUSED"


@pytest.mark.parametrize("field,value", [
    ("host", ""), ("host", "/var/run/postgresql"), ("host", "source password=secret"),
    ("host", "host1,host2"), ("host", "-option"), ("port", "0"), ("port", "65536"),
    ("port", "5432 extra"), ("port", ""), ("port", "-1"), ("port", "1.5"), ("port", "a"),
])
def test_invalid_projected_endpoint_is_rejected_without_echoing_values(values, tmp_path, monkeypatch, field, value):
    data = {"application-host": "source.example.invalid", "application-port": "5432",
            "application-username": "reader", "application-password": "PASSWORD_SENTINEL"}
    data["application-" + field] = value
    project(monkeypatch, tmp_path, data)
    values["targets"]["example"] = secret_target(values)
    with pytest.raises(Error) as error:
        pg_params(normalize(values)["targets"]["example"], "application")
    assert error.value.code == "INVALID_ENDPOINT"
    report = str(error.value.report())
    assert "PASSWORD_SENTINEL" not in report and "source.example.invalid" not in report
    if value and len(value) > 3:
        assert value not in report


@pytest.mark.parametrize("missing", ["host", "port"])
def test_missing_projected_endpoint_never_falls_back(values, tmp_path, monkeypatch, missing):
    data = {"application-host": "source.example.invalid", "application-port": "5432"}
    del data["application-" + missing]
    project(monkeypatch, tmp_path, data)
    values["targets"]["example"] = secret_target(values)
    with pytest.raises(Error) as error:
        resolve_target(normalize(values)["targets"]["example"])
    assert error.value.code == "SECRET_UNAVAILABLE"


@pytest.mark.parametrize("mode", ["only-host", "only-port", "mixed-host", "mixed-port"])
def test_endpoint_modes_are_exclusive(values, mode):
    target = secret_target(values)
    if mode.startswith("only-"):
        target["credentialsSecretKeys"].pop("port" if mode == "only-host" else "host")
    elif mode == "mixed-host":
        target["host"] = "inline.example.invalid"
    else:
        target["port"] = 5432
    values["targets"]["example"] = target
    with pytest.raises(Error):
        normalize(values)


def test_source_endpoint_is_frozen_for_an_operation(values, tmp_path, monkeypatch):
    project(monkeypatch, tmp_path, {"application-host": "first.example.invalid", "application-port": "5432"})
    target = secret_target(values)
    resolved = resolve_target(target)
    (tmp_path / "database-credentials/application-host").write_text("second.example.invalid")
    assert resolve_target(resolved)["host"] == "first.example.invalid"
    assert resolve_target(target)["host"] == "second.example.invalid"


def test_same_secret_endpoint_requires_shared_concurrency_key(values):
    first = secret_target(values)
    second = copy.deepcopy(first)
    second.update(sourceId="another-source", concurrencyKey="another-lock")
    values["targets"].update(first=first, second=second)
    with pytest.raises(Error) as error:
        normalize(values)
    assert error.value.code == "CONCURRENCY_KEY_MISMATCH"


def test_hidden_yandex_endpoint_still_refuses_physical_backup(values, tmp_path, monkeypatch):
    from pgbk.postgres import preflight
    from pgbk.process import Control
    project(monkeypatch, tmp_path, {"application-host": "host.mdb.yandexcloud.net", "application-port": "6432"})
    with pytest.raises(Error) as error:
        preflight(secret_target(values), {"kind": "physical-full"}, Control(), tmp_path)
    assert error.value.code == "YANDEX_PHYSICAL_UNSUPPORTED"
