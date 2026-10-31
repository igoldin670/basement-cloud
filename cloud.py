#!/usr/bin/env python3
"""Host operations for basement-cloud. Never formats, partitions, or mounts disks."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path('/var/lib/basement-cloud')
GIB = 1024**3
APP_UID = 82  # Official Nextcloud Alpine www-data, not the album's UID.
NGINX_UID = 101
SELECTORS = ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')


class CloudError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def run(command, **kwargs):
    result = subprocess.run([str(x) for x in command], text=True, capture_output=True, **kwargs)
    if result.returncode:
        # No passwords are ever passed as command arguments.
        raise CloudError(f'{command[0]} failed ({result.returncode}): {result.stderr.strip()[-1800:]}')
    return result.stdout


def write_new(path, content, mode=0o600, gid=None):
    """No overwrite, symlink following, or permissive intermediate file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        if gid is not None:
            os.fchown(fd, 0, gid)
        with os.fdopen(fd, 'w', closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8))
    try:
        write_new(tmp, json.dumps(value, indent=2) + '\n')
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def validate_config(config):
    for role in ('primary', 'backup'):
        entry = config[role]
        uuid.UUID(entry['uuid'])
        path = Path(entry['mount'])
        if not path.is_absolute() or path == Path('/') or path.resolve(strict=False) != path:
            raise CloudError(f'{role}: mount must be an existing absolute path without symlinks')
    a, b = (Path(config[role]['mount']) for role in ('primary', 'backup'))
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        raise CloudError('Live and backup paths cannot overlap')
    uuid.UUID(config['system_uuid'])
    if len({config['system_uuid'], config['primary']['uuid'], config['backup']['uuid']}) != 3:
        raise CloudError('OS, live, and backup filesystem UUIDs must differ')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*\.ts\.net', config['hostname']):
        raise CloudError('Use the existing Tailscale DNS hostname, without a URL or port')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', config['username']):
        raise CloudError('Invalid owner account name')
    for key in ('http_port', 'https_port'):
        if type(config[key]) is not int or not 1024 <= config[key] <= 65535:
            raise CloudError('Use a separate, unprivileged port for the new service')
    if config['http_port'] == 8080 or config['https_port'] == 443:
        raise CloudError('The existing album ports cannot be used')
    for key in ('quota_gib', 'reserve_gib'):
        if type(config[key]) is not int or config[key] < (10 if key == 'reserve_gib' else 1):
            raise CloudError('Invalid quota or free-space reserve')


def inspect_mount(path, expected_uuid, writable=True):
    path = Path(path)
    if path.resolve(strict=True) != path:
        raise CloudError(f'Refusing a symlinked mount: {path}')
    result = json.loads(run(['findmnt', '--json', '--mountpoint', path,
                             '--output', 'TARGET,FSTYPE,UUID,MAJ:MIN,OPTIONS']))
    rows = result.get('filesystems', [])
    if len(rows) != 1:
        raise CloudError(f'Expected one actual mount at {path}')
    row = rows[0]
    if row.get('target') != str(path) or row.get('uuid') != expected_uuid or row.get('fstype') != 'ext4':
        raise CloudError(f'Wrong UUID, filesystem, or missing mount at {path}; no storage will be created there')
    if writable and 'rw' not in row.get('options', '').split(','):
        raise CloudError(f'Read-only filesystem: {path}')
    dev = path.stat().st_dev
    if row.get('maj:min') != f'{os.major(dev)}:{os.minor(dev)}':
        raise CloudError('Mount device changed during inspection')
    block = (Path('/sys/dev/block') / row['maj:min']).resolve(strict=True)
    if (block / 'partition').exists():
        block = block.parent
    if not (block / 'device').exists() or list((block / 'slaves').iterdir()):
        raise CloudError('Physical disk identity is unavailable or is a multi-device mapping')
    physical = str(block)
    for relative in ('device/wwid', 'wwid', 'device/serial'):
        identifier = block / relative
        if identifier.exists() and identifier.read_text().strip():
            physical = identifier.read_text().strip()
            break
    return {'path': path, 'dev': dev, 'physical': physical, 'free': shutil.disk_usage(path).free}


