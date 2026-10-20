import os
from pathlib import Path

from .errors import Error


def secret(ref, key, optional=False):
    try:
        # Kubernetes projections are symlinks; ref/key are schema-validated, never supplied by SQL.
        return (Path(os.environ.get("PGBK_SECRET_ROOT", "/secrets")) / ref / key).read_text().rstrip("\n")
    except OSError:
        if optional:
            return None
        raise Error("SECRET_UNAVAILABLE", "Required Secret projection is unavailable", 8) from None


def secret_path(ref, key):
    path = Path(os.environ.get("PGBK_SECRET_ROOT", "/secrets")) / ref / key
    if not path.is_file():
        raise Error("SECRET_UNAVAILABLE", "Required TLS Secret projection is unavailable", 8)
    return str(path)


def pg_params(target, database):
    tls = target["tls"]
    params = {"host": target["host"], "port": target["port"], "dbname": database,
              "user": secret(target["credentialsSecretRef"], "username"),
              "password": secret(target["credentialsSecretRef"], "password"),
              "sslmode": tls["mode"], "connect_timeout": 15}
    if tls.get("caSecretRef"):
        params["sslrootcert"] = secret_path(tls["caSecretRef"], "ca.crt")
    if tls.get("clientSecretRef"):
        params["sslcert"] = secret_path(tls["clientSecretRef"], "tls.crt")
        params["sslkey"] = secret_path(tls["clientSecretRef"], "tls.key")
    return params


def pg_environment(target, database, scratch):
    params = pg_params(target, database)
    # A password file with wildcard identity supports arbitrary SQL database names.
    path = scratch / "pgpass"
    value = params.pop("password").replace("\\", "\\\\").replace(":", "\\:")
    if "\n" in value or "\r" in value:
        raise Error("INVALID_SECRET", "Multiline PostgreSQL passwords are unsupported", 3)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("*:*:*:*:" + value + "\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(scratch), "TMPDIR": str(scratch),
           "LANG": "C.UTF-8", "PGPASSFILE": str(path)}
    for name, key in {"PGHOST": "host", "PGPORT": "port", "PGDATABASE": "dbname", "PGUSER": "user",
                      "PGSSLMODE": "sslmode", "PGSSLROOTCERT": "sslrootcert", "PGSSLCERT": "sslcert",
                      "PGSSLKEY": "sslkey", "PGCONNECT_TIMEOUT": "connect_timeout"}.items():
        if key in params:
            env[name] = str(params[key])
    return env
