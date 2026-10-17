"""Plain-Docker updater. Inspect configuration, never read the API-key file."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/cobeng13/PacheckEssay.git'
IDLE_CHECK = (
    "import sqlite3; db=sqlite3.connect('file:/data/ledger.sqlite3?mode=ro',uri=True); "
    "print(db.execute(\"SELECT count(*) FROM jobs WHERE state IN ('queued','running')\").fetchone()[0])"
)
READY_CHECK = (
    "import sqlite3,sys,json,urllib.request; "
    "db=sqlite3.connect('file:/data/ledger.sqlite3?mode=ro',uri=True); "
    "row=db.execute('SELECT heartbeat FROM worker WHERE id=1').fetchone(); "
    "assert row and row[0]>=float(sys.argv[1]); "
    "assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3))['status']=='ready'"
)


class UpdateError(RuntimeError):
    pass


class Host:
    def __init__(self):
        self.docker_prefix = ['docker']

    def command(self, args, check=True, timeout=None):
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            # Do not print inspect JSON, environment values, or secret-bearing errors.
            raise UpdateError(f'{args[0]} {args[1]} failed. Check Docker access, Git access, and host configuration.')
        return result

    def docker(self, *args, check=True, timeout=None):
        return self.command(self.docker_prefix + list(args), check, timeout)

    def connect_docker(self):
        if self.docker('info', check=False).returncode == 0:
            return
        if os.geteuid() == 0:
            raise UpdateError('Docker daemon is unavailable.')
        print('Docker requires elevated access; authenticating sudo...', flush=True)
        if subprocess.run(['sudo', '-v']).returncode:
            raise UpdateError('Could not authenticate sudo for Docker.')
        self.docker_prefix = ['sudo', '-n', 'docker']
        self.docker('info')


def inspect(host, name):
    result = host.docker('container', 'inspect', name)
    data = json.loads(result.stdout)[0]
    if not data['State']['Running']:
        raise UpdateError(f'{name} must be running before an update.')
    return data


def environment(container):
    values = container['Config'].get('Env') or []
    if any('\n' in value or '\r' in value for value in values):
        raise UpdateError('Multiline environment values need a manual deployment; refusing to change them.')
    return dict(value.split('=', 1) for value in values)


def data_mount(container):
    matches = [m for m in container.get('Mounts', []) if m['Destination'] == '/data']
    if len(matches) != 1 or not matches[0]['RW']:
        raise UpdateError('Each container must have a writable persistent mount at /data.')
    mount = matches[0]
    if mount['Type'] not in ('volume', 'bind'):
        raise UpdateError('The /data mount must be a Docker volume or host directory.')
    return mount['Type'], mount.get('Name') or mount['Source']


def preflight_pair(web, worker):
    if web['Id'] == worker['Id'] or data_mount(web) != data_mount(worker):
        raise UpdateError('Web and worker must be separate containers sharing the same /data mount.')
    for container in (web, worker):
        env = environment(container)
        if env.get('ESSAY_DATA', '/data') != '/data':
            raise UpdateError('This updater requires ESSAY_DATA=/data.')
        host = container['HostConfig']
        # Unsupported options must never be silently dropped during recreation.
        if (host.get('Privileged') or host.get('AutoRemove') or host.get('Devices')
                or host.get('DeviceRequests') or host.get('VolumesFrom') or host.get('Links')
                or host.get('PidMode') or host.get('IpcMode') not in (None, '', 'private')
                or host.get('NetworkMode') not in ('default', 'bridge')):
            raise UpdateError('Custom devices, namespaces, links or networks require a manual update.')
        if host.get('Dns') or host.get('ExtraHosts') or host.get('Ulimits'):
            raise UpdateError('Custom DNS, extra hosts or ulimits require a manual update.')
        for option in ('Memory', 'MemoryReservation', 'MemorySwap', 'NanoCpus', 'CpuShares', 'CpuPeriod', 'CpuQuota', 'PidsLimit'):
            if host.get(option) not in (None, 0):
                raise UpdateError('Custom resource limits require a manual update.')
        if host.get('CpusetCpus') or host.get('CpusetMems'):
            raise UpdateError('Custom CPU affinity requires a manual update.')
        if len(container['Config'].get('Entrypoint') or []) > 1:
            raise UpdateError('Custom multi-part entrypoints require a manual update.')
        for mount in container.get('Mounts', []):
            if mount['Type'] not in ('volume', 'bind', 'tmpfs'):
                raise UpdateError('Unsupported container mount type.')
            if any(',' in str(mount.get(k, '')) for k in ('Source', 'Destination', 'Name')):
                raise UpdateError('Mount paths containing commas require a manual update.')
    key = environment(worker).get('OPENAI_API_KEY_FILE', '/run/secrets/openai_api_key')
    key_mounts = [m for m in worker.get('Mounts', []) if m['Destination'] == key]
    if len(key_mounts) != 1 or key_mounts[0]['Type'] != 'bind' or key_mounts[0]['RW']:
        raise UpdateError('The worker must have a read-only bind mount for its API-key file.')
    if any(m['Source'] == key_mounts[0]['Source'] for m in web.get('Mounts', [])):
        raise UpdateError('The web container must not mount the API-key file.')
    if not web['HostConfig'].get('PortBindings'):
        raise UpdateError('The web container has no published port to preserve.')


def require_idle(host, worker):
    result = host.docker('exec', worker, 'python', '-c', IDLE_CHECK)
    if result.stdout.strip() != '0':
        raise UpdateError('Queued or running grading jobs exist. Finish or cancel them before updating.')


def creation_args(container, name, image, env_file):
    config, host = container['Config'], container['HostConfig']
    args = ['create', '--name', name, '--env-file', str(env_file)]
    policy = host.get('RestartPolicy') or {}
    restart = policy.get('Name') or 'no'
    if restart == 'on-failure' and policy.get('MaximumRetryCount'):
        restart += ':' + str(policy['MaximumRetryCount'])
    args += ['--restart', restart]
    if host.get('Init'):
        args.append('--init')
    if config.get('User'):
        args += ['--user', config['User']]
    if config.get('WorkingDir'):
        args += ['--workdir', config['WorkingDir']]
    if config.get('StopSignal'):
        args += ['--stop-signal', config['StopSignal']]
    if config.get('StopTimeout') is not None:
        args += ['--stop-timeout', str(config['StopTimeout'])]
    if host.get('ReadonlyRootfs'):
        args.append('--read-only')
    for flag, field in (('--cap-add', 'CapAdd'), ('--cap-drop', 'CapDrop'), ('--security-opt', 'SecurityOpt')):
        for value in host.get(field) or []:
            args += [flag, value]
    for destination, options in (host.get('Tmpfs') or {}).items():
        args += ['--tmpfs', f'{destination}:{options}']
    for mount in container.get('Mounts', []):
        if mount['Type'] == 'tmpfs':
            continue
        source = mount.get('Name') if mount['Type'] == 'volume' else mount['Source']
        value = f"type={mount['Type']},src={source},dst={mount['Destination']}"
        if not mount['RW']:
            value += ',readonly'
        if mount['Type'] == 'bind' and mount.get('Propagation'):
            value += ',bind-propagation=' + mount['Propagation']
        args += ['--mount', value]
    for port, bindings in (host.get('PortBindings') or {}).items():
        for binding in bindings or []:
            ip, published = binding.get('HostIp', ''), binding.get('HostPort', '')
            if not published:
                raise UpdateError('Dynamically allocated ports require a manual update.')
            if ':' in ip:
                ip = '[' + ip + ']'
            args += ['--publish', (ip + ':' if ip else '') + published + ':' + port]
    for key, value in (config.get('Labels') or {}).items():
        args += ['--label', key + '=' + value]
    log = host.get('LogConfig') or {}
    if log.get('Type'):
        args += ['--log-driver', log['Type']]
        for key, value in (log.get('Config') or {}).items():
            args += ['--log-opt', key + '=' + value]
    if config.get('Entrypoint'):
        args += ['--entrypoint', config['Entrypoint'][0]]
    return args + [image] + (config.get('Cmd') or [])


class Deployment:
    def __init__(self, host, web_name, worker_name, readiness_timeout=120, sleep=time.sleep):
        self.host, self.names = host, [web_name, worker_name]
        self.timeout, self.sleep = readiness_timeout, sleep

    def deploy(self, revision):
        host = self.host
        originals = [inspect(host, name) for name in self.names]
        preflight_pair(*originals)
        require_idle(host, self.names[1])
        tag = 'pacheckessay:update-' + revision[:12] + '-' + str(int(time.time()))
        print('Building the new image while existing containers remain running...', flush=True)
        host.docker('build', '--label', 'org.opencontainers.image.revision=' + revision, '-t', tag, '.')
        suffix = '-previous-' + str(os.getpid()) + '-' + str(int(time.time()))
        candidates, renamed, stopped = [], [], []
        # Private env files are temporary, outside Git; never print inspect data.
        with tempfile.TemporaryDirectory(prefix='pacheckessay-update-') as temporary:
            try:
                for index, (container, name) in enumerate(zip(originals, self.names)):
                    env_file = Path(temporary) / f'container-{index}.env'
                    env_file.touch(mode=0o600)
                    env_file.write_text('\n'.join(container['Config'].get('Env') or []) + '\n')
                    result = host.docker(*creation_args(container, name + '-next' + suffix, tag, env_file))
                    candidates.append(result.stdout.strip())
                require_idle(host, self.names[1])
                print('Switching containers; preserving published ports, data and key mounts...', flush=True)
                stopped.append(originals[0]['Id'])
                host.docker('stop', '--time', '30', originals[0]['Id'])
                # Web is now stopped: no new jobs can race the final idle check.
                require_idle(host, self.names[1])
                stopped.append(originals[1]['Id'])
                host.docker('stop', '--time', '150', originals[1]['Id'])
                for container, name in zip(originals, self.names):
                    host.docker('rename', container['Id'], name + suffix)
                    renamed.append((container['Id'], name))
                for candidate, name in zip(candidates, self.names):
                    host.docker('rename', candidate, name)
                started = time.time()
                host.docker('start', candidates[1])
                host.docker('start', candidates[0])
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    running = host.docker('inspect', '--format={{.State.Running}}', candidates[1], check=False)
                    ready = host.docker('exec', candidates[0], 'python', '-c', READY_CHECK, str(started), check=False, timeout=10)
                    if running.stdout.strip() == 'true' and ready.returncode == 0:
                        break
                    self.sleep(2)
                else:
                    raise UpdateError('Readiness failed: the new worker heartbeat and web health did not pass.')
            except BaseException:
                print('Update failed; restoring the previous containers...', file=sys.stderr, flush=True)
                rollback_ok = True
                for candidate in candidates:
                    host.docker('stop', '--time', '150', candidate, check=False)
                    if host.docker('rm', candidate, check=False).returncode:
                        rollback_ok = False
                for ident, name in reversed(renamed):
                    if host.docker('rename', ident, name, check=False).returncode:
                        rollback_ok = False
                # Start the old worker before the old web process.
                for ident in reversed(stopped):
                    if host.docker('start', ident, check=False).returncode:
                        rollback_ok = False
                if not rollback_ok:
                    raise UpdateError('Automatic restoration was incomplete. Previous containers remain available; administrator action is required.') from None
                raise
        # No volume deletion, image pruning, database copying or key-file access.
        for container in originals:
            if host.docker('rm', container['Id'], check=False).returncode:
                print('Note: an old stopped container could not be removed; no volume was deleted.')
        print(f'Updated successfully to {revision}. Readiness passed. Existing ports and storage preserved.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-container', default=os.getenv('ESSAY_WEB_CONTAINER', 'pacheckessay-web'))
    parser.add_argument('--worker-container', default=os.getenv('ESSAY_WORKER_CONTAINER', 'pacheckessay-worker'))
    parser.add_argument('--readiness-timeout', type=int, default=120)
    parser.add_argument('--deploy-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise UpdateError('Run the updater on the Linux Docker host.')
    if args.readiness_timeout < 1 or args.web_container == args.worker_container:
        raise UpdateError('Use distinct container names and a positive readiness timeout.')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) for name in (args.web_container, args.worker_container)):
        raise UpdateError('Use Docker container names, without slashes or shell options.')
    import fcntl
    lock_path = Path(tempfile.gettempdir()) / ('pacheckessay-update-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16] + '.lock')
    lock = lock_path.open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise UpdateError('Another updater is already running for this checkout.') from None
    host = Host()
    branch = host.command(['git', 'symbolic-ref', '--quiet', '--short', 'HEAD']).stdout.strip()
    if branch != 'main':
        raise UpdateError('Check out main before updating.')
    if host.command(['git', 'status', '--porcelain']).stdout.strip():
        raise UpdateError('The checkout has local changes. Commit or move them before updating; nothing will be overwritten.')
    host.connect_docker()
    preflight_pair(inspect(host, args.web_container), inspect(host, args.worker_container))
    require_idle(host, args.worker_container)
    if not args.deploy_only:
        print('Pulling main from ' + REPOSITORY + '...', flush=True)
        host.command(['git', 'pull', '--ff-only', REPOSITORY, 'main'])
        # Run freshly pulled deployment logic, as the scheduler updater does.
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], '--deploy-only'])
    revision = host.command(['git', 'rev-parse', 'HEAD']).stdout.strip()
    Deployment(host, args.web_container, args.worker_container, args.readiness_timeout).deploy(revision)


if __name__ == '__main__':
    try:
        main()
    except (UpdateError, OSError, subprocess.TimeoutExpired) as error:
        print('Update stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