def check_drives(config, backup=True, free=False, primary=True):
    validate_config(config)
    system = inspect_mount('/', config['system_uuid'], writable=False)
    found = {}
    if primary:
        found['primary'] = inspect_mount(config['primary']['mount'], config['primary']['uuid'])
    if backup:
        found['backup'] = inspect_mount(config['backup']['mount'], config['backup']['uuid'])
    disks = [system['physical']] + [x['physical'] for x in found.values()]
    if len(set(disks)) != len(disks):
        raise CloudError('OS, live data, and backups must use separate physical drives')
    if free and any(x['free'] < config['reserve_gib'] * GIB for x in found.values()):
        raise CloudError('Shared-drive free-space reserve has been reached')
    return found


@contextmanager
def pinned_mount(info):
    fd = os.open(info['path'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_dev != info['dev']:
            raise CloudError('Mount changed before directory creation')
        yield fd
    finally:
        os.close(fd)


def make_at(parent_fd, name, uid=0):
    """Directory operations stay on the verified inode even if the mount detaches."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    os.fchown(fd, uid, uid)
    os.fchmod(fd, 0o750 if uid else 0o700)
    return fd


class Cloud:
    def __init__(self, state=STATE, project='basement-cloud'):
        self.state = Path(state)
        self.project = project
        self.config = json.loads((self.state / 'config.json').read_text())
        self.images = json.loads((self.state / 'runtime/images.json').read_text())
        self.primary = Path(self.config['primary']['mount']) / 'basement-cloud'
        self.repository = Path(self.config['backup']['mount']) / 'basement-cloud-backups' / 'restic'
        self.env = os.environ.copy()
        for selector in SELECTORS:
            self.env.pop(selector, None)

    @property
    def docker(self):
        return ['docker', '--host=unix:///var/run/docker.sock']

    def compose(self, *args, **kwargs):
        return run([*self.docker, 'compose', '--project-name', self.project,
                    '--env-file', self.state / 'compose.env', '-f', self.state / 'runtime/deploy/compose.yaml', *args], env=self.env, **kwargs)

    def occ(self, *args):
        return self.compose('exec', '-T', '--user', str(APP_UID), 'app', 'php', 'occ', *args)

    @contextmanager
    def lock(self, wait=True):
        with (self.state / 'operation.lock').open('a') as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def record(self, **patch):
        path = self.state / 'status.json'
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update(patch)
        atomic_json(path, data)

    def running(self):
        return set(self.compose('ps', '--services', '--status', 'running').split())

    def start(self):
        self.record(desired_running=True)
        check_drives(self.config, backup=False, free=True)
        if shutil.disk_usage(self.state).free < 5 * GIB:
            raise CloudError('OS disk has less than 5 GiB available for database/metadata')
        self.cleanup_restic()
        self.compose('up', '-d', '--wait', '--wait-timeout', '900')
        journal = self.state / 'backup-running.json'
        if journal.exists():
            previous = json.loads(journal.read_text())
            if previous.get('changed_maintenance'):
                self.occ('maintenance:mode', '--off')
            self.record(backup_running=False, last_failure={'at': now(), 'message': 'A backup was interrupted. The previous completed snapshot remains intact.'})
            journal.unlink()
        self.occ('user:setting', self.config['username'], 'files', 'quota', f"{self.config['quota_gib']} GB")
        self.occ('background:cron')
        self.record(last_start=now(), desired_running=True)

    def cleanup_restic(self):
        name = self.project + '-restic'
        probe = subprocess.run([*self.docker, 'inspect', '--format', '{{index .Config.Labels "io.basement-cloud.project"}}', name], env=self.env, text=True, capture_output=True)
        if probe.returncode:
            return
        if probe.stdout.strip() != self.project:
            raise CloudError('Refusing to stop a container not owned by this deployment')
        run([*self.docker, 'rm', '--force', name], env=self.env)

    def notify(self, message):
        try:
            self.occ('notification:generate', self.config['username'], message)
        except CloudError:
            pass  # Journal/status remain available when Nextcloud itself is offline.

    def restic_command(self, arguments, extra_mounts=()):
        command = [*self.docker, 'run', '--rm', '--name', self.project + '-restic', '--label', 'io.basement-cloud.project=' + self.project, '--network', 'none', '--read-only',
                   '--cap-drop=ALL', '--cap-add=DAC_OVERRIDE', '--security-opt=no-new-privileges:true',
                   '--memory=2g', '--cpus=1', '--pids-limit=64', '--tmpfs', '/tmp:size=256m,mode=1777',
                   '--mount', f'type=bind,src={self.repository},dst=/repository',
                   '--mount', f'type=bind,src={self.state}/secrets/restic_password,dst=/run/secrets/password,readonly',
                   '-e', 'RESTIC_REPOSITORY=/repository', '-e', 'RESTIC_PASSWORD_FILE=/run/secrets/password',
                   '-e', 'GOMEMLIMIT=1400MiB']
        if arguments and arguments[0] == 'restore':
            command += ['--cap-add=CHOWN', '--cap-add=FOWNER']
        for source, destination, writable in extra_mounts:
            command += ['--mount', f'type=bind,src={source},dst={destination}' + ('' if writable else ',readonly')]
        return [*command, self.images['RESTIC_IMAGE'], '--no-cache', *arguments]

    def restic(self, arguments, mounts=(), guard_space=False):
        needs_primary = bool(arguments and arguments[0] == 'backup')
        check_drives(self.config, primary=needs_primary)
        self.cleanup_restic()
        log = self.state / 'restic.log'
        with log.open('ab') as output:
            process = subprocess.Popen(self.restic_command(arguments, mounts), env=self.env, stdout=output, stderr=output)
            try:
                while process.poll() is None:
                    try:
                        found = check_drives(self.config, primary=needs_primary)
                        if guard_space and found['backup']['free'] < self.config['reserve_gib'] * GIB:
                            raise CloudError('Backup-drive free-space reserve reached; old snapshots retained')
                    except Exception:
                        process.terminate()
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait()
                        raise
                    time.sleep(2)
                if process.returncode:
                    raise CloudError(f'Restic failed ({process.returncode}); inspect root-only {log}')
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=30)

    def backup(self):
        check_drives(self.config, free=True)
        if not (self.repository / 'config').is_file():
            if any(self.repository.iterdir()):
                raise CloudError('Refusing to initialize a nonempty, unrecognized backup repository')
            self.restic(['init'], guard_space=True)
        running = self.running()
        if 'app' not in running:
            raise CloudError('Start Nextcloud before taking a new consistent backup')
        info = json.loads(self.occ('status', '--output=json'))
        if not info.get('installed'):
            raise CloudError('Nextcloud installation is not complete')
        changed = not info.get('maintenance', False)
        journal = self.state / 'backup-running.json'
        if journal.exists():
            raise CloudError('An interrupted backup needs recovery: run start first')
        atomic_json(journal, {'started': now(), 'changed_maintenance': changed, 'running': sorted(running)})
        staging = self.state / 'backup-staging'
        staging.mkdir(mode=0o700, exist_ok=True)
        self.record(backup_running=True, backup_started=now())
        try:
            # Stop all writers before dumping. Partial uploads remain for client retries.
            self.compose('stop', '--timeout', '120', 'cron')
            if changed:
                self.occ('maintenance:mode', '--on')
            self.compose('stop', '--timeout', '120', 'web', 'app')
            size = int(self.compose('exec', '-T', 'db', 'psql', '-At', '-U', 'cloud', '-d', 'cloud', '-c', 'SELECT pg_database_size(current_database())').strip())
            if shutil.disk_usage(staging).free < size * 2 + 5 * GIB:
                raise CloudError('Insufficient OS space for a safe database dump')
            exports = [
                ('roles.sql', ['pg_dumpall', '-U', 'cloud', '--roles-only']),
                ('database.dump', ['pg_dump', '-U', 'cloud', '-d', 'cloud', '-Fc']),
            ]
            for filename, command in exports:
                dump = staging / (filename + '.part')
                with dump.open('wb') as output:
                    subprocess.run([*self.docker, 'compose', '--project-name', self.project,
                        '--env-file', str(self.state / 'compose.env'), '-f', str(self.state / 'runtime/deploy/compose.yaml'),
                        'exec', '-T', 'db', *command], env=self.env, stdout=output, check=True)
                    output.flush(); os.fsync(output.fileno())
                os.replace(dump, staging / filename)
            atomic_json(staging / 'config.json', self.config)
            atomic_json(staging / 'images.json', self.images)
            self.restic(['backup', '--host', 'basement-cloud', '--tag', 'basement-cloud',
                         '/source/nextcloud', '/source/data', '/source/database', '/source/secrets'], [
                (self.state / 'nextcloud', '/source/nextcloud', False),
                (self.primary / 'data', '/source/data', False),
                (staging, '/source/database', False),
                (self.state / 'secrets', '/source/secrets', False)], guard_space=True)
        finally:
            # Never leave an otherwise healthy service in maintenance after a failed backup.
            self.compose('start', 'app')
            if changed:
                self.occ('maintenance:mode', '--off')
            resume = sorted(running & {'app', 'cron', 'web'})
            self.compose('up', '-d', '--wait', '--wait-timeout', '300', *resume)
            journal.unlink(missing_ok=True)
            self.record(backup_running=False)
        self.restic(['check'])
        snapshots = json.loads(run(self.restic_command(['snapshots', '--json', '--tag', 'basement-cloud']), env=self.env))
        snapshot = max(snapshots, key=lambda x: x['time'])
        self.record(last_backup={'at': now(), 'snapshot': snapshot['id'], 'snapshot_time': snapshot['time']}, last_failure=None)
        # Retention only runs after a completed, checked snapshot. Never prune to make a failing backup fit.
        self.restic(['forget', '--tag', 'basement-cloud', '--group-by', 'host,tags', '--keep-last', '2',
                     '--keep-daily', '7', '--keep-weekly', '4', '--keep-monthly', '3', '--prune'])

    def verify(self):
        self.restic(['check', '--read-data'])
        self.record(last_full_integrity=now())

    def health(self):
        status = self.state / 'status.json'
        if status.exists() and json.loads(status.read_text()).get('desired_running') is False:
            return
        try:
            check_drives(self.config, backup=False, free=True)
        except Exception as exc:
            self.compose('stop', '--timeout', '120', 'web', 'cron', 'app')
            self.record(last_failure={'at': now(), 'message': str(exc)})
            raise
        running = self.running()
        if not {'app', 'web', 'cron', 'db', 'redis'} <= running:
            self.start()
        try:
            check_drives(self.config, free=True)
        except Exception as exc:
            self.record(last_failure={'at': now(), 'message': str(exc)})

    def expose(self):
        before = json.loads(run(['tailscale', 'serve', 'status', '--json']))
        port = str(self.config['https_port'])
        if any(value for key, value in before.get('AllowFunnel', {}).items() if key.endswith(':' + port)):
            raise CloudError('The HTTPS port is already configured for public Funnel; no route was changed')
        if port in before.get('TCP', {}):
            expected = f"http://127.0.0.1:{self.config['http_port']}"
            endpoints = [value for host, value in before.get('Web', {}).items() if host.endswith(':' + port)]
            if not endpoints or any(value.get('Handlers', {}).get('/', {}).get('Proxy') != expected for value in endpoints):
                raise CloudError('The requested HTTPS port already belongs to another service')
        atomic_json(self.state / 'tailscale-before.json', before)
        run(['tailscale', 'serve', '--bg', f'--https={port}', f"http://127.0.0.1:{self.config['http_port']}"])
        after = json.loads(run(['tailscale', 'serve', 'status', '--json']))
        if not preserved_routes(before, after, port):
            raise CloudError('Existing Tailscale routes changed. Inspect tailscale-before.json; no reset was attempted.')
        if any(value for key, value in after.get('AllowFunnel', {}).items() if key.endswith(':' + port)):
            raise CloudError('The new listener must be tailnet-only, not Funnel')
        print(f"Open https://{self.config['hostname']}:{port}")

    def restore(self, snapshot, target):
        if not re.fullmatch(r'[0-9a-f]{8,64}', snapshot):
            raise CloudError('Specify an explicit snapshot ID from status/snapshots')
        target = Path(target)
        if not target.is_absolute() or not target.is_dir() or target.resolve() != target or any(target.iterdir()):
            raise CloudError('Restore target must be an existing, empty absolute directory without symlinks')
        protected = [self.state, Path(self.config['primary']['mount']), Path(self.config['backup']['mount'])]
        if target == Path('/') or any(target == x or target.is_relative_to(x) or x.is_relative_to(target) for x in protected):
            raise CloudError('Restore into a separate recovery location, never live storage or its parent')
        self.restic(['restore', snapshot, '--target', '/restore'], [(target, '/restore', True)])
        print('Files restored into the empty recovery directory. Follow README before restoring the database.')

    def status(self):
        result = {'url': f"https://{self.config['hostname']}:{self.config['https_port']}"}
        for role in ('primary', 'backup'):
            try:
                info = inspect_mount(self.config[role]['mount'], self.config[role]['uuid'])
                result[role] = {'online': True, 'free_gib': round(info['free'] / GIB, 1)}
            except Exception as exc:
                result[role] = {'online': False, 'error': str(exc)}
        path = self.state / 'status.json'
        result.update(json.loads(path.read_text()) if path.exists() else {})
        print(json.dumps(result, indent=2))


def preserved_routes(before, after, new_port):
    for section in ('Web', 'TCP', 'Services', 'AllowFunnel'):
        for key, value in before.get(section, {}).items():
            if str(key) == new_port or str(key).endswith(':' + new_port):
                continue
            if after.get(section, {}).get(key) != value:
                return False
    return True


def initialize(config, state=STATE, root=ROOT, password=None):
    found = check_drives(config, free=True)
    budget = min(x['free'] for x in found.values()) - config['reserve_gib'] * GIB
    if config['quota_gib'] * GIB > budget * .70:
        raise CloudError('Initial quota would leave insufficient room for album growth and backup history; choose a lower quota')
    state = Path(state)
    if state.exists() and any(state.iterdir()):
        raise CloudError('State directory is nonempty; existing data/secrets will not be overwritten')
    for role, name in [('primary', 'basement-cloud'), ('backup', 'basement-cloud-backups')]:
        if (found[role]['path'] / name).exists() or (found[role]['path'] / name).is_symlink():
            raise CloudError(f'Dedicated {name} directory already exists; refusing to overwrite it')
    if state.exists() and (state.is_symlink() or state.stat().st_uid != 0):
        raise CloudError('State directory must be root-owned and cannot be a symlink')
    if (state / 'config.json').exists():
        raise CloudError('Already initialized; existing accounts, secrets, and data will not be overwritten')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', config['http_port']))
    except OSError as exc:
        raise CloudError('The loopback HTTP port is already in use; no new directories were created') from exc
    password = getpass.getpass('Choose a Nextcloud owner password (at least 12 characters): ') if password is None else password
    if len(password) < 12 or password.startswith('-') or '\n' in password or '\x00' in password:
        raise CloudError('Use at least 12 characters, no line breaks, and no leading dash (upstream installer requirement)')
    if password is not None and sys.stdin.isatty():
        if getpass.getpass('Confirm the owner password: ') != password:
            raise CloudError('Passwords did not match')
    state.mkdir(mode=0o700, parents=True, exist_ok=True); state.chmod(0o700)
    for name in ('nextcloud', 'postgres', 'secrets', 'backup-staging'):
        (state / name).mkdir(mode=0o700, exist_ok=True)
    # Reject preexisting unrelated namespaces before touching their contents/ownership.
    for role, name in [('primary', 'basement-cloud'), ('backup', 'basement-cloud-backups')]:
        with pinned_mount(found[role]) as mount_fd:
            try:
                os.stat(name, dir_fd=mount_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise CloudError(f'Dedicated {name} directory already exists; inspect partial setup without overwriting it')
            fd = make_at(mount_fd, name)
            try:
                if role == 'primary':
                    children = [('data', APP_UID), ('tmp', 0)]
                else:
                    children = [('restic', 0)]
                for child, uid in children:
                    child_fd = make_at(fd, child, uid)
                    if child == 'tmp':
                        for sub, sub_uid in [('php', APP_UID), ('nginx', NGINX_UID)]:
                            sub_fd = make_at(child_fd, sub, sub_uid); os.close(sub_fd)
                    os.close(child_fd)
            finally:
                os.close(fd)
    (state / 'nextcloud').chmod(0o755); os.chown(state / 'nextcloud', APP_UID, APP_UID)
    (state / 'nextcloud/config').mkdir(mode=0o750, exist_ok=True)
    os.chown(state / 'nextcloud/config', APP_UID, APP_UID)
    shutil.copytree(root / 'deploy', state / 'runtime/deploy', dirs_exist_ok=True)
    shutil.copy2(root / 'images.json', state / 'runtime/images.json')
    shutil.copy2(root / 'cloud.py', state / 'runtime/cloud.py')
    for template in (state / 'runtime/deploy').iterdir():
        if template.is_file():
            template.chmod(0o755 if template.suffix == '.sh' else 0o644)
    for name, content in [('db_password', 'db_' + secrets.token_urlsafe(40)), ('redis_password', secrets.token_hex(32)),
                          ('admin_password', password), ('restic_password', secrets.token_urlsafe(48))]:
        write_new(state / 'secrets' / name, content + '\n', 0o440, APP_UID)
    redis_password = (state / 'secrets/redis_password').read_text().strip()
    write_new(state / 'secrets/redis_auth', f'requirepass {redis_password}\n', 0o444)
    images = json.loads((root / 'images.json').read_text())
    values = dict(images, CLOUD_STATE=str(state), CLOUD_PRIMARY=str(Path(config['primary']['mount']) / 'basement-cloud'),
                  CLOUD_HOST=f"{config['hostname']}:{config['https_port']}", CLOUD_HTTP_PORT=str(config['http_port']), CLOUD_USER=config['username'])
    if any('\n' in str(value) or '$' in str(value) or ' ' in str(value) for value in values.values()):
        raise CloudError('Deployment paths and settings cannot contain spaces, newlines, or variable substitutions')
    write_new(state / 'compose.env', ''.join(f'{key}={value}\n' for key, value in values.items()))
    atomic_json(state / 'config.json', config)
    print(f'Initialized private storage. Initial owner quota: {config["quota_gib"]} GiB. No album files were changed.')
    print(f'Save the Restic recovery key from {state}/secrets/restic_password in your password manager, separate from the server.')


def enable(cloud):
    if cloud.state != STATE:
        raise CloudError('Install system services only for the standard state location')
    base = f'/usr/bin/python3 {STATE}/runtime/cloud.py'
    units = {
        'basement-cloud.service': f'''[Unit]\nDescription=Private Nextcloud (UUID-guarded)\nRequires=docker.service\nAfter=docker.service tailscaled.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={base} start\nExecStop={base} stop\nTimeoutStartSec=1200\nTimeoutStopSec=360\n[Install]\nWantedBy=multi-user.target\n''',
    }
    for name, command, schedule in [('backup','backup','*-*-* 03:00:00 America/New_York'),
                                    ('verify','verify','Sun *-*-* 05:00:00 America/New_York'),
                                    ('health','health',None)]:
        units[f'basement-cloud-{name}.service'] = f'[Unit]\nDescription=Basement cloud {name}\nAfter=basement-cloud.service\n[Service]\nType=oneshot\nExecStart={base} {command}\nNice=10\nIOSchedulingClass=idle\nTimeoutStartSec=24h\n'
        timer = f'OnCalendar={schedule}\nPersistent=true' if schedule else 'OnBootSec=90s\nOnUnitActiveSec=60s'
        units[f'basement-cloud-{name}.timer'] = f'[Unit]\nDescription=Basement cloud {name} schedule\n[Timer]\n{timer}\n[Install]\nWantedBy=timers.target\n'
    for name, content in units.items():
        path = Path('/etc/systemd/system') / name
        if path.exists() and path.read_text() != content:
            raise CloudError(f'Existing different unit will not be overwritten: {name}')
        if not path.exists():
            write_new(path, content, 0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'basement-cloud.service', 'basement-cloud-backup.timer',
         'basement-cloud-verify.timer', 'basement-cloud-health.timer'])
    print('Guarded startup and scheduled backups enabled. Album services were not changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=STATE)
    parser.add_argument('--config', type=Path, default=ROOT / 'server.example.json')
    parser.add_argument('command', choices=['check','init','start','stop','expose','enable','backup','verify','status','snapshots','restore','health'])
    parser.add_argument('--quota-gib', type=int)
    parser.add_argument('--snapshot')
    parser.add_argument('--target', type=Path)
    args = parser.parse_args()
    cloud = None
    try:
        if args.command != 'check' and args.state != STATE:
            raise CloudError('Host operations use the standard state directory to prevent Compose project collisions')
        if args.command in ('check','init'):
            config = json.loads((args.state / 'config.json').read_text()) if (args.state / 'config.json').exists() else json.loads(args.config.read_text())
            if args.quota_gib is not None:
                config['quota_gib'] = args.quota_gib
            if args.command == 'check':
                found = check_drives(config, free=True)
                print(json.dumps({role:{'mount':str(x['path']),'free_gib':round(x['free']/GIB,1)} for role,x in found.items()},indent=2))
                print(f'New private URL: https://{config["hostname"]}:{config["https_port"]}; planned quota: {config["quota_gib"]} GiB')
                return 0
            if os.geteuid() != 0:
                raise CloudError('Run init with sudo on the Linux server')
            initialize(config, args.state)
            return 0
        if os.geteuid() != 0:
            raise CloudError('Run host operations with sudo on the Linux server')
        cloud = Cloud(args.state)
        if args.command == 'status':
            cloud.status(); return 0
        if args.command == 'enable':
            enable(cloud); return 0
        with cloud.lock(wait=args.command != 'health') as acquired:
            if not acquired:
                return 0
            if args.command == 'restore':
                if not args.snapshot or args.target is None:
                    raise CloudError('restore requires --snapshot ID --target EMPTY_DIRECTORY')
                cloud.restore(args.snapshot,args.target)
            elif args.command == 'snapshots':
                check_drives(cloud.config, primary=False)
                print(run(cloud.restic_command(['snapshots']), env=cloud.env))
            elif args.command == 'stop':
                cloud.record(desired_running=False)
                cloud.compose('stop','--timeout','120')
            else:
                getattr(cloud,args.command)()
        return 0
    except (CloudError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        if cloud and args.command in ('backup','verify'):
            cloud.record(last_failure={'at':now(),'message':str(exc)})
            cloud.notify('Storage backup needs attention. Check the server status before deleting phone originals.')
        print(f'ERROR: {exc}',file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
