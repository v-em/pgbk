import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pgbk.config import canonical, cron_valid


class Problem(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value):
        raise Problem("NOT_FOUND", 404)
    return value


def next_occurrence(expression, zone, clock=None):
    """Scan real UTC minutes: DST gaps disappear and repeated instants stay distinct."""
    if not cron_valid(expression):
        raise Problem("SCHEDULE_INVALID", 409)
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise Problem("SCHEDULE_TIMEZONE_INVALID", 409) from None
    fields = expression.split()
    sets = []
    for field, (low, high) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]):
        values = set()
        for term in field.split(","):
            base, _, step = term.partition("/")
            bounds = base.split("-")
            start = low if base == "*" else int(bounds[0])
            end = high if base == "*" or (step and len(bounds) == 1) else int(bounds[-1])
            values.update(range(start, end + 1, int(step or 1)))
        sets.append(values)
    minute = (clock or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    # Gregorian calendars repeat; eight years covers leap-day schedules.
    for _ in range(8 * 366 * 24 * 60):
        minute += timedelta(minutes=1)
        local = minute.astimezone(tz)
        if local.month not in sets[3] or local.hour not in sets[1] or local.minute not in sets[0]:
            continue
        dom = local.day in sets[2]
        dow = (local.weekday() + 1) % 7 in {v % 7 for v in sets[4]}
        days = (dom and dow) if fields[2].startswith("*") or fields[4].startswith("*") else (dom or dow)
        if days:
            return local.isoformat()
    raise Problem("SCHEDULE_NO_OCCURRENCE", 409)


# Explicit projection: unknown keys never reach HTTP responses or the index.
# Also covers nested captured environments and artifact metadata.
SAFE_FIELDS = set("""schemaVersion installationId sourceId profileId runId kind serverMajor architecture
startedAt completedAt scheduledAt durationSeconds bytes workerImage trigger configDigest artifacts
fileId filename purpose format size sha256 database scope compression transfer selection inventory
snapshotAt snapshotTimeBasis databaseSnapshots recoveryInstructions manifestSha256
verificationPolicy enabled required dueAt dueWithinSeconds environmentDigest environment
verificationId sequence finishedAt status reasonCode activeAttempt latestAttempt attempts
restoreVerification artifactStatus profileCompliant verificationPending verificationOverdue
waitingProtected operationPinned name version pg_dump pg_dumpall pg_restore postgres tools
level method mode jobs tables schemas include exclude extensions prerequisites archiveInventory
tableCount rowCount uncompressedBytes compressionMethod compressionLevel transferMode dumpJobs
indexes sequences constraints largeObjects schema table oid includeTables excludeTables schemaSelection tableSelection
filteredBySchema excludedSchemaObjects excludedExtensionTables included excluded effective provenance resolvedAt
partitionExpansion dependencyDiscovery knownPrerequisites types referencedTables parents extensionOwned
phase at failedPhase jobName jobUid namespace operation decision requestId subject time
source backupPolicy logical globals retention maxAgeDays minSuccessfulBackups
assertions results passed expected actual language snapshotTimeBasis description
""".split())
REDACTED_FIELDS = {"sourceEndpoint", "storageSnapshot", "configurationRef", "coordinationNamespace",
                   "credentialsSecretRef", "credentialsSecretRefs", "clientCredentialsSecretRef",
                   "caSecretRef", "clientSecretRef", "host", "port", "endpoint", "sql", "query",
                   "assertions", "secret", "password", "token"}


def project(value):
    if isinstance(value, dict):
        return {key: "UNKNOWN_REASON" if key == "reasonCode" and item is not None and (not isinstance(item, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", item))
                else "[redacted]" if key in REDACTED_FIELDS or re.search(r"secret|password|token|sql|query|host|endpoint", key, re.I)
                else item if key == "database" and isinstance(item, str)
                else project(item) for key, item in value.items()
                if key in SAFE_FIELDS or key in REDACTED_FIELDS}
    if isinstance(value, list):
        return [project(item) for item in value]
    if isinstance(value, str):
        # Internal literal addresses in otherwise approved free-form metadata.
        return re.sub(r"https?://[^\s]+|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[\w.-]+\.(?:svc|internal|cluster\.local)\b", "[redacted]", value)
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
