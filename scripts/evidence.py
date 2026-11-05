"""Map retained subcase evidence without claiming full unexecuted acceptance cases."""
import json
import tomllib
import re
from datetime import datetime,timezone
from pathlib import Path

root=Path('tests/reports')
spec=Path('docs/en/specification.md').read_text()
plan=Path('docs/en/test-plan.md').read_text()
requirements=sorted(set(re.findall(r'REQ-\d{3}',spec)))
# Specification 1.2 defines these features before runtime implementation.
# Keep their acceptance coverage visible without inventing implementation evidence.
planned_requirements={f'REQ-{number:03}' for number in [*range(46,49),54]}
tests={}
for line in plan.splitlines():
    if re.match(r'\| T-\d{3} \|',line):
        parts=line.split('|')
        tests[parts[1].strip()]={'requirements':sorted(set(re.findall(r'REQ-\d{3}',parts[2]))),'fullAcceptanceStatus':'not-run','subcaseEvidence':[]}
subcases={
 'integration':[2,3,5,8,10,11,13,15,23,29,30,31,32,35,38,39,40,41,42,43,45],
 'kubernetes':[7,19,20,21,22,23,27,28,31,36],
 'unit':[6,13,14,16,17,18,21,22,23,29,31,38,39,40,41,42,43],
 'm0':[1,2,3,4], 'benchmark':[34],
 'schema':[46,47,48,49,50,51],
 'spec13':[59,60,61,62,63,64,65,66,67,68],
 'unit-spec13':[59,60,62,63,64,65,66,67]}
evidence=[]
for filename in sorted(root.glob('*.json')):
    if filename.name=='acceptance.json' or filename.stat().st_size==0: continue
    report=json.loads(filename.read_text())
    kind=filename.stem.split('-')[0]
    if filename.name=='unit-spec13.json': kind='unit-spec13'
    if filename.name.startswith('spec13-offline'): kind='offline-spec13'
    if kind=='modes': kind='integration'
    record={'path':str(filename),'kind':kind,'summary':report}
    evidence.append(record)
    for number in ([68] if kind=='offline-spec13' else subcases.get(kind,[])):
        tests[f'T-{number:03}']['subcaseEvidence'].append(str(filename))
# T-002 is precisely the retained no-archive source-offline native test for all majors.
if all((root/f'm0-{major}.json').exists() and json.loads((root/f'm0-{major}.json').read_text())['status']=='passed' for major in range(14,19)):
    tests['T-002']['fullAcceptanceStatus']='passed'
features={
 'src/pgbk/config.py':[2,3,7,12,13,14,15,20,34,38,39,43,44,45,49,51],
 'src/pgbk/postgres.py':[5,6,7,8,10,11,12,16,17,18,19],
 'src/pgbk/runner.py':[9,14,18,19,21,22,23,24,32,50,51,53],
 'src/pgbk/process.py':[14,21,23,34],
 'src/pgbk/kube.py':[9,15,20,21,22,23,29,33,49,51,52,53],
 'src/pgbk/storage.py':[10,11,14,19,23,25],
 'src/pgbk/catalog.py':[19,21,22,24,26,27,32,50,51,52,53],
 'src/pgbk/verification.py':[15,26,30,32,49,50,51,52],
 'src/pgbk/selection.py':[16,38,39,40,41,42,43,44,45],
 'src/pgbk/restore.py':[14,19,27,28,29,30,42,49,51,53,55],
 'src/pgbk/cli.py':[1,9,20,22,27,28,29,30,31,32,49,52,53],
 'charts/pgbk':[1,4,9,12,14,15,20,21,30,33,34,49,52,53],
 'docs/en':[4,5,6,7,8,11,12,14,15,16,17,20,21,23,25,28,29,30,32,34,35,36,37,38,39,40,41,42],
 'images/Dockerfile':[1,4,10,11,12,35], '.github/workflows/test.yaml':[35,37]}
coverage=[]
for req in requirements:
    num=int(req[-3:])
    coverage.append({'id':req,'implementationPaths':[p for p,ids in features.items() if num in ids],
                     **({'implementationStatus':'planned; specification only'} if req in planned_requirements else {}),
                     'acceptanceTests':[tid for tid,t in tests.items() if req in t['requirements']],
                     'releaseCertification':'incomplete; inspect full-case gates and subcase evidence'})
result={'schemaVersion':1,'version':tomllib.loads(Path('pyproject.toml').read_text())['project']['version'],'generatedAt':datetime.now(timezone.utc).isoformat(),
        'specificationVersion':'1.3','productionReady':False,'requirements':coverage,'tests':tests,'evidence':evidence,
        'openGates':['Yandex live-provider tests are performed by the user','Live managed and pinned Bitnami certification',
            'Minimum Kubernetes and native amd64 certification; local minikube arm64/emulated-amd64 subcases are separate','Comprehensive TLS, source mutation audit, failover/concurrent writes/DDL, node-loss and S3 fault injection',
            'Globals, large objects and external filtered dependency acceptance','Final license audit and bit-identical build reproducibility',
            'Any remaining full acceptance procedures in docs/en/test-plan.md'],
        'note':'Only precisely completed full procedures are marked passed. Subcase successes do not certify the entire acceptance procedure.'}
assert set(requirements)=={f'REQ-{n:03}' for n in range(1,len(requirements)+1)}
assert set(tests)=={f'T-{n:03}' for n in range(1,len(tests)+1)}
assert all((r['implementationPaths'] or r['id'] in planned_requirements) and r['acceptanceTests'] for r in coverage)
(root/'acceptance.json').write_text(json.dumps(result,indent=2)+'\n')
print(f'{len(coverage)} requirements / {len(tests)} acceptance cases mapped')
