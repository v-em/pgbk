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


def identifier_pattern(name):
    # Quoted pg_dump patterns preserve literal wildcard characters and dots.
    return '"' + name.replace('"', '""') + '"'


def pattern(schema, table):
    return identifier_pattern(schema) + "." + identifier_pattern(table)


CATALOG = """
SELECT c.oid,n.nspname,c.relname,c.relkind,
       EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
              AND d.objid=c.oid AND d.deptype='e') AS extension_owned
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
"""


def schema_filtered(logical):
    rule = logical.get("schemaSelection", {})
    return bool(rule.get("include") or rule.get("exclude") or "largeObjects" in rule)


def dump_args(selection):
    """Reapply frozen names for dump and restored-inventory inspection."""
    args = []
    schemas = selection.get("schemaSelection", {})
    if selection["mode"] == "selected":
        args.extend("--table=" + pattern(row["schema"], row["table"]) for row in selection["effective"])
    else:
        args.extend("--schema=" + identifier_pattern(name) for name in schemas.get("include", []))
        args.extend("--exclude-schema=" + identifier_pattern(name) for name in schemas.get("exclude", []))
    args.append("--blobs" if selection["largeObjects"] == "all" else "--no-blobs")
    for row in selection["excluded"]:
        args.append("--exclude-table=" + pattern(row["schema"], row["table"]))
    # Extension configuration data may be selected independently of TABLE definitions.
    for row in selection.get("excludedExtensionTables", []):
        args.append("--exclude-table-data=" + pattern(row["schema"], row["table"]))
    return args


def resolve(conn, rule, options, schema_rule=None):
    """Caller holds one read-only repeatable-read transaction through pg_dump."""
    conn.execute("SELECT set_config('statement_timeout', %s, true)",
                 (str(options["queryTimeoutSeconds"] * 1000),))
    conn.execute("SELECT set_config('lock_timeout', %s, true)",
                 (str(options["queryTimeoutSeconds"] * 1000),))
    schema_rule = schema_rule or {}
    namespaces = conn.execute("SELECT oid,nspname FROM pg_namespace").fetchall()
    user_schemas = {name for _, name in namespaces if not name.startswith("pg_") and name != "information_schema"}
    include = set(schema_rule.get("include", []))
    exclude = set(schema_rule.get("exclude", []))
    permitted = (user_schemas & include if include else user_schemas) - exclude
    require(not include or permitted, "EMPTY_SCHEMA_SELECTION", "No schemas remain after schema selection", 3)
    forbidden = {name for _, name in namespaces} - permitted
    schema_info = {"include": sorted(include), "exclude": sorted(exclude), "effective": sorted(permitted),
                   "resolved": [{"oid": oid, "name": name} for oid, name in namespaces if name in permitted],
                   "unmatchedInclude": sorted(include - {name for _, name in namespaces}),
                   "unmatchedExclude": sorted(exclude - {name for _, name in namespaces})}
    # OIDs avoid parsing whitespace, quotes or wildcard characters out of TOC names.
    forbidden_objects = [[2615, oid] for oid, name in namespaces if name in forbidden]
    for catalog, column in [("pg_class", "relnamespace"), ("pg_proc", "pronamespace"),
                            ("pg_type", "typnamespace"), ("pg_constraint", "connamespace")]:
        forbidden_objects.extend([int(classid), oid] for classid, oid in conn.execute(
            sql.SQL("SELECT {}::regclass::oid,oid FROM {} WHERE {}=ANY(%s)").format(
                sql.Literal(catalog), sql.Identifier(catalog), sql.Identifier(column)),
            ([oid for oid, name in namespaces if name in forbidden and name in user_schemas],)).fetchall())
    rows = conn.execute(CATALOG).fetchall()
    by_name = {(r[1], r[2]): r for r in rows}
    by_oid = {r[0]: r for r in rows}
    edges = conn.execute("SELECT inhparent,inhrelid FROM pg_inherits").fetchall()
    children = {}
    for parent, child in edges:
        children.setdefault(parent, set()).add(child)

    def admissible(row, role="include"):
        return row[3] in ("r", "p") and (role == "exclude" or not row[4]) and row[1] in user_schemas

    def expand(seeds, role):
        result, todo = set(), list(seeds)
        while todo:
            oid = todo.pop()
            if oid not in result:
                require(oid in by_oid and admissible(by_oid[oid], role), "UNSUPPORTED_RELATION",
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
        sets[role] = expand(seeds, role)
    all_tables = {oid for oid, row in by_oid.items() if admissible(row)}
    candidates = sets["include"] if rule["mode"] == "selected" else all_tables
    filtered = {oid for oid in candidates if by_oid[oid][1] not in permitted}
    effective = candidates - filtered - sets["exclude"]
    require(rule["mode"] != "selected" or effective, "EMPTY_TABLE_SELECTION", "No tables remain after exclusions", 3)
    # ONLY prevents ancestor locks from implicitly locking unexpected descendants.
    for oid in sorted(effective):
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
        return [{"oid": oid, "schema": by_oid[oid][1], "table": by_oid[oid][2],
                 "extensionOwned": bool(by_oid[oid][4])} for oid in sorted(ids)]
    extension_excluded = {oid for oid, row in by_oid.items() if row[4] and row[3] in ("r", "p") and
                          (rule["mode"] == "selected" or row[1] not in permitted or oid in sets["exclude"])}
    info = {"mode": rule["mode"], "schemaSelection": schema_info,
            "largeObjects": rule.get("largeObjects", schema_rule.get("largeObjects", "none" if include or rule["mode"] == "selected" else "all")),
            "filteredBySchema": descriptions(filtered), "excludedSchemaObjects": forbidden_objects,
            "excludedExtensionTables": descriptions(extension_excluded), "included": descriptions(sets["include"]),
            "excluded": descriptions(sets["exclude"]), "effective": descriptions(effective),
            "provenance": provenance, "resolvedAt": datetime.now(timezone.utc).isoformat(),
            "partitionExpansion": True, "dependencyDiscovery": "incomplete",
            "knownPrerequisites": known,
            "prerequisites": ["External schemas, types, extensions, functions, parents and FK targets may be required"]}
    return ["--snapshot=" + snapshot, *dump_args(info)], info


def validate_toc(toc, selection):
    actual = {int(m[1]) for m in re.finditer(r"^\d+; 1259 (\d+) TABLE (?!ATTACH )", toc, re.M)}
    expected = {row["oid"] for row in selection["effective"]}
    forbidden = {tuple(pair) for pair in selection.get("excludedSchemaObjects", [])}
    excluded = {row["oid"] for row in [*selection.get("excluded", []),
                *selection.get("filteredBySchema", []), *selection.get("excludedExtensionTables", [])]}
    for classid, oid, kind in re.findall(r"^\d+; (\d+) (\d+) ([^\n]+)", toc, re.M):
        key = (int(classid), int(oid))
        relation_data = key[0] == 0 and kind.startswith(("TABLE DATA ", "SEQUENCE SET ", "MATERIALIZED VIEW DATA "))
        require(key not in forbidden and not (relation_data and
                ((1259, key[1]) in forbidden or key[1] in excluded)),
                "ARCHIVE_SELECTION_MISMATCH", "Archive contains an excluded schema object or table data", 6)
    require(actual == expected, "ARCHIVE_SELECTION_MISMATCH", "Archive table definitions differ from the frozen selection", 6)
