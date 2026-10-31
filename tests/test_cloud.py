import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import cloud


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads((cloud.ROOT / 'server.example.json').read_text())
        for role in ('primary', 'backup'):
            path = self.root / role; path.mkdir()
            self.config[role]['mount'] = str(path)
        self.state = self.root / 'state'

    def mount_info(self, name):
        return dict(path=Path(self.config[name]['mount']), dev=1 if name=='primary' else 2,
                    physical=name, free=2_000*cloud.GIB)

    def instance(self):
        self.state.mkdir()
        (self.state/'runtime').mkdir()
        (self.state/'config.json').write_text(json.dumps(self.config))
        (self.state/'runtime/images.json').write_text((cloud.ROOT/'images.json').read_text())
        return cloud.Cloud(self.state, 'basement-cloud-test')

    def test_supplied_server_excludes_missing_drive_two_and_os(self):
        cfg=json.loads((cloud.ROOT/'server.example.json').read_text())
        self.assertEqual(cfg['primary']['uuid'],'d10eb869-4458-449e-880a-36ad70210d1d')
        self.assertEqual(cfg['backup']['uuid'],'f0b3d701-2343-46d2-b2b2-09791ba22b2d')
        self.assertNotIn('album-drive-2',json.dumps(cfg))

    def test_overlapping_or_symlinked_mounts_rejected(self):
        bad=copy.deepcopy(self.config);bad['backup']['mount']=bad['primary']['mount']
        with self.assertRaises(cloud.CloudError):cloud.validate_config(bad)
        link=self.root/'link';link.symlink_to(self.root/'primary',target_is_directory=True)
        self.config['primary']['mount']=str(link)
        with self.assertRaises(cloud.CloudError):cloud.validate_config(self.config)

    def test_uuid_and_existing_album_ports_rejected(self):
        for key,value in [('system_uuid','bad'),('http_port',8080),('https_port',443),('username','$(id)'),('hostname','example.com')]:
            with self.subTest(key=key):
                cfg=copy.deepcopy(self.config);cfg[key]=value
                with self.assertRaises((cloud.CloudError,ValueError)):cloud.validate_config(cfg)
        self.config['backup']['uuid']=self.config['primary']['uuid']
        with self.assertRaises(cloud.CloudError):cloud.validate_config(self.config)

    def test_two_partitions_on_same_physical_disk_rejected(self):
        def inspect(path,uuid,writable=True):return {'physical':'os' if str(path)=='/' else 'same-disk','free':2000*cloud.GIB}
        with patch.object(cloud,'inspect_mount',side_effect=inspect):
            with self.assertRaises(cloud.CloudError):cloud.check_drives(self.config)

    def test_os_disk_never_accepted_for_data(self):
        with patch.object(cloud,'inspect_mount',return_value={'physical':'os','free':2000*cloud.GIB}):
            with self.assertRaises(cloud.CloudError):cloud.check_drives(self.config)

    def test_missing_actual_mount_fails_even_when_directory_exists(self):
        with patch.object(cloud,'run',return_value=json.dumps({'filesystems':[]})):
            with self.assertRaises(cloud.CloudError):cloud.inspect_mount(self.root/'primary',self.config['primary']['uuid'])

    def test_wrong_uuid_or_read_only_mount_fails(self):
        row={'target':str(self.root/'primary'),'fstype':'ext4','uuid':'wrong','options':'rw','maj:min':'1:2'}
        with patch.object(cloud,'run',return_value=json.dumps({'filesystems':[row]})):
            with self.assertRaises(cloud.CloudError):cloud.inspect_mount(self.root/'primary',self.config['primary']['uuid'])
        row.update(uuid=self.config['primary']['uuid'],options='ro')
        with patch.object(cloud,'run',return_value=json.dumps({'filesystems':[row]})):
            with self.assertRaises(cloud.CloudError):cloud.inspect_mount(self.root/'primary',self.config['primary']['uuid'])

    def test_initialize_bad_drive_does_not_create_anything(self):
        with patch.object(cloud,'check_drives',side_effect=cloud.CloudError('wrong UUID')),patch.object(Path,'mkdir') as mkdir:
            with self.assertRaises(cloud.CloudError):cloud.initialize(self.config,self.state,password='synthetic-password')
            mkdir.assert_not_called()

    def test_quota_preserves_space_for_history(self):
        found={x:self.mount_info(x) for x in ('primary','backup')};found['backup']['free']=200*cloud.GIB
        with patch.object(cloud,'check_drives',return_value=found),patch.object(Path,'mkdir') as mkdir:
            with self.assertRaises(cloud.CloudError):cloud.initialize(self.config,self.state,password='synthetic-password')
            mkdir.assert_not_called()

    def test_existing_namespace_is_never_reused_or_chowned(self):
        found={x:self.mount_info(x) for x in ('primary','backup')}
        (self.root/'backup/basement-cloud-backups').mkdir()
        with patch.object(cloud,'check_drives',return_value=found),patch.object(os,'chown') as chown:
            with self.assertRaises(cloud.CloudError):cloud.initialize(self.config,self.state,password='synthetic-password')
            chown.assert_not_called()
        self.assertFalse((self.root/'primary/basement-cloud').exists())

    def test_pinned_directory_detects_device_swap(self):
        info=self.mount_info('primary');info['dev']=-1
        with self.assertRaises(cloud.CloudError):
            with cloud.pinned_mount(info):pass

    def test_child_creation_refuses_symlink(self):
        (self.root/'primary/link').symlink_to(self.root/'backup',target_is_directory=True)
        fd=os.open(self.root/'primary',os.O_RDONLY|os.O_DIRECTORY)
        try:
            with self.assertRaises(OSError):cloud.make_at(fd,'link')
        finally:os.close(fd)

    def test_write_new_never_overwrites_files_or_symlinks(self):
        path=self.root/'secret';cloud.write_new(path,'first')
        with self.assertRaises(FileExistsError):cloud.write_new(path,'second')
        self.assertEqual(path.read_text(),'first')
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(FileExistsError):cloud.write_new(link,'third')

    def test_modes_are_explicit_even_under_restrictive_umask(self):
        old=os.umask(0o077)
        try:cloud.write_new(self.root/'readable','data',0o440)
        finally:os.umask(old)
        self.assertEqual((self.root/'readable').stat().st_mode&0o777,0o440)

    def test_atomic_status_preserves_previous_success_on_failure(self):
        c=self.instance();c.record(last_backup={'snapshot':'previous'})
        c.record(last_failure={'message':'disk full'})
        result=json.loads((self.state/'status.json').read_text())
        self.assertEqual(result['last_backup']['snapshot'],'previous')
        self.assertEqual((self.state/'status.json').stat().st_mode&0o777,0o600)

    def test_health_does_not_restart_manually_stopped_service(self):
        c=self.instance();c.record(desired_running=False)
        with patch.object(c,'start') as start,patch.object(cloud,'check_drives') as check:
            c.health();start.assert_not_called();check.assert_not_called()

    def test_health_stops_writers_when_primary_is_wrong(self):
        c=self.instance()
        with patch.object(cloud,'check_drives',side_effect=cloud.CloudError('wrong UUID')),patch.object(c,'compose') as compose:
            with self.assertRaises(cloud.CloudError):c.health()
            self.assertIn('stop',compose.call_args.args)

    def test_restore_cannot_target_live_storage_or_nonempty_folder(self):
        c=self.instance()
        for target in [self.root/'primary',self.root,self.state,self.root/'missing']:
            with self.subTest(target=target),self.assertRaises(cloud.CloudError):c.restore('a'*64,target)
        (self.root/'backup/file').write_text('keep')
        with self.assertRaises(cloud.CloudError):c.restore('a'*64,self.root/'backup')
        with self.assertRaises(cloud.CloudError):c.restore('latest',self.root/'primary')

    def test_lock_skips_health_while_backup_owns_it(self):
        c=self.instance()
        with c.lock():
            with c.lock(wait=False) as acquired:self.assertFalse(acquired)

    def test_restic_has_no_network_and_no_writable_source_mounts(self):
        c=self.instance();cmd=c.restic_command(['backup','/source/data'],[(self.root/'primary','/source/data',False)])
        self.assertEqual(cmd[cmd.index('--network')+1],'none')
        self.assertTrue(any('dst=/source/data,readonly' in x for x in cmd))
        self.assertNotIn('--privileged',cmd)
        self.assertNotIn('RESTIC_PASSWORD=', ' '.join(cmd))

    def test_tailscale_preserves_both_album_addresses(self):
        before={'TCP':{'443':{'HTTPS':True}},'Web':{'basement.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8080'}}},'our-album.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8080'}}}}}
        after=copy.deepcopy(before);after['TCP']['8443']={'HTTPS':True}
        self.assertTrue(cloud.preserved_routes(before,after,'8443'))
        del after['Web']['our-album.ts.net:443']
        self.assertFalse(cloud.preserved_routes(before,after,'8443'))

    def test_expose_refuses_preexisting_public_funnel(self):
        c=self.instance()
        before={'AllowFunnel':{self.config['hostname']+':8443':True}}
        with patch.object(cloud,'run',return_value=json.dumps(before)) as run:
            with self.assertRaises(cloud.CloudError):c.expose()
            self.assertEqual(run.call_count,1)

    def test_every_image_is_digest_pinned(self):
        for image in json.loads((cloud.ROOT/'images.json').read_text()).values():
            self.assertRegex(image,r'@sha256:[a-f0-9]{64}$')

    def test_initial_password_rejected_before_directories_are_created(self):
        found={x:self.mount_info(x) for x in ('primary','backup')}
        with patch.object(cloud,'check_drives',return_value=found),patch.object(cloud.socket,'socket'),patch.object(Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(cloud.CloudError,'leading dash'):
                cloud.initialize(self.config,self.state,password='-synthetic-password')
            mkdir.assert_not_called()

    def test_runtime_templates_are_readable_and_album_files_untouched(self):
        found={x:self.mount_info(x) for x in ('primary','backup')}
        for role,info in found.items():
            info['dev']=info['path'].stat().st_dev
            archive=info['path']/'album-archive';archive.mkdir();(archive/'original').write_bytes(b'existing album bytes')
        with patch.object(cloud,'check_drives',return_value=found),patch.object(cloud.socket,'socket'),patch.object(cloud.os,'chown'),patch.object(cloud.os,'fchown'):
            cloud.initialize(self.config,self.state,password='synthetic-owner-password')
        self.assertEqual((self.state/'runtime/deploy/redis.conf').stat().st_mode&0o777,0o644)
        self.assertEqual((self.state/'runtime/deploy/pre-install.sh').stat().st_mode&0o777,0o755)
        self.assertEqual(list((self.state/'nextcloud/config').iterdir()),[])
        self.assertFalse((self.state/'secrets/db_password').read_text().startswith('-'))
        for role in ('primary','backup'):
            self.assertEqual((Path(self.config[role]['mount'])/'album-archive/original').read_bytes(),b'existing album bytes')

    def test_backup_only_check_tolerates_a_missing_primary_mount(self):
        self.config['primary']['mount']=str(self.root/'lost-primary')
        def inspect(path,identifier,writable=True):
            self.assertNotEqual(str(path),self.config['primary']['mount'])
            return {'physical':'os' if str(path)=='/' else 'backup','free':2000*cloud.GIB}
        with patch.object(cloud,'inspect_mount',side_effect=inspect):
            self.assertEqual(set(cloud.check_drives(self.config,primary=False)),{'backup'})

    def test_default_cli_check_never_initializes(self):
        with patch('sys.argv',['cloud.py','--state',str(self.state),'check']),patch.object(cloud,'check_drives',return_value={}),patch.object(cloud,'initialize') as init:
            self.assertEqual(cloud.main(),0);init.assert_not_called()


if __name__=='__main__':unittest.main()

class RecoveryTests(unittest.TestCase):
    setUp = SafetyTests.setUp
    instance = SafetyTests.instance
    def test_reads_do_not_require_lost_primary_disk(self):
        c=self.instance();target=self.root/'recovery';target.mkdir()
        process=Mock();process.poll.return_value=0;process.returncode=0
        with patch.object(cloud,'check_drives',return_value={}) as check,patch.object(c,'cleanup_restic'),patch.object(cloud.subprocess,'Popen',return_value=process):
            c.restore('a'*64,target)
        self.assertFalse(check.call_args.kwargs['primary'])

    def test_interrupted_backup_recovers_without_discarding_old_snapshot(self):
        c=self.instance();c.record(backup_running=True,last_backup={'snapshot':'previous'})
        cloud.atomic_json(self.state/'backup-running.json',{'changed_maintenance':True})
        with patch.object(cloud,'check_drives'),patch.object(cloud.shutil,'disk_usage',return_value=Mock(free=20*cloud.GIB)),patch.object(c,'compose'),patch.object(c,'cleanup_restic') as cleanup,patch.object(c,'occ') as occ:
            c.start()
        cleanup.assert_called_once()
        self.assertIn(unittest.mock.call('maintenance:mode','--off'),occ.call_args_list)
        state=json.loads((self.state/'status.json').read_text())
        self.assertFalse(state['backup_running']);self.assertEqual(state['last_backup']['snapshot'],'previous')
        self.assertFalse((self.state/'backup-running.json').exists())
