import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import install

class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'知识 with spaces'
    def test_install_repeat_upgrade_uninstall_preserves_user_data(self):
        install.install(self.root,'example/knowledge','local')
        record=self.root/'projects/user.md';record.write_text('keep this exact fact')
        policy=self.root/'POLICY.md';policy.write_text('my policy')
        rules=self.root/'AGENTS.md';rules.write_text('my header\n'+rules.read_text()+'\nmy footer')
        result=install.install(self.root,'example/knowledge','local')
        self.assertTrue(result['repeat_install']);self.assertEqual(rules.read_text().count(install.BEGIN),1)
        self.assertEqual(policy.read_text(),'my policy')
        install.uninstall(self.root)
        self.assertEqual(record.read_text(),'keep this exact fact');self.assertIn('my footer',rules.read_text())
        self.assertNotIn(install.BEGIN,rules.read_text())
    def test_unmanaged_and_modified_rules_fail_without_overwrite(self):
        self.root.mkdir();(self.root/'AGENTS.md').write_text('user content')
        with self.assertRaises(ValueError):install.install(self.root,'example/knowledge','local')
        self.assertEqual((self.root/'AGENTS.md').read_text(),'user content')
    def test_wrong_identity_and_newer_schema_fail(self):
        install.install(self.root,'example/knowledge','local')
        with self.assertRaises(ValueError):install.install(self.root,'other/repo','local')
        p=self.root/'MANIFEST.json';p.write_text(json.dumps({'schema_version':'99'}))
        before=(self.root/'AGENTS.md').read_bytes()
        with self.assertRaises(ValueError):install.install(self.root,'example/knowledge','local')
        self.assertEqual(before,(self.root/'AGENTS.md').read_bytes())
    def test_repeat_install_retains_task_ownership(self):
        install.install(self.root,'example/knowledge','local')
        marker=self.root/'.ai-knowledge/install.json';state=json.loads(marker.read_text())
        state['tasks']=['managed-test-task'];marker.write_text(json.dumps(state))
        install.install(self.root,'example/knowledge','local')
        self.assertEqual(json.loads(marker.read_text())['tasks'],['managed-test-task'])
        with self.assertRaises(ValueError):install.uninstall(self.root)
    def test_same_schema_upgrade_and_tool_version_rollback(self):
        with mock.patch.object(install,'VERSION','0.1.0'):
            install.install(self.root,'example/knowledge','local')
        record=self.root/'projects/preserved.md';record.write_text('original')
        with mock.patch.object(install,'VERSION','0.2.0'):
            install.install(self.root,'example/knowledge','local')
        with mock.patch.object(install,'VERSION','0.1.0'):
            install.install(self.root,'example/knowledge','local')
        state=json.loads((self.root/'.ai-knowledge/install.json').read_text())
        self.assertEqual(state['tool_version'],'0.1.0');self.assertEqual(record.read_text(),'original')
        self.assertEqual(len(list((self.root/'.ai-knowledge/backups').glob('*.json'))),2)
    def test_second_device_attaches_existing_clone_preserving_policy(self):
        install.install(self.root,'example/knowledge','local')
        second=Path(self.temp.name)/'second'
        subprocess.run(['git','clone',str(self.root),str(second)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(second),'remote','set-url','origin','https://github.com/example/knowledge.git'],check=True)
        policy=second/'POLICY.md';policy.write_text('my exact chosen policy')
        result=install.install(second,'example/knowledge','local')
        self.assertTrue(result['attached_existing']);self.assertEqual(policy.read_text(),'my exact chosen policy')
        self.assertEqual((second/'AGENTS.md').read_text().count(install.BEGIN),1)
    def test_core_capture_readback_correction_on_separate_root(self):
        install.install(self.root,'example/knowledge','local')
        def run(*args):
            r=subprocess.run([sys.executable,str(ROOT/'scripts/kb.py'),'--root',str(self.root),*args,'--json'],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr+r.stdout);return json.loads(r.stdout)
        for id_,value in [('old','blue'),('new','green')]:
            run('capture','--id',id_,'--kind','fact','--scope','garden','--statement','theme '+value,'--source','fictional:user','--authority','user-stated')
        run('supersede','--old-id','old','--new-id','new','--source','fictional:correction')
        result=run('search','theme','--scope','garden')
        self.assertEqual(result['results'][0]['id'],'new')
        import hot_sync
        subprocess.run(['git','-C',str(self.root),'config','user.name','Fixture'],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.email','fixture@example.test'],check=True)
        with mock.patch.object(hot_sync,'ROOT',self.root):
            self.assertTrue(hot_sync.commit_canonical()['committed'])
        self.assertEqual(subprocess.check_output(['git','-C',str(self.root),'status','--porcelain'],text=True),'')

if __name__=='__main__':unittest.main()
