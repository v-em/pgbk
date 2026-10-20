"""Run real-tool fixtures in the built worker images; requires a local Moto bucket."""
import argparse
import json
import platform
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--major', action='append', type=int, choices=range(14, 19))
parser.add_argument('--mode', action='append', choices=['staged','direct','directory'])
parser.add_argument('--compression', default='gzip', choices=['none','gzip','lz4','zstd'])
parser.add_argument('--logs', default='/tmp/pgbk-integration-logs')
args = parser.parse_args()
reports = Path('tests/reports')
reports.mkdir(parents=True, exist_ok=True)
logs=Path(args.logs)
logs.mkdir(parents=True, exist_ok=True)
for major in args.major or range(14, 19):
    for mode in args.mode or ['staged']:
        image = f'pgbk-worker:{major}-test'
        command = ['docker', 'run', '--rm', '--platform', 'linux/amd64',
                   '-e', f'PGBK_TEST_MODE={mode}', '-e', f'PGBK_TEST_COMPRESSION={args.compression}']
        if platform.system() == 'Linux':
            command += ['--add-host', 'host.docker.internal:host-gateway']
        command += ['-v', str(Path('tests/integration').resolve()) + ':/tests:ro',
                    '--entrypoint', '/opt/pgbk/bin/python', image, '/tests/roundtrip.py']
        stem=f'modes-{major}-{mode}-{args.compression}'
        report_path=reports / (stem + '.json')
        with report_path.open('w') as output, (logs / (stem + '.log')).open('w') as log:
            subprocess.run(command, stdout=output, stderr=log, check=True)
        image_metadata = json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
        report = json.loads(report_path.read_text())
        report.update(pgbkVersion='0.2.0', workerImage=image, imageConfigId=image_metadata['Id'], registryDigests=image_metadata.get('RepoDigests', []))
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        print(f'PostgreSQL {major} {mode}/{args.compression}: {len(report["cases"])} cases passed', flush=True)
