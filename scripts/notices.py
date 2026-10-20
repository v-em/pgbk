"""Produce component notice inventory and retain scanner license texts."""
import base64
import argparse
import hashlib
import json
import tomllib
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--sbom', default='dist/worker-17.sbom.cdx.json')
parser.add_argument('--syft-json', help='Optional Syft JSON containing full license contents')
args = parser.parse_args()
version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
source = Path(args.sbom)
sbom = json.loads(source.read_text())
lines = [f'pgbk {version} — third-party component inventory',
         'Original code: Apache-2.0; see LICENSE and NOTICE.',
         'PostgreSQL: PostgreSQL License; WAL-G: Apache-2.0; Python: PSF license.',
         'OS/Python/embedded Go components have separate licenses listed below.',
         'Keep distributed copyright/license files and corresponding source obligations.',
         'Source locations: Debian https://sources.debian.org/ ; PostgreSQL https://www.postgresql.org/ftp/source/ ;',
         'WAL-G https://github.com/wal-g/wal-g/tree/v3.0.9 ; Python https://www.python.org/downloads/source/ ;',
         'Python dependency sdists: https://pypi.org/ ; Go modules: source URLs/purls in the SBOM.',
         'This is an automated development inventory, not final license certification.', '']
unknown = []
texts = Path('dist/license-texts')
texts.mkdir(parents=True, exist_ok=True)
if args.syft_json:
    for artifact in json.loads(Path(args.syft_json).read_text())['artifacts']:
        for entry in artifact.get('licenses', []):
            content = entry.get('contents')
            if content:
                (texts / (hashlib.sha256(content.encode()).hexdigest() + '.txt')).write_text(content)
for i, component in enumerate(sorted(sbom['components'], key=lambda c: (c['name'], c.get('version', '')))):
    if component['type'] in ['file', 'operating-system']:
        continue
    licences = component.get('licenses', [])
    ids = []
    for j, entry in enumerate(licences):
        license = entry.get('license', {})
        ids.append(license.get('id') or license.get('name') or entry.get('expression', 'unknown'))
        text = license.get('text', {})
        if text.get('content'):
            content = text['content']
            if text.get('encoding') == 'base64':
                content = base64.b64decode(content).decode('utf-8', errors='replace')
            (texts / f'{i:05d}-{j}.txt').write_text(component['name'] + '\n' + content)
    lines.append(component['name'] + ' ' + component.get('version', '') + ' — ' + (', '.join(ids) if ids else 'scanner unresolved'))
    if not ids:
        unknown.append({'name': component['name'], 'version': component.get('version'), 'purl': component.get('purl')})
Path('THIRD-PARTY-NOTICES.txt').write_text('\n'.join(lines) + '\n')
Path('dist/license-audit.json').write_text(json.dumps({'schemaVersion':1,'status':'review-required',
    'retainedLicenseTextFiles':len(list(texts.glob('*.txt'))),
    'unresolvedScannerEntries':unknown,'note':'Includes scanner-detected Windows launcher helpers; inspect retained package/OS copyright files before distribution.'},indent=2)+'\n')
print(f'Notice inventory written; unresolved entries: {len(unknown)}')
