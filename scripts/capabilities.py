#!/usr/bin/env python3
"""Probe local operations; do not infer permissions from an Agent brand."""
import datetime
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

NAMES = ('public_repo_read private_repo_read private_repo_write private_repo_create atomic_commit '
         'local_files local_exec persistent_instructions lifecycle_hooks scheduler unattended_model history_access').split()
def probe():
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    report={n:{'declared':None,'status':'unknown','checked_at':now,'evidence':None,'reason':'not probed'} for n in NAMES}
    with tempfile.TemporaryDirectory(prefix='knowledge-probe-') as directory:
        p=Path(directory)/'probe';p.write_text('probe',encoding='utf-8')
        if p.read_text(encoding='utf-8')=='probe':
            report['local_files'].update(status='verified',evidence='isolated write/read/delete',reason=None)
    result=subprocess.run([sys.executable,'-c','print("exec-ok")'],capture_output=True,text=True,timeout=10)
    if result.returncode==0 and result.stdout.strip()=='exec-ok':
        report['local_exec'].update(status='verified',evidence={'python':platform.python_version(),'os':platform.system()},reason=None)
    return {'capabilities':report,'mode':'local','private_repository':'unverified','automation':'unplanned'}
if __name__=='__main__':print(json.dumps(probe(),indent=2))
