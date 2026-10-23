import copy
import hashlib
import json
import re
from importlib.resources import files
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

import jsonschema
import yaml
from kubernetes.utils.quantity import parse_quantity

from .errors import Error, require
from .credentials import pg_credential_keys


class StrictLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        key = loader.construct_object(key, deep=deep)
        require(key not in result, "DUPLICATE_CONFIG_KEY", "Duplicate configuration key")
        result[key] = loader.construct_object(value, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def tcp_host(value):
    return isinstance(value, str) and bool(value) and not value.startswith("-") and not any(
        character.isspace() or character in "/\\@?=#,%" or ord(character) < 32 for character in value)


def cron_valid(expression):
    fields = expression.split()
    if len(fields) != 5:
        return False
    for value, (low, high) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]):
        for term in value.split(","):
            m = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", term)
            if not m or (m[2] and not 1 <= int(m[2]) <= high - low + 1):
                return False
            if m[1] != "*":
                bounds = [int(x) for x in m[1].split("-")]
                if any(not low <= x <= high for x in bounds) or bounds[0] > bounds[-1]:
                    return False
    return True


def normalize(raw):
    schema = json.loads(files("pgbk").joinpath("values.schema.json").read_text())
    errors = list(jsonschema.Draft7Validator(schema).iter_errors(raw))
    if errors:
        # Do not echo values: a rejected unknown field may contain a credential.
        raise Error("INVALID_CONFIG", "Configuration does not match the v1 schema", 2,
                    paths=["/".join(map(str, e.absolute_path)) for e in errors])
    c = copy.deepcopy(raw)
    require(c["profiles"] and c["targets"] and c["storage"], "INVALID_CONFIG", "At least one profile, target and storage is required")
    c.setdefault("images", {})
    for t in c["targets"].values():
        require(tcp_host(t["host"]),
                "INVALID_ENDPOINT", "Source must be a TCP hostname")
        t.setdefault("port", 5432)
        t.setdefault("maintenanceDatabase", "postgres")
        t["credentialsSecretKeys"] = pg_credential_keys(t)
        t.setdefault("tls", {})["mode"] = t.get("tls", {}).get("mode", "verify-full")
    aliases = {}
    for t in c["targets"].values():
        for alias in [("source", t["sourceId"]), ("endpoint", t["host"].lower(), t["port"])]:
            require(alias not in aliases or aliases[alias] == t["concurrencyKey"],
                    "CONCURRENCY_KEY_MISMATCH", "Aliases for one source must share concurrencyKey")
            aliases[alias] = t["concurrencyKey"]
    for s in c["storage"].values():
        try:
            endpoint = urlsplit(s["endpoint"])
            valid = endpoint.scheme in ["http", "https"] and endpoint.hostname and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment and endpoint.path in ["", "/"] and (endpoint.port is None or 1 <= endpoint.port <= 65535)
        except ValueError:
            valid = False
        require(valid and not any(ch.isspace() for ch in s["endpoint"]), "INVALID_ENDPOINT", "S3 endpoint must be a credential-free origin URL")
        s.setdefault("addressingStyle", "path")
        s.setdefault("tls", {})["verify"] = True
    for pid, p in c["profiles"].items():
        require(p["target"] in c["targets"] and p["storage"] in c["storage"], "INVALID_REFERENCE", "Profile has an unknown target or storage")
        t = c["targets"][p["target"]]
        if p["kind"] == "physical-full":
            require(t.get("physicalEndpoint") == "direct-primary" and t.get("expectedSystemIdentifier"),
                    "PHYSICAL_PREREQUISITE", "Physical targets require direct-primary and expectedSystemIdentifier")
        p.setdefault("schedule", {"enabled": False})
        p["execution"].setdefault("activeDeadlineSeconds", 21600)
        p["execution"].setdefault("ttlSecondsAfterFinished", 86400)
        retention = p.setdefault("retention", {})
        retention.setdefault("maxAgeDays", 30)
        retention.setdefault("minSuccessfulBackups", 3)
        retention.setdefault("incompleteGraceDays", 7)
        retention.setdefault("schedule", {"enabled": True, "cron": "0 4 * * *"})
        p.setdefault("verification", {"enabled": False})
        p.setdefault("restore", {"enabled": False})
        p.setdefault("download", {"allowedDestinationClaims": []})
        require(c.get("coordinationNamespace") or not (p["restore"]["enabled"] or p["verification"]["enabled"]),
                "COORDINATION_NAMESPACE_REQUIRED", "Isolated operations require an explicit coordination namespace")
        accounts = p.setdefault("serviceAccounts", {"create": False})
        accounts.setdefault("create", False)
        if not accounts["create"]:
            required_accounts = ["backup", "repository"]
            if p["verification"]["enabled"]:
                required_accounts.append("verify")
            if p["restore"]["enabled"]:
                required_accounts.append("restore")
            require(all(accounts.get(role) for role in required_accounts), "EXISTING_SERVICE_ACCOUNT_REQUIRED",
                    "Configure existing ServiceAccounts for every enabled operation")
        schedules = [p["schedule"], retention["schedule"]]
        if p["verification"]["enabled"]:
            p["verification"].setdefault("failureDebugTtlSeconds", 86400)
            schedules.append(p["verification"]["schedule"])
        for schedule in schedules:
            schedule.setdefault("cron", "0 2 * * *")
            schedule.setdefault("timeZone", "UTC")
            schedule.setdefault("startingDeadlineSeconds", 3600)
            require(cron_valid(schedule["cron"]), "INVALID_SCHEDULE", "Schedule must be a valid numeric five-field cron expression")
            try:
                ZoneInfo(schedule["timeZone"])
            except ZoneInfoNotFoundError:
                raise Error("INVALID_TIME_ZONE", "Schedule time zone must be an IANA zone", 2) from None
        scratch = p["scratch"]
        try:
            scratches = [scratch]
            if p["verification"]["enabled"]:
                scratches.append(p["verification"]["scratch"])
            if p["restore"]["enabled"]:
                scratches.append(p["restore"]["scratch"])
            for volume in scratches:
                require(parse_quantity(volume["size"]) > 0, "INVALID_CAPACITY", "Scratch capacity must be positive")
                require(not volume.get("retainFailed") or volume["type"] != "existingClaim",
                        "UNBOUNDED_DEBUG_RETENTION", "Debug retention needs run-owned scratch")
                if volume["type"] == "existingClaim":
                    require(volume["claimName"] in volume["allowedClaims"], "UNSAFE_PVC", "Scratch must be allowlisted")
            budgets = [p["execution"]["resources"]]
            if p["verification"]["enabled"]:
                budgets.append(p["verification"]["resources"])
            if p["restore"]["enabled"]:
                budgets.append(p["restore"]["resources"])
            for budget in budgets:
                for resource in ["cpu", "memory"]:
                    require(0 < parse_quantity(budget["requests"][resource]) <= parse_quantity(budget["limits"][resource]),
                            "INVALID_RESOURCES", "Resource requests must be positive and no greater than limits")
        except (ValueError, ArithmeticError):
            raise Error("INVALID_RESOURCES", "Invalid Kubernetes resource quantity", 2) from None
        require(not scratch.get("retainFailed") or scratch["type"] != "existingClaim",
                "UNBOUNDED_DEBUG_RETENTION", "Failed workspace retention requires a run-owned ephemeral volume")
        selectors = p["execution"].get("nodeSelector", {})
        require(selectors.get("kubernetes.io/arch", "amd64") == "amd64" and
                selectors.get("kubernetes.io/os", "linux") == "linux", "UNSUPPORTED_PLATFORM", "Worker platform is Linux amd64", 3)
        if scratch["type"] == "existingClaim":
            require(scratch["claimName"] in scratch["allowedClaims"], "UNSAFE_PVC", "Scratch claim must be explicitly allowlisted")
        if p["kind"] == "logical":
            logical = p["logical"]
            logical.setdefault("format", "custom")
            logical.setdefault("jobs", 1)
            transfer = logical.setdefault("transfer", {})
            transfer.setdefault("mode", "staged")
            transfer.setdefault("partSizeMiB", 16)
            compression = logical.setdefault("compression", {"method": "gzip", "level": 6})
            method, level = compression["method"], compression.get("level")
            require(logical["jobs"] == 1 or logical["format"] == "directory",
                    "PARALLEL_FORMAT_REQUIRED", "Parallel dump requires directory format")
            require(transfer["mode"] != "direct" or (logical["format"] == "custom" and logical["jobs"] == 1),
                    "DIRECT_FORMAT_REQUIRED", "Direct S3 requires custom format and jobs=1")
            require(transfer["mode"] != "direct" or scratch["type"] == "emptyDir",
                    "DIRECT_SCRATCH_REQUIRED", "Direct S3 uses a small emptyDir for credential and metadata files")
            require(method in ["none", "gzip"] or t["serverMajor"] >= 16,
                    "COMPRESSION_UNSUPPORTED", "lz4/zstd require PostgreSQL tools 16 or later", 3)
            require(level is None or (method == "gzip" and 1 <= level <= 9) or
                    (method == "zstd" and 1 <= level <= 22),
                    "COMPRESSION_LEVEL_INVALID", "gzip accepts levels 1..9; zstd 1..22; none/lz4 have no level")
            logical.setdefault("globals", "disabled")
            selection = logical.setdefault("tableSelection", {"byDatabase": {}})
            selection.setdefault("queryTimeoutSeconds", 30)
            selection.setdefault("maxQueryRows", 10000)
            for db, rule in selection["byDatabase"].items():
                require(db in logical["databases"], "INVALID_REFERENCE", "Table selection references an unconfigured database")
                rule.setdefault("mode", "all")
                rule.setdefault("largeObjects", "none" if rule["mode"] == "selected" else "all")
                include = rule.get("include", {})
                require(rule["mode"] != "all" or not (include.get("tables") or include.get("query")),
                        "INVALID_SELECTION", "All mode cannot have an include source")
                require(rule["mode"] != "selected" or include.get("tables") or include.get("query"),
                        "EMPTY_TABLE_SELECTION", "Selected mode requires an include source")
                for section in [include, rule.get("exclude", {})]:
                    if section.get("query"):
                        from .selection import validate_query
                        validate_query(section["query"])
    return c


def load(path):
    try:
        with open(path) as f:
            return normalize(yaml.load(f, Loader=StrictLoader))
    except (OSError, TypeError, yaml.YAMLError):
        raise Error("INVALID_CONFIG", "Cannot read configuration", 2) from None


def profile(c, name):
    require(name in c["profiles"], "UNKNOWN_PROFILE", "An explicit configured profile is required")
    p = c["profiles"][name]
    return p, c["targets"][p["target"]], c["storage"][p["storage"]]
