import pytest

from pgbk.config import cron_valid, normalize
from pgbk.errors import Error
from pgbk.selection import pattern, validate_query, validate_toc


def test_defaults(values):
    c = normalize(values)
    p = c["profiles"]["example-logical"]
    assert p["retention"]["maxAgeDays"] == 30
    assert p["retention"]["minSuccessfulBackups"] == 3
    assert p["schedule"]["enabled"] is False
    assert p["execution"]["activeDeadlineSeconds"] == 21600


@pytest.mark.parametrize("change", [
    lambda c: c.update(password="secret-canary"),
    lambda c: c["profiles"]["example-logical"].update(kind="incremental"),
    lambda c: c["profiles"]["example-logical"].update(target="absent"),
    lambda c: c["profiles"]["example-logical"]["retention"].update(maxAgeDays=0),
    lambda c: c["storage"]["archive"].update(prefix="a/../b"),
    lambda c: c["storage"]["archive"]["tls"].update(verify=False),
    lambda c: c["targets"]["example"]["tls"].update(unknown=True),
    lambda c: c["targets"]["example"].update(host="host=source password=secret-canary"),
    lambda c: c["targets"]["example"].update(host="postgres://user:secret-canary@source"),
    lambda c: c["storage"]["archive"].update(endpoint="https://source?secret-canary"),
    lambda c: c["profiles"]["example-logical"].update(schedule={"enabled": True, "cron": "0 25 * * *"}),
    lambda c: c["profiles"]["example-logical"].update(schedule={"enabled": True, "cron": "0 2 * * *", "timeZone": "No/SuchZone"}),
])
def test_invalid(values, change):
    change(values)
    with pytest.raises(Error) as error:
        normalize(values)
    assert "secret-canary" not in str(error.value.report())


@pytest.mark.parametrize("query", [
    "DELETE FROM public.a", "SELECT 1; SELECT 2", "SELECT 1 INTO a",
    "WITH x AS (DELETE FROM a RETURNING *) SELECT * FROM x",
    "WITH x AS (INSERT INTO a VALUES(1) RETURNING *) SELECT * FROM x",
    "SELECT * FROM a FOR UPDATE", "DO $$ BEGIN END $$", "COMMIT",
    "SELECT (WITH x AS (UPDATE a SET n=1 RETURNING *) SELECT * FROM x)",
])
def test_writing_sql_rejected(query):
    with pytest.raises(Error):
        validate_query(query)


@pytest.mark.parametrize("query", [
    "SELECT 'a;b'::text AS schema_name, 'x'::text AS table_name;",
    "WITH x AS (SELECT 1) SELECT 'public'::text AS schema_name, 'a'::text AS table_name FROM x",
    "/* DELETE; */ SELECT n::text AS schema_name,t::text AS table_name FROM selections",
])
def test_select_accepted(query):
    validate_query(query)


def test_pattern_is_exact():
    assert pattern('a.b"*', "Юникод ?[]") == '"a.b""*"."Юникод ?[]"'


def test_archive_oids_do_not_parse_whitespace_names():
    selected = {"effective": [{"oid": 16392}]}
    validate_toc("12; 1259 16392 TABLE strange schema strange table owner\n13; 0 16392 TABLE DATA strange schema strange table owner\n", selected)
    with pytest.raises(Error, match="frozen"):
        validate_toc("12; 1259 16393 TABLE public extra postgres\n", selected)


@pytest.mark.parametrize("expression,valid", [("0 2 * * *", True), ("*/15 0-4 * 1,12 0", True),
    ("0 24 * * *", False), ("* * * *", False), ("*/0 * * * *", False), ("0 2 32 * *", False)])
def test_cron(expression, valid):
    assert cron_valid(expression) is valid


def test_physical_filters_rejected(values):
    values["profiles"]["example-logical"]["kind"] = "physical-full"
    with pytest.raises(Error):
        normalize(values)


def test_selected_requires_include(values):
    values["profiles"]["example-logical"]["logical"]["tableSelection"] = {"byDatabase": {"application": {"mode": "selected"}}}
    with pytest.raises(Error) as error:
        normalize(values)
    assert error.value.code == "EMPTY_TABLE_SELECTION"


def test_aliases_must_share_source_lock(values):
    import copy
    alias = copy.deepcopy(values["targets"]["example"])
    alias["concurrencyKey"] = "another-key"
    values["targets"]["alias"] = alias
    with pytest.raises(Error) as error:
        normalize(values)
    assert error.value.code == "CONCURRENCY_KEY_MISMATCH"
