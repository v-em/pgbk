"""Create CLI runtime dependency SBOM and checksums from retained worker evidence."""
import copy
import hashlib
import json
import tomllib
import re
from datetime import datetime, timezone
from pathlib import Path

version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
dist = Path('dist')
wheel = dist / f'pgbk-{version}-py3-none-any.whl'
worker = json.loads((dist / 'worker-17.sbom.cdx.json').read_text())
canonical = lambda name: re.sub(r'[-_.]+', '-', name).lower()
required = dict(line.split('==') for line in Path('requirements.lock').read_text().splitlines()
                if line and not line.startswith('#'))
components = [copy.deepcopy(c) for c in worker['components']
              if c.get('purl', '').startswith('pkg:pypi/') and canonical(c['name']) in required]
found = {canonical(c['name']): c['version'] for c in components}
assert found == required, f'Runtime dependency inventory mismatch: {required.keys() - found.keys()}'
root = {'type': 'application', 'name': 'pgbk', 'version': version,
        'bom-ref': f'pgbk-cli-{version}', 'purl': f'pkg:pypi/pgbk@{version}',
        'licenses': [{'license': {'id': 'Apache-2.0'}}],
        'hashes': [{'alg': 'SHA-256', 'content': hashlib.sha256(wheel.read_bytes()).hexdigest()}]}
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
        lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + str(path.relative_to(dist)))
(dist / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
print(f'CLI SBOM: {len(components)} locked dependencies; checksums: {len(lines)} files')
