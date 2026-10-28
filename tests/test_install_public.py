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
        # Contract tests simulate a verified external mount, not real hardware.
        self.real_ismount=install.os.path.ismount
        self.mount_patch=mock.patch.object(install.os.path,'ismount',side_effect=lambda path: Path(path).resolve()==Path(self.temp.name).resolve() or self.real_ismount(path))
        self.mount_patch.start();self.addCleanup(self.mount_patch.stop)
        self.volume_patch=mock.patch.object(install.device_identity,'mounted_volume_identity',return_value='sha256:'+'a'*64)
        self.volume_patch.start();self.addCleanup(self.volume_patch.stop)
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
    def test_local_only_needs_no_github_and_defaults_lightweight(self):
        result=install.install(self.root,None,'local')
        self.assertIsNone(result['installation']['repository'])
        self.assertEqual(result['installation']['private_identity'],'not-applicable-local-only')
        config=json.loads((self.root/'config/kb.json').read_text())
        self.assertFalse(config['retrieval']['include_raw_history'])
        self.assertFalse(config['history_capture']['enabled'])
        self.assertFalse(config['semantic_maintenance']['enabled'])
        self.assertFalse(config['hot_sync']['enabled'])
        self.assertEqual(subprocess.check_output(['git','-C',str(self.root),'remote'],text=True),'')
        self.assertFalse((self.root/'.kb-data/history.sqlite').exists())
        install.install(self.root,None,'local')
    def test_removable_rebind_preserves_data_and_missing_mount_fails(self):
        install.install(self.root,None,'removable')
        record=self.root/'projects/keep.md';record.write_text('keep exact')
        relocated=Path(self.temp.name)/'new mount'/'knowledge';relocated.parent.mkdir()
        self.root.rename(relocated)
        result=install.install(relocated,None,'removable')
        self.assertEqual(result['installation']['knowledge_root'],str(relocated.resolve()))
        self.assertEqual((relocated/'projects/keep.md').read_text(),'keep exact')
        missing=Path(self.temp.name)/'missing mount'/'knowledge'
        with self.assertRaises(ValueError):install.install(missing,None,'removable')
        self.assertFalse(missing.parent.exists())
    def test_local_only_clone_attachment_without_origin(self):
        install.install(self.root,None,'local')
        second=Path(self.temp.name)/'copy'
        subprocess.run(['git','clone',str(self.root),str(second)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(second),'remote','remove','origin'],check=True)
        self.assertTrue(install.install(second,None,'local')['attached_existing'])
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


    def test_receipt_binds_device_and_preserves_only_same_owner_evidence(self):
        with mock.patch.object(install.device_identity,'resolve_device_id',return_value='device-a'), mock.patch.object(install.device_identity,'platform_fingerprint',return_value='a'*64):
            install.install(self.root,'example/knowledge','local')
            marker=self.root/'.ai-knowledge/install.json';state=json.loads(marker.read_text())
            state.update({'private_verified':True,'private_identity':'verified-private','tasks':['host-a-task']})
            marker.write_text(json.dumps(state))
            original=marker.read_bytes()
            same=install.install(self.root,'example/knowledge','local')['installation']
            self.assertTrue(same['private_verified']);self.assertEqual(same['tasks'],['host-a-task'])
            receipt=self.root/'.ai-knowledge/automation/device-a/health.json';receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({**install.installation_owner(self.root),'registration':'registered'}))
            receipt_bytes=receipt.read_bytes()
        with mock.patch.object(install.device_identity,'resolve_device_id',return_value='device-b'), mock.patch.object(install.device_identity,'platform_fingerprint',return_value='b'*64):
            foreign=install.install(self.root,'example/knowledge','removable')['installation']
            self.assertFalse(foreign['private_verified']);self.assertEqual(foreign['tasks'],[])
            self.assertEqual(foreign['receipt_binding'],'rebound-unverified')
            self.assertEqual(receipt.read_bytes(),receipt_bytes)
            result=install.uninstall(self.root)
            self.assertEqual(result['foreign_receipts_preserved'],1)
            self.assertEqual(result['foreign_scheduler_status'],'not-verified')
        self.assertEqual(receipt.read_bytes(),receipt_bytes)
        backups=[json.loads(p.read_text()) for p in (self.root/'.ai-knowledge/backups').glob('*.json')]
        self.assertTrue(any(p.get('tasks')==['host-a-task'] for p in backups))

    def test_relocation_resets_verification_and_retains_unverified_task_evidence(self):
        install.install(self.root,None,'removable')
        marker=self.root/'.ai-knowledge/install.json';state=json.loads(marker.read_text())
        state.update({'private_verified':True,'tasks':['old-path-job']});marker.write_text(json.dumps(state))
        relocated=Path(self.temp.name)/'other mount';self.root.rename(relocated)
        current=install.install(relocated,None,'removable')['installation']
        self.assertEqual(current['tasks'],[]);self.assertFalse(current['private_verified'])
        self.assertEqual(current['unverified_prior_tasks'],['old-path-job'])
        with self.assertRaisesRegex(ValueError,'unverified prior tasks'):install.uninstall(relocated)

    def test_legacy_installation_cannot_inherit_verified_state(self):
        install.install(self.root,'example/knowledge','local')
        marker=self.root/'.ai-knowledge/install.json';state=json.loads(marker.read_text())
        for key in ('source_device_id','platform_fingerprint','knowledge_base_id'):state.pop(key,None)
        state.update({'private_verified':True,'tasks':['unknown-owner-task']})
        marker.write_text(json.dumps(state))
        current=install.install(self.root,'example/knowledge','local')['installation']
        self.assertFalse(current['private_verified']);self.assertEqual(current['tasks'],[])
        self.assertEqual(current['unverified_prior_tasks'],['unknown-owner-task'])
        with self.assertRaises(ValueError):install.uninstall(self.root)

    def test_uninstall_checks_current_device_and_preserves_legacy_registered_receipt(self):
        install.install(self.root,None,'local')
        owner=install.installation_owner(self.root)
        receipt=self.root/'.ai-knowledge/automation'/owner['source_device_id']/'health.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({**owner,'registration':'registered'}))
        with self.assertRaisesRegex(ValueError,'disable and verify'):install.uninstall(self.root)
        receipt.write_text(json.dumps({**owner,'registration':'disabled'}))
        legacy=self.root/'.ai-knowledge/automation/old.json'
        legacy.write_text(json.dumps({'registration':'registered','scheduler_id':'unknown-owner'}))
        legacy_bytes=legacy.read_bytes()
        with self.assertRaisesRegex(ValueError,'legacy registered scheduler ownership'):install.uninstall(self.root)
        self.assertEqual(legacy.read_bytes(),legacy_bytes)
        self.assertTrue((self.root/'.ai-knowledge/install.json').exists())

    def test_uninstall_refuses_old_device_or_path_without_attachment(self):
        install.install(self.root,None,'local')
        marker=self.root/'.ai-knowledge/install.json'
        original=marker.read_bytes()
        with mock.patch.object(install.device_identity,'platform_fingerprint',return_value='f'*64):
            with self.assertRaisesRegex(ValueError,'another device or path'):install.uninstall(self.root)
        self.assertEqual(marker.read_bytes(),original)

    def test_uninstall_recovers_each_interruption_stage_and_preserves_knowledge(self):
        stages = ('pending-before', 'pending-after', 'rule-before', 'rule-after',
                  'completion-before', 'completion-after', 'marker-before', 'marker-after',
                  'pending-delete-before', 'pending-delete-after')
        for stage in stages:
            with self.subTest(stage=stage):
                root = (Path(self.temp.name)/stage).resolve()
                install.install(root, None, 'local')
                rule = root/'AGENTS.md'
                original = b'User header\r\n' + rule.read_bytes() + b'\r\nUser footer\r\n'
                rule.write_bytes(original)
                (root/'projects/fact.md').write_bytes(b'exact fictional fact\x00\xff')
                policy_before = (root/'POLICY.md').read_bytes()
                config_before = (root/'config/kb.json').read_bytes()
                original_save, original_write, original_unlink = install.save, install.write_uninstall_rule, Path.unlink
                interrupted = [False]
                def interrupt():
                    interrupted[0] = True
                    raise KeyboardInterrupt('fixture interrupted process')
                def save(path, value):
                    group = 'pending' if path.name == 'uninstall-pending.json' else 'completion' if path.name == 'uninstalled.json' else ''
                    if stage == group + '-before':
                        interrupt()
                    original_save(path, value)
                    if stage == group + '-after':
                        interrupt()
                def write(path, before, after):
                    if stage == 'rule-before':
                        interrupt()
                    original_write(path, before, after)
                    if stage == 'rule-after':
                        interrupt()
                def unlink(path, *args, **kwargs):
                    group = 'marker' if path == root/'.ai-knowledge/install.json' else 'pending-delete' if path == root/'.ai-knowledge/uninstall-pending.json' else ''
                    if stage == group + '-before':
                        interrupt()
                    result = original_unlink(path, *args, **kwargs)
                    if stage == group + '-after':
                        interrupt()
                    return result
                with mock.patch.object(install, 'save', side_effect=save), mock.patch.object(install, 'write_uninstall_rule', side_effect=write), mock.patch.object(Path, 'unlink', unlink):
                    with self.assertRaises(KeyboardInterrupt):
                        install.uninstall(root)
                self.assertTrue(interrupted[0])
                if (root/'.ai-knowledge/uninstall-pending.json').exists():
                    with self.assertRaisesRegex(ValueError, 'uninstall recovery pending'):
                        install.install(root, None, 'local')
                result = install.uninstall(root)
                self.assertTrue(result['knowledge_preserved'])
                start, end = original.index(install.BEGIN.encode()), original.index(install.END.encode()) + len(install.END)
                self.assertEqual(rule.read_bytes(), original[:start] + original[end:])
                self.assertEqual((root/'projects/fact.md').read_bytes(), b'exact fictional fact\x00\xff')
                self.assertEqual((root/'POLICY.md').read_bytes(), policy_before)
                self.assertEqual((root/'config/kb.json').read_bytes(), config_before)
                self.assertFalse((root/'.ai-knowledge/install.json').exists())
                self.assertFalse((root/'.ai-knowledge/uninstall-pending.json').exists())
                repeated = install.uninstall(root)
                self.assertTrue(repeated['already_uninstalled'])
                self.assertTrue(install.install(root, None, 'local')['attached_existing'])

    def test_pending_uninstall_preserves_changed_block_and_can_keep_surrounding_edits(self):
        install.install(self.root, None, 'local')
        rule = self.root/'AGENTS.md'
        original = rule.read_text()
        with mock.patch.object(install, 'write_uninstall_rule', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                install.uninstall(self.root)
        changed = original.replace('Read this knowledge', 'My changed rule: read this knowledge')
        rule.write_text(changed)
        with self.assertRaisesRegex(ValueError, 'modified or owned'):
            install.uninstall(self.root)
        self.assertEqual(rule.read_text(), changed)
        self.assertTrue((self.root/'.ai-knowledge/install.json').exists())
        rule.write_text('new user header\n' + original + '\nnew footer')
        result = install.uninstall(self.root)
        self.assertTrue(result['knowledge_preserved'])
        self.assertIn('new user header', rule.read_text())
        self.assertIn('new footer', rule.read_text())
        self.assertNotIn(install.BEGIN, rule.read_text())

    def test_pending_removal_refuses_unexplained_text_changes_without_overwriting(self):
        install.install(self.root, None, 'local')
        original_save = install.save
        def fail_completion(path, value):
            if path.name == 'uninstalled.json':
                raise OSError('fixture failure')
            return original_save(path, value)
        with mock.patch.object(install, 'save', side_effect=fail_completion):
            with self.assertRaises(OSError):
                install.uninstall(self.root)
        rule = self.root/'AGENTS.md'
        after = rule.read_bytes()
        changed = after + b'user change while removal was pending'
        rule.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, 'changed after pending removal'):
            install.uninstall(self.root)
        self.assertEqual(rule.read_bytes(), changed)
        self.assertTrue((self.root/'.ai-knowledge/uninstall-pending.json').exists())

    def test_completed_uninstall_requires_matching_owner_and_keeps_foreign_rules(self):
        install.install(self.root, None, 'local')
        install.uninstall(self.root)
        with mock.patch.object(install.device_identity, 'platform_fingerprint', return_value='different-device'):
            with self.assertRaisesRegex(ValueError, 'no matching installation'):
                install.uninstall(self.root)
        rule = self.root/'AGENTS.md'
        rule.write_text(install.BEGIN + '\nforeign block\n' + install.END)
        before = rule.read_bytes()
        with self.assertRaisesRegex(ValueError, 'block exists after completed uninstall'):
            install.uninstall(self.root)
        self.assertEqual(rule.read_bytes(), before)

    def test_pending_uninstall_rechecks_scheduler_and_connection_guards(self):
        install.install(self.root, None, 'local')
        with mock.patch.object(install, 'write_uninstall_rule', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                install.uninstall(self.root)
        before = (self.root/'AGENTS.md').read_bytes()
        owner = install.installation_owner(self.root)
        receipt = self.root/'.ai-knowledge/automation'/owner['source_device_id']/'health.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({**owner, 'registration': 'registered'}))
        with self.assertRaisesRegex(ValueError, 'disable and verify'):
            install.uninstall(self.root)
        receipt.write_text(json.dumps({**owner, 'registration': 'disabled'}))
        import instruction_link
        with mock.patch.object(instruction_link, 'connections', return_value=[{'connection': 'connected'}]):
            with self.assertRaisesRegex(ValueError, 'disconnect connected'):
                install.uninstall(self.root)
        self.assertEqual((self.root/'AGENTS.md').read_bytes(), before)
        self.assertTrue(install.uninstall(self.root)['knowledge_preserved'])

    def test_uninstall_will_not_write_through_foreign_receipt_symlink(self):
        install.install(self.root, None, 'local')
        outside = Path(self.temp.name)/'foreign.json'
        outside.write_text('foreign content')
        (self.root/'.ai-knowledge/uninstalled.json').symlink_to(outside)
        before = (self.root/'AGENTS.md').read_bytes()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            install.uninstall(self.root)
        self.assertEqual(outside.read_text(), 'foreign content')
        self.assertEqual((self.root/'AGENTS.md').read_bytes(), before)

    def test_uninstall_preserves_foreign_completion_and_invalid_pending_receipts(self):
        install.install(self.root, None, 'local')
        completed = self.root/'.ai-knowledge/uninstalled.json'
        completed.write_text(json.dumps({'owner': 'someone else'}))
        before = (self.root/'AGENTS.md').read_bytes()
        with self.assertRaisesRegex(ValueError, 'receipt belongs to another'):
            install.uninstall(self.root)
        self.assertEqual(json.loads(completed.read_text()), {'owner': 'someone else'})
        self.assertEqual((self.root/'AGENTS.md').read_bytes(), before)
        completed.unlink()
        pending = self.root/'.ai-knowledge/uninstall-pending.json'
        pending.write_text(json.dumps({'format': 'unknown'}))
        with self.assertRaisesRegex(ValueError, 'recovery state does not match'):
            install.uninstall(self.root)
        self.assertEqual(json.loads(pending.read_text()), {'format': 'unknown'})
        self.assertEqual((self.root/'AGENTS.md').read_bytes(), before)

    def test_uninstall_missing_or_unmanaged_root_never_creates_lock_directories(self):
        missing = Path(self.temp.name)/'missing-drive'/'knowledge'
        with self.assertRaisesRegex(ValueError, 'directory unavailable'):
            install.uninstall(missing)
        self.assertFalse(missing.parent.exists())
        ordinary = Path(self.temp.name)/'ordinary'
        ordinary.mkdir()
        (ordinary/'note.md').write_text('user note')
        with self.assertRaisesRegex(ValueError, 'directory unavailable'):
            install.uninstall(ordinary)
        self.assertEqual([p.name for p in ordinary.iterdir()], ['note.md'])
        install.install(self.root, None, 'local')
        policy = self.root/'POLICY.md'
        policy.unlink()
        generated = self.root/'generated'
        for path in generated.iterdir():
            path.unlink()
        generated.rmdir()
        with self.assertRaisesRegex(ValueError, 'directory unavailable'):
            install.uninstall(self.root)
        self.assertFalse(generated.exists())
        self.assertTrue((self.root/'.ai-knowledge/install.json').exists())


    def test_moved_root_can_read_existing_index_without_history_scan(self):
        install.install(self.root,None,'removable')
        def cli(root,*args):
            result=subprocess.run([sys.executable,str(ROOT/'scripts/kb.py'),'--root',str(root),*args,'--json'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return json.loads(result.stdout)
        cli(self.root,'capture','--id','moving-fact','--kind','fact','--scope','project:aurora','--statement','Aurora uses cobalt blue','--source','fictional:user','--authority','user-stated')
        self.assertTrue((self.root/'.kb-data/knowledge.sqlite').is_file())
        self.assertFalse((self.root/'.kb-data/history.sqlite').exists())
        relocated=Path(self.temp.name)/'移动目录';self.root.rename(relocated)
        result=install.install(relocated,None,'removable')
        self.assertFalse(result['installation']['index_rebind']['history_scanned'])
        found=cli(relocated,'search','cobalt','--scope','project:aurora')
        self.assertEqual(found['results'][0]['id'],'moving-fact')
        self.assertEqual(found['results'][0]['statement'],'Aurora uses cobalt blue')
        self.assertFalse((relocated/'.kb-data/history.sqlite').exists())


    def test_removable_rejects_existing_empty_mount_directory_on_system_volume(self):
        empty_mount=Path(self.temp.name)/'unmounted-drive';empty_mount.mkdir()
        target=empty_mount/'knowledge'
        with mock.patch.object(install.os.path,'ismount',side_effect=self.real_ismount):
            with self.assertRaisesRegex(ValueError,'system volume|not mounted'):
                install.install(target,None,'removable')
        self.assertFalse(target.exists())
        self.assertEqual(list(empty_mount.iterdir()),[])

    def test_removable_records_volume_and_rejects_different_volume_without_writes(self):
        initial=install.install(self.root,None,'removable')['installation']
        self.assertEqual(initial['removable_volume']['volume_identity'],'sha256:'+'a'*64)
        marker=self.root/'.ai-knowledge/install.json';before=marker.read_bytes()
        with mock.patch.object(install.device_identity,'mounted_volume_identity',return_value='sha256:'+'b'*64):
            with self.assertRaisesRegex(ValueError,'another volume'):
                install.install(self.root,None,'removable')
        self.assertEqual(marker.read_bytes(),before)
        self.assertFalse((self.root/'.ai-knowledge/backups').exists())

    def test_removable_rechecks_mount_before_creating_root(self):
        with mock.patch.object(install.device_identity,'mounted_volume_identity',side_effect=['sha256:'+'a'*64,'sha256:'+'b'*64]):
            with self.assertRaisesRegex(ValueError,'changed during installation'):
                install.install(self.root,None,'removable')
        self.assertFalse(self.root.exists())

if __name__=='__main__':unittest.main()
