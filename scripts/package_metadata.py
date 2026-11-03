"""Create CLI runtime dependency SBOM and checksums from retained worker evidence."""
import copy
import argparse
import hashlib
import json
import tomllib
import re
from datetime import datetime, timezone
from pathlib import Path

project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
version = project["version"]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dist', type=Path, default=Path('dist') / version)
parser.add_argument('--worker-sbom', default='worker-18.sbom.cdx.json')
args = parser.parse_args()
dist = args.dist
wheel = dist / f'pgbk-{version}-py3-none-any.whl'
worker = json.loads((dist / args.worker_sbom).read_text())


def sha256(path):
    checksum = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()

canonical = lambda name: re.sub(r'[-_.]+', '-', name).lower()
required = dict(line.split('==') for line in Path('requirements.lock').read_text().splitlines()
                if line and not line.startswith('#'))
components = [copy.deepcopy(c) for c in worker['components']
              if c.get('purl', '').startswith('pkg:pypi/') and canonical(c['name']) in required]
found = {canonical(c['name']): c['version'] for c in components}
assert found == required, f'Runtime dependency inventory mismatch: {required.keys() - found.keys()}'
root = {'type': 'application', 'name': 'pgbk', 'version': version,
        'bom-ref': f'pgbk-cli-{version}', 'purl': f'pkg:pypi/pgbk@{version}',
        'licenses': [{'license': {'id': project['license']}}],
        'hashes': [{'alg': 'SHA-256', 'content': sha256(wheel)}]}
refs = {c['bom-ref'] for c in components}
dependencies = [{'ref': root['bom-ref'], 'dependsOn': sorted(refs)}]
for dependency in worker.get('dependencies', []):
    if dependency['ref'] in refs:
        dependencies.append({'ref': dependency['ref'],
                             'dependsOn': [ref for ref in dependency.get('dependsOn', []) if ref in refs]})
sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
        'metadata': {'timestamp': datetime.now(timezone.utc).isoformat(), 'component': root,
                     'properties': [{'name': 'pgbk:scope', 'value':
                                     'CLI wheel and separately installed locked Python runtime dependencies; '
                                     'dependency license inventory from the Linux worker. Native PostgreSQL/WAL-G '
                                     'tools are external to the wheel and inventoried in worker SBOMs.'}]},
        'components': components, 'dependencies': dependencies}
(dist / 'cli.sbom.cdx.json').write_text(json.dumps(sbom, indent=2) + '\n')
lines = []
for path in sorted(dist.rglob('*')):
    if path.is_file() and path.name != 'SHA256SUMS':
        lines.append(sha256(path) + '  ' + str(path.relative_to(dist)))
(dist / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
print(f'CLI SBOM: {len(components)} locked dependencies; checksums: {len(lines)} files')
