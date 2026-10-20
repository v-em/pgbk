"""Generate the single strict schema consumed by Helm and the CLI."""
import json
from pathlib import Path


def obj(properties, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(required)}


def mapping(value):
    return {"type": "object", "patternProperties": {r"^[a-z0-9][a-z0-9-]{0,47}$": value},
            "additionalProperties": False}


text = {"type": "string", "minLength": 1}
identifier = {"type": "string", "minLength": 1, "pattern": r"^[^\x00\r\n]+$"}
name = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,47}$"}
positive = {"type": "integer", "minimum": 1}
boolean = {"type": "boolean"}
image = {"type": "string", "pattern": r"^[^\s@]+@sha256:[0-9a-f]{64}$"}
resources = obj({level: obj({"cpu": text, "memory": text}, ["cpu", "memory"])
                 for level in ["requests", "limits"]}, ["requests", "limits"])
schedule = obj({"enabled": boolean, "cron": text, "timeZone": text,
                "startingDeadlineSeconds": positive}, ["enabled"])
schedule["allOf"] = [{"if": {"properties": {"enabled": {"const": True}}},
                      "then": {"required": ["cron"]}}]
scratch = obj({"type": {"enum": ["emptyDir", "pvc", "existingClaim"]}, "size": text,
               "storageClassName": {"type": "string"}, "claimName": name,
               "allowedClaims": {"type": "array", "items": name},
               "retainFailed": boolean}, ["type", "size"])
scratch["allOf"] = [{"if": {"properties": {"type": {"const": "existingClaim"}}},
                     "then": {"required": ["claimName", "allowedClaims"]}}]
table = obj({"schema": identifier, "table": identifier}, ["schema", "table"])
selector = obj({"tables": {"type": "array", "items": table}, "query": text})
selection = obj({"mode": {"enum": ["all", "selected"]},
                 "largeObjects": {"enum": ["none", "all"]},
                 "include": selector, "exclude": selector})
logical = obj({"databases": {"type": "array", "minItems": 1, "uniqueItems": True,
                             "items": identifier},
               "transfer": obj({"mode": {"enum": ["staged", "direct"]},
                                "partSizeMiB": {"type": "integer", "minimum": 5, "maximum": 512}}),
               "format": {"enum": ["custom", "directory"]},
               "jobs": {"type": "integer", "minimum": 1, "maximum": 64},
               "compression": obj({"method": {"enum": ["none", "gzip", "lz4", "zstd"]},
                                   "level": {"type": "integer"}}, ["method"]),
               "globals": {"enum": ["disabled", "enabled"]},
               "tableSelection": obj({"queryTimeoutSeconds": positive,
                                      "maxQueryRows": {"type": "integer", "minimum": 1, "maximum": 100000},
                                      "byDatabase": {"type": "object", "additionalProperties": selection}},
                                     ["byDatabase"])}, ["databases"])
verification = obj({"enabled": boolean, "schedule": schedule, "namespace": name,
                    "image": image, "resources": resources, "scratch": scratch,
                    "timeoutSeconds": positive, "credentialsSecretRef": name,
                    "failureDebugTtlSeconds": {"type": "integer", "minimum": 0},
                    "clientCredentialsSecretRef": name,
                    "assertions": {"type": "array", "items": text}}, ["enabled"])
verification["allOf"] = [{"if": {"properties": {"enabled": {"const": True}}},
                         "then": {"required": ["schedule", "namespace", "image", "resources",
                                               "scratch", "timeoutSeconds", "credentialsSecretRef",
                                               "clientCredentialsSecretRef"]}}]
restoration = obj({"enabled": boolean, "namespace": name, "resources": resources,
                   "scratch": scratch, "timeoutSeconds": positive,
                   "credentialsSecretRefs": {"type": "array", "uniqueItems": True, "items": name},
                   "allowedDestinationClaims": {"type": "array", "uniqueItems": True, "items": name}}, ["enabled"])
restoration["allOf"] = [{"if": {"properties": {"enabled": {"const": True}}},
                       "then": {"required": ["namespace", "resources", "scratch", "timeoutSeconds",
                                             "credentialsSecretRefs", "allowedDestinationClaims"]}}]
profile = obj({"target": name, "kind": {"enum": ["logical", "physical-full"]},
               "storage": name, "logical": logical,
               "schedule": schedule,
               "execution": obj({"activeDeadlineSeconds": positive, "resources": resources,
                                 "ttlSecondsAfterFinished": {"type": "integer", "minimum": 0},
                                 "nodeSelector": {"type": "object", "additionalProperties": text}}, ["resources"]),
               "scratch": scratch,
               "retention": obj({"maxAgeDays": positive, "minSuccessfulBackups": positive,
                                 "incompleteGraceDays": positive, "schedule": schedule}),
               "verification": verification, "restore": restoration,
               "download": obj({"allowedDestinationClaims": {"type": "array", "uniqueItems": True, "items": name}}, ["allowedDestinationClaims"]),
               "serviceAccounts": obj({"create": boolean, "backup": name, "repository": name, "verify": name, "restore": name})}, ["target", "kind", "storage", "execution", "scratch"])
profile["allOf"] = [
    {"if": {"properties": {"kind": {"const": "logical"}}}, "then": {"required": ["logical"]}},
    {"if": {"properties": {"kind": {"const": "physical-full"}}},
     "then": {"not": {"required": ["logical"]}}}]
schema = obj({
    "installationId": name,
    "coordinationNamespace": name,
    "coordination": obj({"createLeases": boolean}),
    "rbac": obj({"create": boolean}),
    "images": {"type": "object", "patternProperties": {r"^(14|15|16|17|18)$": image}, "additionalProperties": False},
    "networkPolicy": obj({"enabled": boolean, "egress": {"type": "array", "items": {"type": "object"}}}),
    "storage": mapping(obj({"bucket": text, "prefix": {"type": "string", "pattern": r"^[a-zA-Z0-9_-]+(/[a-zA-Z0-9_-]+)*$"},
                            "endpoint": {"type": "string", "pattern": "^https?://[^/@]+(:[0-9]+)?/?$"},
                            "region": text, "addressingStyle": {"enum": ["path", "virtual"]},
                            "credentialsSecretRef": name,
                            "tls": obj({"verify": {"const": True}, "caSecretRef": name})},
                           ["bucket", "prefix", "endpoint", "region", "credentialsSecretRef"])),
    "targets": mapping(obj({"sourceId": name, "concurrencyKey": name, "host": text,
                            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                            "serverMajor": {"enum": [14, 15, 16, 17, 18]}, "maintenanceDatabase": identifier,
                            "credentialsSecretRef": name, "expectedSystemIdentifier": {"type": "string", "pattern": "^[0-9]+$"},
                            "physicalEndpoint": {"enum": ["direct-primary"]},
                            "tls": obj({"mode": {"enum": ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]},
                                        "caSecretRef": name, "clientSecretRef": name})},
                           ["sourceId", "concurrencyKey", "host", "serverMajor", "credentialsSecretRef"])),
    "profiles": mapping(profile)
}, ["installationId", "targets", "storage", "profiles"])
schema["$schema"] = "http://json-schema.org/draft-07/schema#"
schema["title"] = "pgbk v1 configuration"
for path in ["charts/pgbk/values.schema.json", "src/pgbk/values.schema.json"]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(schema, indent=2) + "\n")
