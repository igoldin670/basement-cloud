"""Opt-in Docker tests. Every writable bind is under a new /tmp fixture.

Production UUID checks are mocked only inside this test process; no CLI bypass
or production simulation switch exists. Never uses the server's real mounts.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cloud


def request(port, method, path, body=None, headers=None, authenticated=True):
    password=PASSWORD
    extra={'Authorization':'Basic '+base64.b64encode(('owner:'+password).encode()).decode()} if authenticated else {}
    extra.update(headers or {})
    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=body,headers=extra,method=method)
    try:
        with urllib.request.urlopen(req,timeout=180) as response:return response.status,response.read(),dict(response.headers)
    except urllib.error.HTTPError as exc:return exc.code,exc.read(),dict(exc.headers)


PASSWORD='test_' + secrets.token_urlsafe(24)
fixture=Path(tempfile.mkdtemp(prefix='basement-cloud-smoke-'))
assert fixture.parent==Path('/tmp') and fixture.name.startswith('basement-cloud-smoke-')
project='basement-cloud-smoke-'+secrets.token_hex(4)
instances=[]
images=json.loads((cloud.ROOT/'images.json').read_text())
original_images=images.copy()
if os.environ.get('BASEMENT_CLOUD_TEST_IMAGES'):
    images.update(json.loads(Path(os.environ['BASEMENT_CLOUD_TEST_IMAGES']).read_text()))
docker=['docker','--host=unix:///var/run/docker.sock']
env=os.environ.copy()
for key in cloud.SELECTORS:env.pop(key,None)
checks=[]
def passed(message):
    checks.append(message)
    print(message,flush=True)


def root_sh(code):
    return cloud.run([*docker,'run','--rm','--network','none','--entrypoint','/bin/sh',
        '--mount',f'type=bind,src={fixture},dst=/fixture',images['NGINX_IMAGE'],'-ec',code],env=env)


def fake_check(config, backup=True, free=False, primary=True):
    # Do not misrepresent real mounts; unit tests exercise the actual identity checks.
    return {role:{'path':Path(config[role]['mount']),'dev':Path(config[role]['mount']).stat().st_dev,
                 'physical':role,'free':2000*cloud.GIB} for role in ((['primary'] if primary else []) + (['backup'] if backup else []))}


def make_instance(name,port):
    home=fixture/name;home.mkdir()
    for role in ('primary','backup'):(home/role).mkdir()
    cfg=json.loads((cloud.ROOT/'server.example.json').read_text())
    cfg.update(http_port=port,https_port=port+100,quota_gib=1,reserve_gib=10)
    for role in ('primary','backup'):cfg[role]['mount']=str(home/role)
    state=home/'state'
    with patch.object(os,'chown'),patch.object(os,'fchown'):
        cloud.initialize(cfg,state,password=PASSWORD)
    # The daemon can chown fixture binds; the host test runner is unprivileged.
    prefix='/fixture/'+name
    root_sh(f'''chown 82:82 {prefix}/state/nextcloud {prefix}/state/nextcloud/config
        chgrp 82 {prefix}/state/secrets/*
        chown 82:82 {prefix}/primary/basement-cloud/data {prefix}/primary/basement-cloud/tmp/php
        chown 101:101 {prefix}/primary/basement-cloud/tmp/nginx''')
    # Loopback HTTP only in this test, production remains HTTPS via Tailscale.
    path=state/'compose.env';text=path.read_text().replace(f"CLOUD_HOST={cfg['hostname']}:{cfg['https_port']}",f'CLOUD_HOST=127.0.0.1:{port}')
    for key,value in images.items():
        text=text.replace(original_images[key],value)
    (state/'runtime/images.json').write_text(json.dumps(images))
    path.write_text(text)
    compose=state/'runtime/deploy/compose.yaml'
    compose.write_text(compose.read_text().replace('OVERWRITEPROTOCOL: https','OVERWRITEPROTOCOL: http').replace('OVERWRITECLIURL: https://','OVERWRITECLIURL: http://'))
    nc=cloud.Cloud(state,project+'-'+name);instances.append(nc)
    nc.compose('config','--quiet')
    return nc


real_usage=shutil.disk_usage
# Container layer storage is unusually expensive under managed VFS. Synthetic
# data is bounded to a few MiB; the OS reserve itself is tested separately.
def test_usage(path):
    usage=real_usage(path)
    if str(path).startswith(str(fixture)):
        return type(usage)(usage.total,usage.used,max(usage.free,20*cloud.GIB))
    return usage


try:
 with patch.object(cloud,'check_drives',side_effect=fake_check),patch.object(shutil,'disk_usage',side_effect=test_usage):
    first=make_instance('first',18081)
    first.start()
    assert first.occ('config:system:get','datadirectory').strip()=='/data'
    assert first.occ('config:system:get','memcache.locking').strip().endswith('Redis')
    assert first.occ('config:system:get','tempdirectory').strip()=='/upload-tmp'
    assert first.compose('exec','-T','app','php','-r','echo ini_get("upload_tmp_dir");').strip()=='/upload-tmp'
    assert request(18081,'GET','/login',authenticated=False)[0]==200
    dav='/remote.php/dav/files/owner'
    assert request(18081,'GET',dav,authenticated=False)[0]==401
    assert request(18081,'MKCOL',dav+'/Smoke')[0] in (201,405)
    data=secrets.token_bytes(2*1024*1024)
    assert request(18081,'PUT',dav+'/Smoke/original.bin',data)[0]==201
    assert request(18081,'GET',dav+'/Smoke/original.bin')[1]==data
    passed('Authenticated uploads/downloads preserve exact bytes; anonymous access is rejected')
    assert request(18081,'GET','/.well-known/carddav',authenticated=False)[0]==401
    assert request(18081,'GET','/config/config.php',authenticated=False)[0]==404
    assert request(18081,'GET','/data/owner/files/Smoke/original.bin',authenticated=False)[0]==404
    # A partially received upload survives a cold backup and resumes afterward.
    upload='/remote.php/dav/uploads/owner/synthetic-chunks'
    assert request(18081,'MKCOL',upload,headers={'OC-Total-Length':str(len(data))})[0]==201
    assert request(18081,'PUT',upload+'/00001',data[:len(data)//2])[0]==201
    first.backup()
    status=json.loads((first.state/'status.json').read_text())
    snapshot=status['last_backup']['snapshot'];assert not status['backup_running']
    assert request(18081,'PUT',upload+'/00002',data[len(data)//2:])[0]==201
    code,body,_=request(18081,'MOVE',upload+'/.file',headers={'Destination':'http://127.0.0.1:18081'+dav+'/Smoke/chunked.bin','OC-Total-Length':str(len(data))})
    assert code in (201,204),(code,body[:300])
    assert request(18081,'GET',dav+'/Smoke/chunked.bin')[1]==data
    passed('Consistent encrypted backup completes, resumes service, and preserves resumable uploads')
    # A failed backup cannot prune old history or strand maintenance mode.
    with patch.object(first,'restic',side_effect=cloud.CloudError('synthetic full backup disk')) as restic:
        try:first.backup()
        except cloud.CloudError:pass
        else:raise AssertionError('Expected backup failure')
        assert len(restic.call_args_list)==1 and restic.call_args.args[0][0]=='backup'
    assert request(18081,'GET',dav+'/Smoke/original.bin')[1]==data
    assert json.loads((first.state/'status.json').read_text())['last_backup']['snapshot']==snapshot
    passed('Backup failure preserves the prior snapshot, skips pruning, and restores service')
    assert request(18081,'PUT',dav+'/Smoke/original.bin',b'new live bytes')[0]==204
    recovery=fixture/'recovery';recovery.mkdir()
    first.restore(snapshot,recovery)
    # Read restored files through the root-only fixture helper (not private live paths).
    actual=root_sh('sha256sum /fixture/recovery/source/data/owner/files/Smoke/original.bin').split()[0]
    assert actual==hashlib.sha256(data).hexdigest()
    first.verify()
    passed('Old snapshot restores byte-identical originals after a live overwrite; full integrity check passes')
    # Rebuild a completely separate database and application from the snapshot.
    first.compose('down','--volumes')
    second=make_instance('second',18082)
    root_sh('''cp -a /fixture/recovery/source/nextcloud/. /fixture/second/state/nextcloud/
        cp -a /fixture/recovery/source/data/. /fixture/second/primary/basement-cloud/data/
        cp -a /fixture/recovery/source/secrets/. /fixture/second/state/secrets/''')
    # Source Docker PG bootstraps role cloud; Nextcloud additionally creates
    # oc_admin. A fresh PG uses postgres so the recorded roles restore without
    # conflicts, then pg_restore recreates cloud with original ownership/ACLs.
    restore_compose=second.state/'runtime/deploy/compose.yaml'
    normal_compose=restore_compose.read_text()
    restore_compose.write_text(normal_compose.replace('      POSTGRES_USER: cloud','      POSTGRES_USER: postgres').replace('      POSTGRES_DB: cloud','      POSTGRES_DB: postgres').replace('pg_isready -U cloud -d cloud','pg_isready -U postgres -d postgres'))
    second.compose('up','-d','--wait','db','redis')
    base=[*second.docker,'compose','--project-name',second.project,'--env-file',str(second.state/'compose.env'),'-f',str(restore_compose),'exec','-T','db']
    with (recovery/'source/database/roles.sql').open('rb') as stream:
        subprocess.run([*base,'psql','-U','postgres','-d','postgres','--set','ON_ERROR_STOP=on'],stdin=stream,env=env,check=True,stdout=subprocess.DEVNULL)
    with (recovery/'source/database/database.dump').open('rb') as stream:
        subprocess.run([*base,'pg_restore','-U','postgres','-d','postgres','--create','--exit-on-error'],stdin=stream,env=env,check=True)
    restore_compose.write_text(normal_compose)
    second.compose('up','-d','app')
    second.occ('config:system:set','trusted_domains','1','--value=127.0.0.1:18082')
    second.occ('maintenance:data-fingerprint')
    second.occ('maintenance:mode','--off')
    second.start()
    assert request(18082,'GET',dav+'/Smoke/original.bin')[1]==data
    passed('A fresh Nextcloud and PostgreSQL instance restores the saved account, metadata, and original download')
    # Destructive corruption is confined to this temporary Restic repository.
    root_sh('''pack=$(find /fixture/first/backup/basement-cloud-backups/restic/data -type f | head -1)
        test -n "$pack"
        dd if=/dev/zero of="$pack" bs=1 count=32 seek=64 conv=notrunc 2>/dev/null''')
    try:first.verify()
    except cloud.CloudError:pass
    else:raise AssertionError('Corrupted repository was not detected')
    passed('Full-data verification detects corruption in the temporary repository')
    print(f'All {len(checks)} integration scenarios passed')
    (cloud.ROOT/'integration-results.json').write_text(json.dumps({'checks':checks},indent=2)+'\n')
finally:
 for nc in reversed(instances):
    try:nc.compose('down','--volumes')
    except Exception as exc:print('Temporary container cleanup:',exc,file=sys.stderr)
 # No deletion command ever receives a real server path.
 try:
    root_sh('find /fixture -mindepth 1 -depth -delete')
    fixture.rmdir()
 except Exception as exc:print('Temporary fixture cleanup:',exc,file=sys.stderr)
