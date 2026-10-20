"""Synthetic capacity exercise, not a throughput guarantee; run in a worker image."""
import copy
import json
import os
import resource
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psycopg
from pgbk.catalog import Catalog
from pgbk.config import normalize
from pgbk.postgres import identity
from pgbk.process import Control
from pgbk.restore import drill, server
from pgbk.runner import backup
from pgbk.storage import S3
from roundtrip import FixtureKube


def main():
    size = int(os.environ['PGBK_BENCH_GIB'])
    rows = size * 1024**3 // 16384
    work = Path(tempfile.mkdtemp(prefix='pgbk-capacity-'))
    scratch = work / 'scratch'
    scratch.mkdir()
    reports = []
    owned_catalogs = []
    peak = {'scratchBytes':0, 'processRssBytes':0}
    finished = threading.Event()
    def sample():
        while not finished.wait(2):
            total = 0
            for root, _, files in os.walk(scratch):
                for name in files:
                    try:
                        path = Path(root)/name
                        if not path.is_symlink(): total += path.stat().st_size
                    except OSError: pass
            peak['scratchBytes'] = max(peak['scratchBytes'],total)
            rss = 0
            for status in Path('/proc').glob('[0-9]*/status'):
                try:
                    for line in status.read_text().splitlines():
                        if line.startswith('VmRSS:'): rss += int(line.split()[1])*1024
                except OSError: pass
            peak['processRssBytes'] = max(peak['processRssBytes'],rss)
    monitor = threading.Thread(target=sample,daemon=True)
    monitor.start()
    control = Control(7200)
    kube = FixtureKube()
    try:
        secrets = work/'secrets'
        for ref,values in {'pg':{'username':'postgres','password':'fixture-only'},
                           's3':{'accessKeyId':'fixture-access','secretAccessKey':'fixture-secret-only'}}.items():
            (secrets/ref).mkdir(parents=True)
            for key,value in values.items(): (secrets/ref/key).write_text(value)
        os.environ['PGBK_SECRET_ROOT']=str(secrets)
        budget={'requests':{'cpu':'1','memory':'1Gi'},'limits':{'cpu':'2','memory':'4Gi'}}
        profile={'target':'fixture','storage':'archive','kind':'logical','logical':{'databases':['capacity']},
                 'serviceAccounts':{'create':True},'execution':{'resources':budget,'activeDeadlineSeconds':7200},
                 'scratch':{'type':'emptyDir','size':'120Gi'}}
        raw={'installationId':'capacity-'+uuid.uuid4().hex[:12],
             'storage':{'archive':{'bucket':'pgbk-test','prefix':'capacity','endpoint':os.environ.get('PGBK_TEST_S3','http://host.docker.internal:19000'),
                                   'region':'us-east-1','credentialsSecretRef':'s3'}},
             'targets':{'fixture':{'sourceId':'fixture','concurrencyKey':'fixture','host':'127.0.0.1','port':15499,'serverMajor':int(os.environ['PG_MAJOR']),
                                   'credentialsSecretRef':'pg','tls':{'mode':'disable'}}},'profiles':{'logical':profile}}
        pgdata=work/'source'
        subprocess.run(['initdb','-D',str(pgdata),'--auth=trust'],check=True,stdout=subprocess.DEVNULL)
        with server(pgdata,control,work):
            for _ in range(60):
                try:
                    with psycopg.connect(host='127.0.0.1',port=15499,user='postgres',dbname='postgres',autocommit=True) as conn:
                        conn.execute('CREATE DATABASE capacity')
                    break
                except psycopg.OperationalError: control.stop.wait(.5)
            else: raise AssertionError('Fixture startup failed')
            with psycopg.connect(host='127.0.0.1',port=15499,user='postgres',dbname='capacity',autocommit=True) as conn:
                conn.execute('CREATE TABLE payloads(id integer PRIMARY KEY,payload text)')
                conn.execute('ALTER TABLE payloads ALTER COLUMN payload SET STORAGE EXTERNAL')
                conn.execute('INSERT INTO payloads SELECT g,repeat(md5(g::text),512) FROM generate_series(1,%s) g',(rows,))
                conn.execute('CHECKPOINT')
                table_bytes=conn.execute("SELECT pg_total_relation_size('payloads')").fetchone()[0]
                assert table_bytes >= size * 1024**3
            config=normalize(raw)
            ident=identity(config['targets']['fixture'])
            raw['targets']['fixture'].update(physicalEndpoint='direct-primary',expectedSystemIdentifier=ident['systemIdentifier'])
            physical=copy.deepcopy(profile); physical.pop('logical'); physical['kind']='physical-full'
            raw['profiles']['physical']=physical
            config=normalize(raw)
            results={}
            for kind in ['logical','physical']:
                started=time.monotonic()
                result=backup(config,kind,kube,Control(7200),scratch)
                catalog=Catalog(S3(config['storage']['archive'],control),config,kind)
                owned_catalogs.append(catalog)
                results[kind]=result
                reports.append({'kind':kind,'runId':result['runId'],'backupSeconds':time.monotonic()-started,
                                'artifactBytes':result['manifest']['bytes'],'tools':result['manifest']['tools']})
        # Original source stopped; both restore drills consume only S3 artifacts.
        for report in reports:
            kind=report['kind']
            config['profiles'][kind]['verification']={'enabled':True,'namespace':'verification','timeoutSeconds':7200,
                'schedule':{'enabled':False},'image':'local/worker@sha256:'+'0'*64,'resources':budget,
                'scratch':{'type':'emptyDir','size':'120Gi'},'credentialsSecretRef':'pg','clientCredentialsSecretRef':'pg',
                'assertions':['SELECT NOT pg_is_in_recovery()']}
            started=time.monotonic()
            catalog=next(c for c in owned_catalogs if c.scope['profileId']==kind)
            restored=drill(config,kind,catalog,kube,Control(7200),scratch,results[kind]['runId'])
            assert restored['status']=='passed'
            checks=next(c for c in restored['checks'] if c['database']=='capacity')
            assert next(t for t in checks['tableRowCounts'] if t['table']=='payloads')['rows']==rows
            report.update(restoreSeconds=time.monotonic()-started,sourceUnavailable=True,status='passed')
        print(json.dumps({'schemaVersion':1,'datasetGiB':size,'rows':rows,'payloadBytes':size * 1024**3,
                          'sourceRelationBytes':table_bytes,'cases':reports,'observedPeaks':peak,'samplingSeconds':2,
                          'controllerPeakRssBytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                          'nativeChildPeakRssBytes':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024,
                          'environment':'Docker Desktop amd64 emulation; S3 Moto host outside container; Kubernetes stubbed',
                          'fixture':'16KiB repeated per-row md5 text, uncompressed PostgreSQL TOAST; logical archive compressible',
                          'memoryLimitBytes':4*1024**3,'cpuLimit':2}))
    finally:
        finished.set(); monitor.join(timeout=5)
        for catalog in owned_catalogs:
            # Only unique disposable benchmark installation/profile scopes.
            catalog.s3.control=Control(120)
            for key in list(catalog.s3.keys(catalog.root)): catalog.s3.delete(key)
        shutil.rmtree(work)


if __name__=='__main__': main()
