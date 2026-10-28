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
    return json.loads(command([sys.executable,str(TOOL/'scripts/kb.py'),'--root',str(root),*args,'--json']))
def main():
    with tempfile.TemporaryDirectory(prefix='knowledge-demo-') as directory:
        a=Path(directory)/'知识 A'; b=Path(directory)/'Knowledge B'
        command([sys.executable,str(TOOL/'scripts/install.py'),'--root',str(a),'--repository','example/private-knowledge'])
        for id_,color in [('theme-old','blue'),('theme-new','green')]:
            cli(a,'capture','--id',id_,'--kind','fact','--scope','garden','--statement','theme '+color,
                '--source','fictional:demo-user','--authority','user-stated','--source-device-id','demo-device')
        cli(a,'supersede','--old-id','theme-old','--new-id','theme-new','--source','fictional:demo-correction')
        command(['git','add','ledger'],a)
        command(['git','-c','user.name=Demo','-c','user.email=demo@example.test','commit','-m','Fictional facts'],a)
        command(['git','clone',str(a),str(b)])
        report=json.loads(command([sys.executable,str(TOOL/'scripts/retrieve.py'),'--root',str(b),'theme','--scope','garden','--offline']))
        assert report['results'][0]['id']=='theme-new',report
        assert not (b/'.kb-data').exists()
        print(json.dumps({'ok':True,'coverage':'one machine, two directories, real local Git clone',
            'current_id':report['results'][0]['id'],'source':report['results'][0].get('source'),
            'cold_layer':'absent; hot retrieval succeeded','remote_freshness':'offline, not verified',
            'cleanup':'TemporaryDirectory finally'},indent=2))
if __name__=='__main__':main()
