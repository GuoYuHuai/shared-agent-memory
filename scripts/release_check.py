#!/usr/bin/env python3
"""Verify the explicit public file manifest and optionally export exactly those files."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
def check(root=ROOT, archive=None):
    manifest=json.loads((root/'TOOL_MANIFEST.json').read_text())
    failures=[]
    excluded_runtime=[]
    allowed=set(manifest['files'])
    for name,expected in manifest['files'].items():
        p=root/name
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            failures.append(name+': unsafe/missing path');continue
        if any(part in {'.git','.kb-data','raw-history','ledger','resources','profile','inbox','generated'} for part in p.relative_to(root).parts):
            failures.append(name+': private/runtime zone')
        content=p.read_bytes()
        if hashlib.sha256(content).hexdigest()!=expected:failures.append(name+': content changed')
    for p in root.rglob('*'):
        rel=p.relative_to(root)
        if '.git' in rel.parts:continue
        if any(part in {'__pycache__','generated','.kb-data'} for part in rel.parts):
            if p.is_file():excluded_runtime.append(rel.as_posix())
            continue
        if p.is_file() and rel.as_posix() not in allowed and rel.as_posix()!='TOOL_MANIFEST.json':
            failures.append(rel.as_posix()+': unlisted file')
    if archive and not failures:
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(allowed|{'TOOL_MANIFEST.json'}):z.write(root/rel,rel)
    return {'ok':not failures,'files':len(allowed),'failures':failures,
            'excluded_runtime':excluded_runtime,
            'privacy':'allowlisted code/docs/fictional fixtures; manual source review still required',
            'archive':str(archive) if archive and not failures else None}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path);a=p.parse_args()
    result=check(archive=a.archive);print(json.dumps(result,indent=2));raise SystemExit(0 if result['ok'] else 1)
