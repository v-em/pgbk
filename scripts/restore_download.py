"""Restore an already downloaded artifact set without Kubernetes/S3 connectivity."""
import argparse
import json
import re
from pathlib import Path

from pgbk.errors import Error, require
from pgbk.process import Control, install_signals
from pgbk.restore import load_destination, logical_restore, physical_restore
from pgbk.storage import hash_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives', required=True)
    parser.add_argument('--timeout', type=int, default=21600)
    commands = parser.add_subparsers(dest='kind', required=True)
    logical = commands.add_parser('logical')
    logical.add_argument('--database', required=True)
    logical.add_argument('--destination-config', required=True)
    physical = commands.add_parser('physical')
    physical.add_argument('--pgdata', required=True)
    physical.add_argument('--major', required=True, type=int)
    physical.add_argument('--tablespace-map', default='{}')
    physical.add_argument('--acknowledge-isolated-stopped', action='store_true')
    args = parser.parse_args()
    archives = Path(args.archives).resolve()
    manifest = json.loads((archives / 'manifest.json').read_text())
    items = [a for a in manifest['artifacts'] if args.kind == 'physical' or a.get('database') == args.database]
    require(items, 'DATABASE_NOT_FOUND', 'No matching downloaded archive', 2)
    for item in items:
        require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', item['filename']) and
                not (archives / item['filename']).is_symlink(), 'UNSAFE_FILENAME', 'Unsafe downloaded filename', 6)
        require(hash_file(archives / item['filename']) == (item['sha256'], item['size']),
                'ARTIFACT_HASH_MISMATCH', 'Downloaded archive hash differs', 6)
    control = Control(args.timeout)
    install_signals(control)
    if args.kind == 'logical':
        report = logical_restore({'targets': {}}, manifest, archives, args.database,
                                 load_destination(args.destination_config), control, archives)
    else:
        mappings = json.loads(args.tablespace_map)
        require(isinstance(mappings, dict) and all(isinstance(k, str) and re.fullmatch('[0-9]+', k)
                and isinstance(v, str) for k,v in mappings.items()), 'INVALID_TABLESPACE_MAP', 'Invalid tablespace mapping', 2)
        report = physical_restore(manifest, archives, Path(args.pgdata), mappings, control, args.major,
                                  args.acknowledge_isolated_stopped)
    print(json.dumps(report))


if __name__ == '__main__':
    try:
        main()
    except Error as error:
        print(json.dumps(error.report()))
        raise SystemExit(error.exit_code)
