#!/bin/bash
set -euo pipefail
export PATH="/usr/lib/postgresql/$PG_MAJOR/bin:$PATH"
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=postgres PGDATABASE=postgres
walg="${WALG_BIN:-wal-g}"
work=$(mktemp -d)
trap 'pg_ctl -D "$work/source" -m immediate stop >/dev/null 2>&1 || true; pg_ctl -D "$work/restore" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
initdb -D "$work/source" --auth=trust >/dev/null
pg_ctl -D "$work/source" -o '-c archive_mode=off -c listen_addresses=127.0.0.1' -w start >/dev/null
psql -X -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE DATABASE fixture;
CREATE ROLE restricted LOGIN;
\connect fixture
CREATE TABLE public.items(id integer PRIMARY KEY, value text);
INSERT INTO public.items SELECT i, md5(i::text) FROM generate_series(1,10000) i;
CREATE TABLE public.events(id integer);
CREATE SCHEMA reporting;
CREATE TABLE reporting.months(id integer, month integer) PARTITION BY RANGE(month);
CREATE TABLE reporting.jan PARTITION OF reporting.months FOR VALUES FROM(1) TO(2);
INSERT INTO reporting.months VALUES(42,1);
GRANT CONNECT ON DATABASE fixture TO restricted;
GRANT USAGE ON SCHEMA public,reporting TO restricted;
GRANT SELECT ON ALL TABLES IN SCHEMA public,reporting TO restricted;
SQL
pg_dump -U restricted -d fixture -Fc -f "$work/fixture.dump"
createdb logical_restored
pg_restore --exit-on-error --no-owner --no-acl -d logical_restored "$work/fixture.dump"
test "$(psql -XAt -d logical_restored -c 'SELECT count(*) FROM items')" = 10000
pg_basebackup -D "$work/backup" -Ft -Xstream --checkpoint=fast --manifest-checksums=SHA256
mkdir "$work/restore"
tar -xf "$work/backup/base.tar" -C "$work/restore"
tar -xf "$work/backup/pg_wal.tar" -C "$work/restore/pg_wal"
cp "$work/backup/backup_manifest" "$work/restore/backup_manifest"
pg_verifybackup "$work/restore"
pg_ctl -D "$work/source" -m fast -w stop >/dev/null
chmod 700 "$work/restore"
pg_ctl -D "$work/restore" -l "$work/restore.log" -o '-c archive_mode=off -c listen_addresses=127.0.0.1' -w start >/dev/null || { cat "$work/restore.log"; exit 1; }
test "$(psql -XAt -d fixture -c 'SELECT count(*) FROM items')" = 10000
pg_ctl -D "$work/restore" -m fast -w stop >/dev/null
head -c 8388611 /dev/urandom > "$work/raw.bin"
"$walg" st put "$work/raw.bin" "m0/$PG_MAJOR/raw.bin" --no-compress --no-encrypt
"$walg" st get "m0/$PG_MAJOR/raw.bin" "$work/roundtrip.bin" --no-decompress --no-decrypt
cmp "$work/raw.bin" "$work/roundtrip.bin"
if "$walg" st get "m0/$PG_MAJOR/missing" "$work/missing.bin" --no-decompress --no-decrypt; then
  echo 'Missing object unexpectedly succeeded' >&2; exit 1
fi
printf 'M0_CORE_PASS PostgreSQL=%s WAL_G=%s bytes=8388611\n' "$(pg_dump --version)" "$("$walg" --version)"
