#!/usr/bin/env python3
"""Run a disposable two-directory demonstration, never a real two-device claim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
TOOL=Path(__file__).resolve().parents[1]
def command(args,cwd=None):
    result=subprocess.run(args,cwd=cwd,capture_output=True,text=True,
                          env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
    if result.returncode:raise RuntimeError(result.stdout+result.stderr)
    return result.stdout
def cli(root,*args):
    return json.loads(command([sys.executable,str(TOOL/'scripts/ak.py'),'--root',str(root),'--json',*args]))
def main():
    with tempfile.TemporaryDirectory(prefix='knowledge-demo-') as directory:
        a=Path(directory)/'知识 A'; b=Path(directory)/'Knowledge B'
        cli(a,'init')
        cli(a,'remember','theme blue','--id','theme-old','--scope','garden',
            '--source','fictional:demo-user')
        corrected=cli(a,'correct','theme-old','theme green','--source','fictional:demo-correction')
        cli(a,'sync')
        command(['git','clone',str(a),str(b)])
        command(['git','remote','remove','origin'],b)
        cli(b,'init')
        report=cli(b,'recall','theme','--scope','garden','--offline')
        assert report['results'][0]['id']==corrected['id'],report
        assert not (b/'.kb-data').exists()
        print(json.dumps({'ok':True,'coverage':'one machine, two directories, real local Git clone',
            'current_id':report['results'][0]['id'],'source':report['results'][0].get('source'),
            'cold_layer':'absent; hot retrieval succeeded','remote_freshness':'offline, not verified',
            'cleanup':'TemporaryDirectory finally'},indent=2))
if __name__=='__main__':main()
