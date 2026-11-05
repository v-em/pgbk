from types import SimpleNamespace

import pytest

from pgbk.config import normalize
from pgbk.errors import Error
from pgbk.selection import CATALOG, dump_args, identifier_pattern, resolve, validate_toc


class CatalogConnection:
    def __init__(self, rows, edges=()):
        self.rows = rows
        self.edges = list(edges)
        self.locks = []

    def execute(self, statement, parameters=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        rows = []
        if statement == CATALOG:
            rows = self.rows
        elif text == 'SELECT oid,nspname FROM pg_namespace':
            rows = [(10, 'public'), (11, 'cron'), (12, 'reporting'), (13, 'a.*" Ю')]
        elif text == 'SELECT inhparent,inhrelid FROM pg_inherits':
            rows = self.edges
        elif text.startswith('LOCK TABLE'):
            self.locks.append(text)
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: ('snapshot',))


def selection(conn, rule=None, schemas=None):
    return resolve(conn, rule or {'mode': 'all'}, {'queryTimeoutSeconds': 30, 'maxQueryRows': 10000}, schemas)


def test_schema_names_are_literal_and_deduplicated(values):
    values['profiles']['example-logical']['logical']['schemaSelection'] = {'exclude': ['cron', 'cron', 'a.*" Ю']}
    normalized = normalize(values)['profiles']['example-logical']['logical']['schemaSelection']
    assert normalized['exclude'] == ['cron', 'a.*" Ю']
    assert identifier_pattern('a.*" Ю') == '"a.*"" Ю"'


@pytest.mark.parametrize('invalid', [None, {'exclude': None}, {'exclude': ['']},
                                    {'exclude': ['cron\t']}, {'exclude': ['cron\x85']}, {'include': [1]}, {'unknown': []}])
def test_invalid_schema_selection(values, invalid):
    values['profiles']['example-logical']['logical']['schemaSelection'] = invalid
    with pytest.raises(Error):
        normalize(values)


def test_large_object_policy_conflict(values):
    logical = values['profiles']['example-logical']['logical']
    logical['schemaSelection'] = {'largeObjects': 'all'}
    logical['tableSelection'] = {'byDatabase': {'application': {'mode': 'all', 'largeObjects': 'none'}}}
    with pytest.raises(Error) as error:
        normalize(values)
    assert error.value.code == 'CONFLICTING_LARGE_OBJECT_POLICY'


def test_excluded_extension_tables_are_never_locked():
    conn = CatalogConnection([(1, 'public', 'items', 'r', False), (2, 'cron', 'job', 'r', True)])
    args, info = selection(conn, {'mode': 'all', 'exclude': {'tables': [{'schema': 'cron', 'table': 'job'}]}})
    assert '--exclude-table="cron"."job"' in args
    assert '--exclude-table-data="cron"."job"' in args
    assert info['excluded'][0]['extensionOwned']
    assert conn.locks == ['LOCK TABLE ONLY "public"."items" IN ACCESS SHARE MODE']
    validate_toc('1; 1259 1 TABLE public items owner\n', info)
    with pytest.raises(Error):
        validate_toc('1; 1259 1 TABLE public items owner\n2; 0 2 TABLE DATA cron job owner\n', info)


def test_schema_filter_after_partition_expansion():
    conn = CatalogConnection([(1, 'public', 'parent', 'p', False), (2, 'cron', 'child', 'r', False)], [(1, 2)])
    args, info = selection(conn, {'mode': 'selected', 'include': {'tables': [{'schema': 'public', 'table': 'parent'}]}}, {'exclude': ['cron']})
    assert '--table="public"."parent"' in args
    assert '--table="cron"."child"' not in args
    assert [row['oid'] for row in info['filteredBySchema']] == [2]
    assert all('cron' not in lock for lock in conn.locks)
    assert info['largeObjects'] == 'none'


def test_schema_only_exclusion_and_unmatched_names():
    conn = CatalogConnection([(1, 'public', 'items', 'r', False), (2, 'cron', 'job', 'r', True)])
    args, info = selection(conn, schemas={'exclude': ['cron', 'missing']})
    assert '--exclude-schema="cron"' in args
    assert '--exclude-table-data="cron"."job"' in args
    assert info['schemaSelection']['unmatchedExclude'] == ['missing']
    assert info['largeObjects'] == 'all'
    assert '--no-blobs' not in args
    assert dump_args(info) == args[1:]


def test_empty_schema_include_fails_closed():
    with pytest.raises(Error) as error:
        selection(CatalogConnection([]), schemas={'include': ['absent']})
    assert error.value.code == 'EMPTY_SCHEMA_SELECTION'


def test_schema_filter_can_select_schema_without_tables():
    args, info = selection(CatalogConnection([]), schemas={'include': ['reporting']})
    assert info['effective'] == []
    assert '--schema="reporting"' in args
    assert '--no-blobs' in args


def test_schema_exclusion_wins_over_include():
    with pytest.raises(Error) as error:
        selection(CatalogConnection([]), schemas={'include': ['cron'], 'exclude': ['cron']})
    assert error.value.code == 'EMPTY_SCHEMA_SELECTION'


def test_schema_toc_check_uses_oids():
    info = {'effective': [], 'excludedSchemaObjects': [[2615, 10], [1255, 999]]}
    with pytest.raises(Error):
        validate_toc('1; 2615 10 SCHEMA strange name owner\n', info)
    with pytest.raises(Error):
        validate_toc('1; 1255 999 FUNCTION strange name() owner\n', info)


def test_excluded_schema_sequence_data_is_rejected():
    info = {'effective': [], 'excludedSchemaObjects': [[1259, 20]]}
    with pytest.raises(Error):
        validate_toc('1; 0 20 SEQUENCE SET cron jobid_seq owner\n', info)


def test_extension_table_inclusion_remains_unsupported():
    conn = CatalogConnection([(1, 'cron', 'job', 'r', True)])
    with pytest.raises(Error) as error:
        selection(conn, {'mode': 'selected', 'include': {'tables': [{'schema': 'cron', 'table': 'job'}]}})
    assert error.value.code == 'UNSUPPORTED_RELATION'


def test_restore_reuses_existing_schemas_by_oid(tmp_path):
    from pgbk.restore import existing_schema_args
    item = {'selection': {'schemaSelection': {'resolved': [{'oid': 10, 'name': 'public'}, {'oid': 11, 'name': 'public other'}]}}}
    toc = '1; 2615 10 SCHEMA - public owner\n2; 2615 11 SCHEMA - public other owner\n3; 1259 22 TABLE public items owner\n'
    args = existing_schema_args(item, toc, {'public'}, tmp_path)
    assert args == ['--use-list=' + str(tmp_path/'restore-list.txt')]
    assert (tmp_path/'restore-list.txt').read_text() == '; ' + toc
    assert existing_schema_args({'selection': {'mode': 'all'}}, toc, {'public'}, tmp_path) == []
