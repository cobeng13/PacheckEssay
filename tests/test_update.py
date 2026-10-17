"""Exercise update failures and rollback with fake Docker; no daemon or API key."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from deploy.update import Deployment, UpdateError, creation_args, preflight_pair


def containers():
    web = {
        'Id': 'old-web', 'State': {'Running': True},
        'Config': {'Env': ['ESSAY_DATA=/data', 'SECURE_COOKIE=false'], 'User': 'appuser', 'WorkingDir': '/app',
                   'Cmd': ['uvicorn', 'essay_web.app:create_app', '--factory', '--host', '0.0.0.0', '--port', '8000']},
        'HostConfig': {'NetworkMode': 'default', 'RestartPolicy': {'Name': 'unless-stopped'}, 'Init': True,
                       'PortBindings': {'8000/tcp': [{'HostIp': '', 'HostPort': '8005'}]}},
        'Mounts': [{'Type': 'volume', 'Name': 'existing-essay-data', 'Source': '/docker/volumes/existing-essay-data/_data', 'Destination': '/data', 'RW': True}],
    }
    worker = copy.deepcopy(web)
    worker['Id'] = 'old-worker'
    worker['Config']['Env'] += ['OPENAI_API_KEY_FILE=/run/secrets/openai_api_key', 'DAILY_BUDGET_USD=3']
    worker['Config']['Cmd'] = ['python', '-m', 'essay_web.worker']
    worker['Config']['StopTimeout'] = 150
    worker['HostConfig']['PortBindings'] = {}
    worker['Mounts'].append({'Type': 'bind', 'Source': '/srv/private/custom-key', 'Destination': '/run/secrets/openai_api_key', 'RW': False, 'Propagation': 'rprivate'})
    return web, worker


class FakeDocker:
    def __init__(self, failure=None, busy_check=None):
        web,worker = containers()
        self.items = {'old-web': web, 'old-worker': worker}
        self.names = {'pacheckessay-web': 'old-web', 'pacheckessay-worker': 'old-worker'}
        self.calls = []
        self.env_files = []
        self.env_contents = []
        self.failure, self.busy_check = failure, busy_check
        self.idle_checks = 0

    def docker(self, *args, check=True, timeout=None):
        self.calls.append(args)
        command=args[0]
        code,output=0,''
        if args[:2]==('container','inspect'):
            output=json.dumps([self.items[self.names[args[2]]]])
        elif command=='build':
            code=int(self.failure=='build')
        elif command=='create':
            if self.failure=='create' and any(i.startswith('new-') for i in self.items):
                code=1
            else:
                ident=f'new-{len(self.env_files)}'
                name=args[args.index('--name')+1]
                path=Path(args[args.index('--env-file')+1]);self.env_files.append(path)
                self.env_contents.append(path.read_text())
                if os.name!='nt':assert path.stat().st_mode & 0o777==0o600
                self.items[ident]={'Id':ident,'State':{'Running':False}}
                self.names[name]=ident
                output=ident
        elif command=='exec':
            if 'SELECT count(*)' in args[4]:
                self.idle_checks+=1
                output='1' if self.idle_checks==self.busy_check else '0'
            else:
                code=int(self.failure=='readiness')
        elif command=='inspect':
            output='true' if self.items[args[-1]]['State']['Running'] else 'false'
        elif command=='stop':
            self.items[args[-1]]['State']['Running']=False
        elif command=='rename':
            ident,name=args[1:]
            self.names={k:v for k,v in self.names.items() if v!=ident}
            self.names[name]=ident
        elif command=='start':
            ident=args[1]
            if self.failure=='start' and ident.startswith('new-'):
                code=1
            else:self.items[ident]['State']['Running']=True
        elif command=='rm':
            ident=args[1]
            del self.items[ident]
            self.names={k:v for k,v in self.names.items() if v!=ident}
        else:
            raise AssertionError(args)
        if code and check:raise UpdateError('Simulated Docker failure')
        return subprocess.CompletedProcess(args,code,output,'')


def test_shell_syntax():
    bash='C:/Program Files/Git/bin/bash.exe' if os.name=='nt' else shutil.which('bash')
    if not bash or not Path(bash).exists():pytest.skip('Bash unavailable')
    path=Path(__file__).resolve().parents[1]/'deploy/update.sh'
    result=subprocess.run([bash,'-n',str(path)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_update_builds_before_downtime_and_preserves_deployed_configuration():
    host=FakeDocker()
    Deployment(host,'pacheckessay-web','pacheckessay-worker').deploy('a'*40)
    commands=[call[0] for call in host.calls]
    assert commands.index('build')<commands.index('create')<commands.index('stop')
    creates=[call for call in host.calls if call[0]=='create']
    assert '8005:8000/tcp' in creates[0]
    assert any('src=existing-essay-data,dst=/data' in value for value in creates[0])
    assert any('src=/srv/private/custom-key,dst=/run/secrets/openai_api_key,readonly' in value for value in creates[1])
    assert not any('custom-key' in value for value in creates[0])
    assert 'SECURE_COOKIE=false' in host.env_contents[0]
    assert 'DAILY_BUDGET_USD=3' in host.env_contents[1]
    assert all(not path.exists() for path in host.env_files)
    assert host.names=={'pacheckessay-web':'new-0','pacheckessay-worker':'new-1'}
    assert all(item['State']['Running'] for item in host.items.values())
    assert not any(call[0] in ('volume','system','image') for call in host.calls)


@pytest.mark.parametrize('failure',['build','create','start','readiness'])
def test_failures_leave_or_restore_originals(failure):
    host=FakeDocker(failure)
    # Expire readiness immediately rather than sleeping or running a daemon.
    timeout=0 if failure=='readiness' else 120
    with pytest.raises(UpdateError):
        Deployment(host,'pacheckessay-web','pacheckessay-worker',readiness_timeout=timeout).deploy('a'*40)
    assert host.names=={'pacheckessay-web':'old-web','pacheckessay-worker':'old-worker'}
    assert all(item['State']['Running'] for item in host.items.values())
    assert all(not path.exists() for path in host.env_files)
    if failure=='build':assert not any(call[0]=='stop' for call in host.calls)


@pytest.mark.parametrize('busy_check',[1,2,3])
def test_busy_queue_before_build_during_build_or_during_switch(busy_check):
    host=FakeDocker(busy_check=busy_check)
    with pytest.raises(UpdateError,match='grading jobs'):
        Deployment(host,'pacheckessay-web','pacheckessay-worker').deploy('a'*40)
    assert host.names=={'pacheckessay-web':'old-web','pacheckessay-worker':'old-worker'}
    assert all(item['State']['Running'] for item in host.items.values())
    assert not any(call[0]=='stop' and call[-1]=='old-worker' for call in host.calls)


@pytest.mark.parametrize('fault',['different-data','key-writeable','key-in-web','network','privileged','multiline-env','limit'])
def test_preflight_refuses_unsafe_or_unsupported_configuration(fault):
    web,worker=containers()
    if fault=='different-data':worker['Mounts'][0]['Name']='another-volume'
    if fault=='key-writeable':worker['Mounts'][1]['RW']=True
    if fault=='key-in-web':web['Mounts'].append(worker['Mounts'][1])
    if fault=='network':web['HostConfig']['NetworkMode']='host'
    if fault=='privileged':web['HostConfig']['Privileged']=True
    if fault=='multiline-env':worker['Config']['Env'].append('VALUE=first\nsecond')
    if fault=='limit':web['HostConfig']['Memory']=512*1024*1024
    with pytest.raises(UpdateError):preflight_pair(web,worker)


def test_loopback_ipv6_and_security_flags_are_preserved(tmp_path):
    web,_=containers()
    web['HostConfig'].update(ReadonlyRootfs=True,CapDrop=['ALL'],SecurityOpt=['no-new-privileges:true'],Tmpfs={'/tmp':'size=32m'})
    web['HostConfig']['PortBindings']['8000/tcp']=[{'HostIp':'127.0.0.1','HostPort':'8005'},{'HostIp':'::1','HostPort':'8005'}]
    args=creation_args(web,'candidate','image',tmp_path/'env')
    assert '127.0.0.1:8005:8000/tcp' in args and '[::1]:8005:8000/tcp' in args
    assert '--read-only' in args and '--cap-drop' in args and 'ALL' in args
    assert 'no-new-privileges:true' in args and '/tmp:size=32m' in args
