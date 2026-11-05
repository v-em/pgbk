"""Real PG18/pg_cron backup Jobs and native restores with a local S3 service.

All source/destination provisioning belongs to this disposable test fixture.
The source fixture image must have pg_cron 1.6 installed. Workers use the
packaged product chart and real Kubernetes Leases, RBAC and S3 operations.
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import boto3
import psycopg
import yaml
from botocore.config import Config
from kubernetes import client, config as kube_config
from psycopg import sql

from pgbk import __version__, cli
from pgbk.catalog import Catalog
from pgbk.config import StrictLoader, normalize
from pgbk.kube import Kubernetes
from pgbk.process import Control
from pgbk.restore import logical_archive
from pgbk.selection import validate_toc
from pgbk.storage import S3, hash_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', help='Run only named cases while diagnosing the fixture')
    parser.add_argument('--context', default='pgbk-local')
    parser.add_argument('--helm', required=True)
    parser.add_argument('--chart', required=True)
    parser.add_argument('--worker-image', required=True)
    parser.add_argument('--source-image', required=True)
    parser.add_argument('--s3-endpoint', required=True)
    parser.add_argument('--s3-host-endpoint', default='http://127.0.0.1:19001')
    parser.add_argument('--logs', required=True)
    parser.add_argument('--output', default='tests/reports/schema-selection-minikube18.json')
    parser.add_argument('--allow-amd64-emulation', action='store_true')
    args = parser.parse_args()
    kube_config.load_kube_config(context=args.context)
    core, batch = client.CoreV1Api(), client.BatchV1Api()
    node = core.list_node().items[0]
    emulated = node.status.node_info.architecture != 'amd64'
    assert not emulated or args.allow_amd64_emulation
    namespace = 'pgbk-schema-' + uuid.uuid4().hex[:8]
    work = Path(tempfile.mkdtemp(prefix='pgbk-schema-')).resolve()
    logs = Path(args.logs).absolute()
    logs.mkdir(parents=True, exist_ok=True)
    os.environ['PGBK_SECRET_ROOT'] = str(work / 'secrets')
    source_hash = hashlib.sha256()
    for path in sorted(Path('src/pgbk').glob('*')):
        if path.suffix in {'.py', '.json', '.txt'}:
            source_hash.update(path.name.encode() + b'\0' + path.read_bytes())
    result = {'pgbkVersion': __version__, 'status': 'failed', 'workerImage': args.worker_image,
              'chart': args.chart, 'chartSha256': hashlib.sha256(Path(args.chart).read_bytes()).hexdigest(), 'sourceSha256': source_hash.hexdigest(), 'sourceImage': args.source_image,
              'kubernetes': node.status.node_info.kubelet_version, 'nodeArchitecture': node.status.node_info.architecture,
              'workerArchitecture': 'amd64', 'emulation': emulated, 's3': 'local Moto', 'requestedCases': args.case, 'cases': []}
    forwards = []
    original_s3 = cli.S3

    def kubectl(*command, input=None):
        return subprocess.check_output(['kubectl', '--context', args.context, '-n', namespace, *command], input=input, text=True)

    def source_sql(statement, database='fixture', deployment='source'):
        return kubectl('exec', '-i', 'deployment/' + deployment, '--', 'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1',
                       '-U', 'postgres', '-d', database, input=statement)

    def passed(name, **details):
        result['cases'].append({'case': name, 'status': 'passed', **details})
        print(name + ': passed', flush=True)

    def routed_s3(storage, control):
        s3 = S3(storage, control)
        s3.client = boto3.client('s3', endpoint_url=args.s3_host_endpoint, region_name='us-east-1',
            aws_access_key_id='fixture-access', aws_secret_access_key='fixture-secret',
            config=Config(retries={'max_attempts': 0}, s3={'addressing_style': 'path'}))
        return s3

    def command(*argv):
        parsed = cli.parser().parse_args(['--config', str(work/'values.yaml'), '--namespace', namespace,
                                         '--context', args.context, '--output', 'json', *argv])
        return cli.execute(parsed)

    def forward(service):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        log = (logs/(service+'-forward.log')).open('w')
        proc = subprocess.Popen(['kubectl', '--context', args.context, '-n', namespace, 'port-forward',
                                 'service/'+service, str(port)+':5432'], stdout=log, stderr=log)
        forwards.append((proc, log))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert proc.poll() is None, 'port-forward terminated'
            try:
                with psycopg.connect(host='127.0.0.1', port=port, user='postgres', password='fixture-password', dbname='fixture', connect_timeout=1):
                    return port
            except psycopg.OperationalError:
                time.sleep(.2)
        raise AssertionError('port-forward timeout')

    def wait(job, expected=True):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            current = batch.read_namespaced_job(job['job'], namespace)
            done = next((c.type for c in current.status.conditions or [] if c.status == 'True' and c.type in ['Complete', 'Failed']), None)
            if done:
                pods = core.list_namespaced_pod(namespace, label_selector='batch.kubernetes.io/controller-uid='+current.metadata.uid).items
                text = '\n'.join(core.read_namespaced_pod_log(p.metadata.name, namespace) for p in pods)
                (logs/(job['job']+'.log')).write_text(text)
                assert (done == 'Complete') == expected, job['job']
                decoder = json.JSONDecoder()
                for index in range(len(text)):
                    if text[index] == '{' and (index == 0 or text[index-1] == '\n'):
                        try:
                            payload, _ = decoder.raw_decode(text[index:])
                        except ValueError:
                            continue
                        if 'manifest' in payload or 'reasonCode' in payload or 'source' in payload or payload.get('status') in ['downloaded', 'completed']:
                            return payload
                raise AssertionError('Job has no final report')
            time.sleep(.5)
        raise AssertionError('Job timeout')

    try:
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace)))
        secrets = {'source': {'username': 'postgres', 'password': 'fixture-password'},
                   'backup': {'username': 'backup', 'password': 'fixture-password'},
                   's3': {'accessKeyId': 'fixture-access', 'secretAccessKey': 'fixture-secret'}}
        for ref, fields in secrets.items():
            directory = work/'secrets'/ref
            directory.mkdir(parents=True)
            for key, value in fields.items():
                (directory/key).write_text(value)
            core.create_namespaced_secret(namespace, client.V1Secret(metadata=client.V1ObjectMeta(name=ref), string_data=fields))
        for name in ['source', 'destination']:
            deploy = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name}, 'spec': {
                'replicas': 1, 'selector': {'matchLabels': {'fixture': name}}, 'template': {
                    'metadata': {'labels': {'fixture': name}}, 'spec': {'containers': [{'name': 'postgres',
                        'image': args.source_image, 'imagePullPolicy': 'IfNotPresent',
                        'args': ['postgres', '-c', 'shared_preload_libraries=pg_cron', '-c', 'cron.database_name=fixture', '-c', 'cron.launch_active_jobs=off'],
                        'env': [{'name': 'POSTGRES_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'source', 'key': 'password'}}},
                                {'name': 'POSTGRES_DB', 'value': 'fixture'}, {'name': 'PGDATA', 'value': '/data/pgdata'}],
                        'volumeMounts': [{'name': 'data', 'mountPath': '/data'}],
                        'readinessProbe': {'exec': {'command': ['pg_isready', '-U', 'postgres', '-d', 'fixture']}, 'periodSeconds': 2}}],
                        'volumes': [{'name': 'data', 'emptyDir': {}}]}}}}
            kubectl('apply', '-f', '-', input=yaml.safe_dump(deploy))
            core.create_namespaced_service(namespace, client.V1Service(metadata=client.V1ObjectMeta(name=name),
                spec=client.V1ServiceSpec(selector={'fixture': name}, ports=[client.V1ServicePort(port=5432)])))
            kubectl('rollout', 'status', 'deployment/'+name, '--timeout=180s')
        source_sql('''CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE ROLE backup LOGIN PASSWORD 'fixture-password';
CREATE TABLE public.items(id serial PRIMARY KEY, value text NOT NULL);
INSERT INTO public.items(value) SELECT md5(i::text) FROM generate_series(1,100) i;
CREATE SCHEMA reporting;
CREATE TYPE reporting.state AS ENUM ('ready');
CREATE TABLE reporting.events(id integer, state reporting.state);
INSERT INTO reporting.events VALUES (1,'ready');
CREATE VIEW reporting.summary AS SELECT count(*) AS count FROM public.items;
CREATE FUNCTION reporting.answer() RETURNS int LANGUAGE sql AS 'SELECT 42';
CREATE SCHEMA "a.*"" Ю";
CREATE TABLE "a.*"" Ю"."x.y"(id integer);
INSERT INTO "a.*"" Ю"."x.y" VALUES (73);
CREATE TABLE cron.private_table(id integer);
INSERT INTO cron.private_table VALUES (99);
CREATE TABLE public.parent(id int) PARTITION BY RANGE(id);
CREATE TABLE public.allowed PARTITION OF public.parent FOR VALUES FROM (0) TO (10);
CREATE TABLE cron.omitted PARTITION OF public.parent FOR VALUES FROM (10) TO (20);
INSERT INTO public.parent VALUES (1),(11);
SELECT cron.schedule('pgbk-canary','0 0 31 2 *','SELECT 17');
UPDATE cron.job SET active=false;
INSERT INTO cron.job_run_details(jobid,runid,username,database,command,status)
SELECT jobid,9001,username,database,command,'succeeded' FROM cron.job;
SELECT lo_from_bytea(0,decode('cafebabe','hex'));
REVOKE ALL ON ALL TABLES IN SCHEMA cron FROM PUBLIC,backup;
REVOKE ALL ON SCHEMA cron FROM PUBLIC,backup;
GRANT USAGE ON SCHEMA public,reporting,"a.*"" Ю" TO backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public,reporting,"a.*"" Ю" TO backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public,reporting TO backup;
DO $$DECLARE r record; BEGIN FOR r IN SELECT oid FROM pg_largeobject_metadata LOOP
EXECUTE format('GRANT SELECT ON LARGE OBJECT %s TO backup',r.oid); END LOOP; END$$;
''')
        source_sql('CREATE DATABASE second;', 'postgres')
        source_sql('CREATE TABLE public.items(id int); INSERT INTO public.items VALUES(5); GRANT SELECT ON public.items TO backup;', 'second')
        result['postgres'] = source_sql('SHOW server_version').strip()
        result['pgCron'] = source_sql("SELECT extversion FROM pg_extension WHERE extname='pg_cron'").strip()
        result['sourceExtconfigRows'] = int(source_sql('SELECT count(*) FROM cron.job').strip())
        assert result['sourceExtconfigRows'] > 0
        assert source_sql("SELECT has_table_privilege('backup','cron.job','SELECT')").strip() == 'f'
        storage = {'bucket': namespace, 'prefix': 'postgres', 'endpoint': args.s3_endpoint, 'region': 'us-east-1', 'credentialsSecretRef': 's3', 'tls': {'verify': True}, 'addressingStyle': 'path'}
        routed_s3(storage, Control()).client.create_bucket(Bucket=namespace)
        resources = {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '2', 'memory': '1Gi'}}
        target = {'host': 'source.'+namespace+'.svc.cluster.local', 'sourceId': 'fixture', 'concurrencyKey': namespace,
                  'serverMajor': 18, 'credentialsSecretRef': 'backup', 'tls': {'mode': 'disable'}}
        values = {'installationId': namespace, 'coordinationNamespace': namespace, 'rbac': {'create': True},
                  'coordination': {'createLeases': True}, 'images': {'18': args.worker_image}, 'storage': {'archive': storage},
                  'targets': {'fixture': target, 'admin': {**target, 'credentialsSecretRef': 'source'}}, 'profiles': {}}
        variants = [('schema-direct', 'direct'), ('schema-staged', 'staged'), ('schema-directory', 'directory'),
                    ('table-manual', 'direct'), ('table-query', 'staged'), ('selected', 'direct'),
                    ('schema-include', 'staged'), ('literal', 'direct'), ('empty-second', 'direct')]
        for name, mode in variants:
            logical = {'databases': ['fixture'], 'format': 'directory' if mode == 'directory' else 'custom',
                       'jobs': 2 if mode == 'directory' else 1, 'compression': {'method': 'lz4'},
                       'transfer': {'mode': 'direct' if mode == 'direct' else 'staged', 'partSizeMiB': 5}, 'globals': 'disabled'}
            if name.startswith('schema-'):
                logical['schemaSelection'] = {'exclude': ['cron', 'missing']}
            if name == 'schema-direct':
                logical['databases'].append('second')
            if name == 'schema-include':
                logical['schemaSelection'] = {'include': ['public', 'reporting', 'cron'], 'exclude': ['cron']}
            if name in ['literal', 'empty-second']:
                logical['schemaSelection'] = {'include': ['a.*" Ю']}
                if name == 'empty-second':
                    logical['databases'].append('second')
            if name.startswith('table-'):
                exclude = {'tables': [{'schema': 'cron', 'table': 'job'}, {'schema': 'cron', 'table': 'job_run_details'}]}
                if name == 'table-query':
                    exclude = {'query': "SELECT 'cron'::text AS schema_name, unnest(ARRAY['job','job_run_details'])::text AS table_name"}
                logical['tableSelection'] = {'byDatabase': {'fixture': {'mode': 'all', 'exclude': exclude}}}
            if name == 'selected':
                logical['schemaSelection'] = {'exclude': ['cron']}
                logical['tableSelection'] = {'byDatabase': {'fixture': {'mode': 'selected', 'include': {'tables': [{'schema': 'public', 'table': 'parent'}]}}}}
            values['profiles'][name] = {'target': 'admin' if name.startswith('table-') else 'fixture', 'kind': 'logical', 'storage': 'archive',
                'logical': logical, 'serviceAccounts': {'create': True}, 'execution': {'resources': resources},
                'download': {'allowedDestinationClaims': ['download-'+name]},
                'restore': {'enabled': True, 'namespace': namespace, 'resources': resources,
                            'scratch': {'type': 'emptyDir', 'size': '2Gi'}, 'timeoutSeconds': 600,
                            'credentialsSecretRefs': ['source'], 'allowedDestinationClaims': []},
                'scratch': {'type': 'emptyDir' if mode == 'direct' else 'pvc', 'size': '2Gi', 'storageClassName': 'standard'},
                'schedule': {'enabled': False}, 'retention': {'schedule': {'enabled': False}}, 'verification': {'enabled': False}}
            core.create_namespaced_persistent_volume_claim(namespace, client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name='download-'+name, labels={'pgbk.io/installation': namespace, 'pgbk.io/purpose': 'download'}),
                spec=client.V1PersistentVolumeClaimSpec(access_modes=['ReadWriteOnce'], storage_class_name='standard',
                    resources=client.V1VolumeResourceRequirements(requests={'storage': '1Gi'}))))
        values['verificationReconciliation'] = {'enabled': False}
        normalize(values)
        (work/'values.yaml').write_text(yaml.safe_dump(values))
        rendered = subprocess.check_output([args.helm, 'template', 'pgbk', args.chart, '-n', namespace, '-f', str(work/'values.yaml')])
        for document in yaml.load_all(rendered, Loader=StrictLoader):
            if document and document.get('kind') == 'Role':
                assert not {'subjects', 'roleRef'} & set(document)
        subprocess.run([args.helm, 'upgrade', '--install', 'pgbk', args.chart, '--kube-context', args.context,
                        '-n', namespace, '-f', str(work/'values.yaml')], check=True, stdout=subprocess.DEVNULL)
        if emulated:
            for cron in batch.list_namespaced_cron_job(namespace).items:
                batch.patch_namespaced_cron_job(cron.metadata.name, namespace,
                    {'spec': {'jobTemplate': {'spec': {'template': {'spec': {'nodeSelector': {'kubernetes.io/arch': None}}}}}}})
        assert kubectl('auth', 'can-i', 'list', 'pods', '--as=system:serviceaccount:'+namespace+':pgbk-schema-direct-restore').strip() == 'yes'
        passed('restore-rbac-document-boundaries-and-real-authorization')
        cli.S3 = routed_s3
        destination_port = forward('destination')
        config = normalize(values)
        preview = wait(Kubernetes(namespace, args.context).launch('schema-direct', 'preflight'))
        assert preview['source']['fixture']['selectionPreview']['schemaSelection']['unmatchedExclude'] == ['missing']
        assert preview['source']['second']['selectionPreview']['schemaSelection']['unmatchedExclude'] == ['cron', 'missing']
        passed('preflight-per-database-schema-resolution-without-cron-read-privileges')
        for name, mode in variants:
            if args.case and name not in args.case:
                continue
            payload = wait(command('backup', 'run', '--profile', name), expected=name != 'empty-second')
            catalog = Catalog(routed_s3(config['storage']['archive'], Control()), config, name)
            if name == 'empty-second':
                assert payload['reasonCode'] == 'EMPTY_SCHEMA_SELECTION', payload
                assert not any(row.get('artifactStatus') == 'Completed' for row in catalog.list()["backups"])
                passed('missing-included-schema-in-second-database-fails-without-success-publication')
                continue
            manifest = payload['manifest']
            assert payload['status'] == 'successful'
            destination = work/name
            downloaded = wait(command('backup', 'download', '--profile', name, '--run-id', payload['runId'],
                                      '--destination-pvc', 'download-'+name, '--destination', '/destination/copy',
                                      '--acknowledge-dedicated-pvc'))
            assert downloaded['status'] == 'downloaded'
            reader = 'reader-'+name
            core.create_namespaced_pod(namespace, client.V1Pod(metadata=client.V1ObjectMeta(name=reader), spec=client.V1PodSpec(
                restart_policy='Never', automount_service_account_token=False,
                security_context=client.V1PodSecurityContext(run_as_user=999, run_as_group=999, fs_group=999),
                containers=[client.V1Container(name='reader', image=args.worker_image, command=['sleep','600'],
                    volume_mounts=[client.V1VolumeMount(name='download', mount_path='/downloads', read_only=True)])],
                volumes=[client.V1Volume(name='download', persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name='download-'+name, read_only=True))])))
            kubectl('wait', '--for=condition=Ready', 'pod/'+reader, '--timeout=120s')
            bundle = work/(name+'.tar')
            with bundle.open('wb') as output:
                subprocess.run(['kubectl','--context',args.context,'-n',namespace,'exec',reader,'--',
                                'tar','-C','/downloads/copy','-cf','-','.'], stdout=output, check=True)
            destination.mkdir()
            with tarfile.open(bundle) as archive:
                archive.extractall(destination, filter='data')
            for artifact in manifest['artifacts']:
                assert hash_file(destination/artifact['filename']) == (artifact['sha256'], artifact['size'])
            for item in manifest['artifacts']:
                assert item['scope'] == 'filtered-database'
                with logical_archive(item, destination, work, Control()) as archive:
                    toc = Control().run(['pg_restore', '--list', str(archive)], capture=True)
                    validate_toc(toc, item['selection'])
                source_sql('DROP DATABASE fixture WITH (FORCE); CREATE DATABASE fixture;', 'postgres', 'destination')
                target_destination = {'host': 'destination.'+namespace+'.svc.cluster.local', 'port': 5432,
                                      'database': 'fixture', 'serverMajor': 18, 'acknowledgeIsolated': True,
                                      'credentialsSecretRef': 'source', 'tls': {'mode': 'disable'}}
                target_file = work/'destination.json'
                target_file.write_text(json.dumps(target_destination))
                try:
                    restored_job = wait(command('restore', 'logical', '--profile', name, '--run-id', payload['runId'],
                                               '--database', item['database'], '--destination-config', str(target_file), '--as-job'))
                except AssertionError:
                    source_sql('DROP DATABASE fixture WITH (FORCE); CREATE DATABASE fixture;', 'postgres', 'destination')
                    # Synthetic fixture diagnostics only, retained outside the public repository.
                    program = '''import json,os,subprocess,sys,tempfile
from pathlib import Path
from pgbk.restore import logical_archive
from pgbk.process import Control
item=json.loads(sys.argv[1])
with tempfile.TemporaryDirectory(dir='/scratch') as temp:
    with logical_archive(item,Path('/downloads/copy'),Path(temp),Control()) as archive:
        subprocess.run(['pg_restore','--exit-on-error','--no-owner','--no-acl','--no-tablespaces','--dbname=fixture',str(archive)],
            env=os.environ|{'PGHOST':sys.argv[2],'PGUSER':'postgres','PGPASSWORD':'fixture-password','PGDATABASE':'fixture','PGSSLMODE':'disable'})
'''
                    with (logs/'native-restore-error.log').open('w') as output:
                        subprocess.run(['kubectl','--context',args.context,'-n',namespace,'exec',reader,'--',
                            'python','-c',program,json.dumps(item),target_destination['host']], stdout=output, stderr=output)
                    raise
                assert restored_job['status'] == 'completed'
                with psycopg.connect(host='127.0.0.1', port=destination_port, user='postgres', password='fixture-password', dbname='fixture') as restored:
                    if name in ['literal']:
                        assert restored.execute('SELECT id FROM "a.*"" Ю"."x.y"').fetchone() == (73,)
                        assert restored.execute("SELECT to_regclass('public.items')").fetchone() == (None,)
                    elif name == 'selected':
                        assert restored.execute('SELECT array_agg(id ORDER BY id) FROM public.parent').fetchone() == ([1],)
                        assert restored.execute("SELECT to_regclass('cron.omitted')").fetchone() == (None,)
                        assert item['selection']['filteredBySchema'][0]['schema'] == 'cron'
                    else:
                        expected_rows = 1 if item['database'] == 'second' else 100
                        assert restored.execute('SELECT count(*) FROM public.items').fetchone() == (expected_rows,)
                        if item['database'] == 'fixture':
                            assert restored.execute('SELECT reporting.answer(), (SELECT count FROM reporting.summary)').fetchone() == (42, 100)
                            assert restored.execute('SELECT count(*) FROM reporting.events').fetchone() == (1,)
                            assert restored.execute('SELECT count(*) FROM public.parent').fetchone() == ((2 if name.startswith('table-') else 1),)
                            assert restored.execute('SELECT count(*) FROM pg_largeobject_metadata').fetchone() == ((0 if name == 'schema-include' else 1),)
                            if name != 'schema-include':
                                assert restored.execute('SELECT count(*) FROM cron.job').fetchone() == (0,)
                                assert restored.execute('SELECT count(*) FROM cron.job_run_details').fetchone() == (0,)
                                assert restored.execute("SELECT extversion FROM pg_extension WHERE extname='pg_cron'").fetchone() == (result['pgCron'],)
                                assert restored.execute("SELECT to_regclass('cron.private_table') IS NOT NULL").fetchone() == (name.startswith('table-'),)
            core.delete_namespaced_pod(reader, namespace)
            passed(name+'-archive-and-native-restore', databases=len(manifest['artifacts']), transfer=mode)
        result['status'] = 'passed'
    except Exception as error:
        result['errorType'] = type(error).__name__
        raise
    finally:
        cli.S3 = original_s3
        Path(args.output).write_text(json.dumps(result, indent=2)+'\n')
        for proc, log in forwards:
            proc.terminate()
            proc.wait(timeout=10)
            log.close()
        try:
            core.delete_namespace(namespace)
        finally:
            shutil.rmtree(work)


if __name__ == '__main__':
    main()
