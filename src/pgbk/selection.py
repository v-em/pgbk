import hashlib
import re
from datetime import datetime, timezone

from pglast import parse_sql
from psycopg import sql

from .errors import Error, require


def validate_query(query):
    try:
        statements = parse_sql(query)
        require(len(statements) == 1 and statements[0].stmt.__class__.__name__ == "SelectStmt",
                "UNSAFE_SELECTION_QUERY", "Selection query must be one read-only SELECT")
        def inspect(node):
            if isinstance(node, dict):
                kind = node.get("@", "")
                require(not kind.endswith("Stmt") or kind in ["RawStmt", "SelectStmt"],
                        "UNSAFE_SELECTION_QUERY", "Writing SQL is forbidden in selection queries")
                require(not node.get("intoClause") and not node.get("lockingClause"),
                        "UNSAFE_SELECTION_QUERY", "SELECT INTO and locking clauses are forbidden")
                for value in node.values():
                    inspect(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    inspect(value)
        inspect(statements[0]())
    except Error:
        raise
    except Exception:
        raise Error("INVALID_SELECTION_QUERY", "Cannot parse selection query", 2) from None


def pattern(schema, table):
    # pg_dump patterns inside double quotes treat wildcard symbols literally.
    return '"' + schema.replace('"', '""') + '"."' + table.replace('"', '""') + '"'


CATALOG = """
SELECT c.oid,n.nspname,c.relname,c.relkind,
       EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
              AND d.objid=c.oid AND d.deptype='e') AS extension_owned
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
"""


def resolve(conn, rule, options):
    """Caller holds one read-only repeatable-read transaction through pg_dump."""
    conn.execute("SELECT set_config('statement_timeout', %s, true)",
                 (str(options["queryTimeoutSeconds"] * 1000),))
    conn.execute("SELECT set_config('lock_timeout', %s, true)",
                 (str(options["queryTimeoutSeconds"] * 1000),))
    rows = conn.execute(CATALOG).fetchall()
    by_name = {(r[1], r[2]): r for r in rows}
    by_oid = {r[0]: r for r in rows}
    edges = conn.execute("SELECT inhparent,inhrelid FROM pg_inherits").fetchall()
    children = {}
    for parent, child in edges:
        children.setdefault(parent, set()).add(child)

    def admissible(row):
        return row[3] in ("r", "p") and not row[4] and not row[1].startswith("pg_") and row[1] != "information_schema"

    def expand(seeds):
        result, todo = set(), list(seeds)
        while todo:
            oid = todo.pop()
            if oid not in result:
                require(oid in by_oid and admissible(by_oid[oid]), "UNSUPPORTED_RELATION",
                        "Only ordinary and partitioned user tables are selectable", 3)
                result.add(oid)
                todo.extend(children.get(oid, ()))
        return result

    provenance = {}
    sets = {}
    for role in ["include", "exclude"]:
        section = rule.get(role, {})
        names = [(t["schema"], t["table"]) for t in section.get("tables", [])]
        provenance[role] = {"manualCount": len(names), "queryRows": 0, "queryDigest": None}
        if section.get("query"):
            query = section["query"]
            validate_query(query)
            provenance[role]["queryDigest"] = hashlib.sha256(query.encode()).hexdigest()
            # Server-side cursor bounds result memory and counts rows BEFORE deduplication.
            with conn.cursor(name="selection_" + role) as cur:
                cur.execute(query)
                batch = cur.fetchmany(1)
                require(cur.description is not None and
                        [(d.name, d.type_code) for d in cur.description] == [("schema_name", 25), ("table_name", 25)],
                        "INVALID_QUERY_RESULT", "Selection query must return exactly schema_name/text and table_name/text", 3)
                while batch:
                    provenance[role]["queryRows"] += len(batch)
                    require(provenance[role]["queryRows"] <= options["maxQueryRows"],
                            "QUERY_ROW_LIMIT", "Selection query exceeds raw row limit", 3)
                    for pair in batch:
                        require(all(isinstance(n, str) and n and not any(x in n for x in ["\x00", "\n", "\r"]) for n in pair),
                                "INVALID_QUERY_RESULT", "Selection identifiers must be nonempty text", 3)
                        names.append(tuple(pair))
                    batch = cur.fetchmany(min(256, options["maxQueryRows"] + 1))
        seeds = set()
        for name in names:
            require(name in by_name, "RELATION_NOT_FOUND", "A selected or excluded relation does not exist", 3)
            seeds.add(by_name[name][0])
        sets[role] = expand(seeds)
    all_tables = {oid for oid, row in by_oid.items() if admissible(row)}
    effective = (sets["include"] if rule["mode"] == "selected" else all_tables) - sets["exclude"]
    require(rule["mode"] != "selected" or effective, "EMPTY_TABLE_SELECTION", "No tables remain after exclusions", 3)
    # ONLY prevents ancestor locks from implicitly locking unexpected descendants.
    for oid in sorted(effective | sets["exclude"]):
        row = by_oid[oid]
        conn.execute(sql.SQL("LOCK TABLE ONLY {} IN ACCESS SHARE MODE").format(sql.Identifier(row[1], row[2])))
    snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
    known = {"schemas": sorted({by_oid[oid][1] for oid in effective}), "types": [], "referencedTables": [], "parents": []}
    if rule["mode"] == "selected":
        known["types"] = [{"schema": r[0], "name": r[1]} for r in conn.execute("""SELECT DISTINCT n.nspname,t.typname
          FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid JOIN pg_namespace n ON n.oid=t.typnamespace
          WHERE a.attrelid=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped AND n.nspname NOT LIKE 'pg\\_%%'
          AND n.nspname<>'information_schema'""", (list(effective),)).fetchall()]
        for key, query in {
            "referencedTables": "SELECT DISTINCT confrelid FROM pg_constraint WHERE contype='f' AND conrelid=ANY(%s)",
            "parents": "SELECT DISTINCT inhparent FROM pg_inherits WHERE inhrelid=ANY(%s)"
        }.items():
            ids = {r[0] for r in conn.execute(query, (list(effective),)).fetchall()} - effective
            known[key] = [{"schema": by_oid[oid][1], "table": by_oid[oid][2]} for oid in sorted(ids) if oid in by_oid]
    def descriptions(ids):
        return [{"oid": oid, "schema": by_oid[oid][1], "table": by_oid[oid][2]} for oid in sorted(ids)]
    info = {"mode": rule["mode"], "included": descriptions(sets["include"]),
            "excluded": descriptions(sets["exclude"]), "effective": descriptions(effective),
            "provenance": provenance, "resolvedAt": datetime.now(timezone.utc).isoformat(),
            "partitionExpansion": True, "dependencyDiscovery": "incomplete",
            "knownPrerequisites": known,
            "prerequisites": ["External schemas, types, extensions, functions, parents and FK targets may be required"]}
    args = ["--snapshot=" + snapshot]
    if rule["mode"] == "selected":
        for row in info["effective"]:
            args.append("--table=" + pattern(row["schema"], row["table"]))
        args.append("--blobs" if rule["largeObjects"] == "all" else "--no-blobs")
    for row in info["excluded"]:
        args.append("--exclude-table=" + pattern(row["schema"], row["table"]))
    return args, info


def validate_toc(toc, selection):
    actual = {int(m[1]) for m in re.finditer(r"^\d+; 1259 (\d+) TABLE (?!ATTACH )", toc, re.M)}
    expected = {row["oid"] for row in selection["effective"]}
    require(actual == expected, "ARCHIVE_SELECTION_MISMATCH", "Archive table definitions differ from the frozen selection", 6)
