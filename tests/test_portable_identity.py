"""Platform path/volume contracts; simulated mounts are not physical drives."""
import json
import errno
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import device_identity as identity
import install


class PortableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portable-identity-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'knowledge'
        self.root.mkdir()
        (self.root / 'MANIFEST.json').write_text('{"knowledge_base_id":"fictional-library"}', encoding='utf-8')
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('AI_KNOWLEDGE_DEVICE_STATE', None)

    def test_linux_uses_state_home_and_ignores_relative_xdg_values(self):
        with mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity.Path, 'home', return_value=self.home):
            for value in ('', 'relative/state', '~/state'):
                with self.subTest(value=value), mock.patch.dict(os.environ, {'XDG_STATE_HOME': value}):
                    self.assertEqual(identity.local_state_path(self.root), self.home / '.local/state/AI-Knowledge/fictional-library/device.json')
            custom = self.base / 'configured state'
            with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(custom)}):
                self.assertEqual(identity.local_state_path(self.root), custom / 'AI-Knowledge/fictional-library/device.json')
                self.assertEqual(identity.local_state_path(self.root, self.home), self.home / '.local/state/AI-Knowledge/fictional-library/device.json')

    def test_linux_upgrade_keeps_existing_bound_state_and_adjacent_receipts(self):
        legacy = self.home / 'Library/Application Support/AI-Knowledge/fictional-library/device.json'
        legacy.parent.mkdir(parents=True)
        state = {'id': 'custom-device', 'knowledge_base_id': 'fictional-library', 'platform_fingerprint': 'a' * 64}
        legacy.write_text(json.dumps(state), encoding='utf-8')
        receipt = legacy.parent / 'task.json'
        receipt.write_text('retain', encoding='utf-8')
        with mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity, 'platform_fingerprint', return_value='a' * 64):
            self.assertEqual(identity.local_state_path(self.root, self.home), legacy)
            self.assertEqual(identity.resolve_device_id(self.root, home=self.home), 'custom-device')
            self.assertEqual(identity.write_state(self.root, 'custom-device', 'Fixture', self.home), legacy)
        self.assertFalse((self.home / '.local').exists())
        self.assertEqual(receipt.read_text(), 'retain')

    def test_darwin_and_windows_state_paths_are_unchanged(self):
        with mock.patch.object(identity.platform, 'system', return_value='Darwin'):
            self.assertEqual(identity.local_state_path(self.root, self.home), self.home / 'Library/Application Support/AI-Knowledge/fictional-library/device.json')
        with mock.patch.object(identity.platform, 'system', return_value='Windows'):
            self.assertEqual(identity.local_state_path(self.root, self.home), self.home / 'AppData/Local/AI-Knowledge/fictional-library/device.json')

    def test_linux_volume_uuid_uses_exact_mount_and_remains_stable(self):
        payload = {'filesystems': [{'target': str(self.base), 'uuid': 'ABCD-1234'}]}
        with mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity.shutil, 'which', return_value='/usr/bin/findmnt'), mock.patch.object(identity.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(payload).encode())) as run:
            one = identity.mounted_volume_binding(self.base)
            two = identity.mounted_volume_binding(self.base)
        self.assertEqual(one, two)
        self.assertEqual(one['method'], 'linux-filesystem-uuid')
        self.assertEqual(one['stability'], 'persistent')
        self.assertEqual(one['identity'], identity.normalize_volume_identity('linux-filesystem-uuid:abcd-1234'))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/findmnt', '--json', '--first-only', '--mountpoint', str(self.base), '--output', 'TARGET,UUID'])

    def test_missing_or_wrong_linux_uuid_is_explicit_runtime_binding(self):
        outputs = [b'{', b'{"filesystems":[]}', b'{"filesystems":[{"target":"/wrong","uuid":"ABCD"}]}', json.dumps({'filesystems': [{'target': str(self.base), 'uuid': None}]}).encode()]
        for payload in outputs:
            with self.subTest(payload=payload), mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity.shutil, 'which', return_value='findmnt'), mock.patch.object(identity.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, payload)):
                result = identity.mounted_volume_binding(self.base)
                self.assertEqual(result['stability'], 'runtime-only')
                self.assertEqual(result['method'], 'device-number')
                self.assertEqual(result['identity'], identity.normalize_volume_identity('st_dev:%d' % self.base.stat().st_dev))
        with mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity.shutil, 'which', return_value=None):
            self.assertEqual(identity.mounted_volume_binding(self.base)['stability'], 'runtime-only')

    def test_darwin_volume_uuid_preserves_existing_hash(self):
        payload = plistlib.dumps({'VolumeUUID': 'AABBCCDD-1111-2222-3333-444444444444'})
        with mock.patch.object(identity.platform, 'system', return_value='Darwin'), mock.patch.object(identity.shutil, 'which', return_value='/usr/sbin/diskutil'), mock.patch.object(identity.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, payload)):
            self.assertEqual(identity.mounted_volume_identity(self.base), identity.normalize_volume_identity('AABBCCDD-1111-2222-3333-444444444444'))

    def test_windows_guid_api_normalizes_guid_without_using_drive_letter(self):
        guid = 'AABBCCDD-1111-2222-3333-444444444444'
        def query(path, output, count):
            self.assertTrue(path.endswith('\\'))
            output.value = '\\\\?\\Volume{' + guid + '}\\'
            return 1
        kernel = mock.Mock()
        kernel.GetVolumeNameForVolumeMountPointW = mock.Mock(side_effect=query)
        self.assertEqual(identity.windows_volume_guid(self.base, kernel), guid.lower())
        with mock.patch.object(identity.platform, 'system', return_value='Windows'), mock.patch.object(identity, 'windows_volume_guid', return_value=guid.lower()):
            result = identity.mounted_volume_binding(self.base)
            self.assertEqual(result['stability'], 'persistent')
            self.assertEqual(result['method'], 'windows-volume-guid')

    def test_windows_guid_unavailable_is_runtime_only(self):
        with mock.patch.object(identity.platform, 'system', return_value='Windows'), mock.patch.object(identity, 'windows_volume_guid', side_effect=OSError('fixture unavailable')):
            self.assertEqual(identity.mounted_volume_binding(self.base)['stability'], 'runtime-only')

    def test_cold_external_mount_outside_volumes_retains_all_identity_guards(self):
        mount = self.base / 'mnt' / 'drive with spaces'
        target = mount / 'archive'
        target.mkdir(parents=True)
        try:
            (self.root / '.kb-data').symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest('host cannot create fixture directory symlink: ' + str(error))
        volume = 'sha256:' + 'a' * 64
        marker = target.with_name('archive.identity.json')
        payload = {'format': 'ai-knowledge-cold-data-v1', 'knowledge_base_id': 'fictional-library', 'data_directory': 'archive', 'manifest_sha256': 'b' * 64, 'volume_identity': {'volume_uuid': volume}}
        marker.write_text(json.dumps(payload), encoding='utf-8')
        binding = {'identity': volume, 'method': 'fixture', 'stability': 'persistent'}
        with mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity.os.path, 'ismount', side_effect=lambda p: Path(p).resolve() == mount), mock.patch.object(identity, 'mounted_volume_binding', return_value=binding), mock.patch.object(identity, 'read_state', return_value={'cold_volume_identity': volume}) as state:
            result = identity.validate_cold_data_root(self.root)
            self.assertTrue(result['valid'], result)
            self.assertEqual(result['mount'], str(mount))
            self.assertEqual(result['volume_identity_stability'], 'persistent')
            state.return_value = {'cold_volume_identity': 'sha256:' + 'c' * 64}
            self.assertEqual(identity.validate_cold_data_root(self.root)['reason'], 'cold-data-volume-not-bound-to-device')
            state.return_value = {'cold_volume_identity': volume}
            payload['knowledge_base_id'] = 'another-library'
            marker.write_text(json.dumps(payload), encoding='utf-8')
            self.assertEqual(identity.validate_cold_data_root(self.root)['reason'], 'cold-data-knowledge-base-mismatch')
        with mock.patch.object(identity.os.path, 'ismount', return_value=False):
            self.assertEqual(identity.validate_cold_data_root(self.root)['reason'], 'cold-data-volume-not-mounted')

    def test_installer_and_cold_storage_use_same_mount_finder(self):
        with mock.patch.object(identity, 'external_mount_root', return_value=self.base) as finder, mock.patch.object(identity, 'mounted_volume_binding', return_value={'identity': 'sha256:' + 'a' * 64, 'method': 'device-number', 'stability': 'runtime-only'}):
            result = install.removable_volume(self.root)
        finder.assert_called_once_with(self.root)
        self.assertEqual(result['identity_stability'], 'runtime-only')

    def test_system_volume_is_never_external_storage(self):
        system_root = Path(self.root.anchor)
        with mock.patch.object(identity.platform, 'system', return_value='Linux'), mock.patch.object(identity.os.path, 'ismount', side_effect=lambda p: Path(p) == system_root):
            with self.assertRaisesRegex(identity.DeviceIdentityError, 'system volume'):
                identity.external_mount_root(self.root)

    def test_native_volume_probe_reports_its_actual_guarantee(self):
        # This runs the host's read-only API. It is not a removable-drive test.
        result = identity.mounted_volume_binding(Path(self.root.anchor))
        self.assertRegex(result['identity'], '^sha256:[0-9a-f]{64}$')
        self.assertIn(result['stability'], {'persistent', 'runtime-only'})
        self.assertEqual(result['method'] == 'device-number', result['stability'] == 'runtime-only')

    def test_unsupported_storage_does_not_create_partial_installation(self):
        target = self.base / 'new parent' / 'knowledge'
        with mock.patch.object(install.atomic_publish, 'probe_directory', side_effect=OSError(errno.ENOTSUP, 'fixture no atomic publication')) as probe:
            with self.assertRaisesRegex(ValueError, 'private Git sync'):
                install.install(target, None, 'local')
        probe.assert_called_once_with(self.base)
        self.assertFalse(target.parent.exists())

    def test_unsupported_existing_storage_preserves_every_user_file(self):
        target = self.base / 'installed'
        install.install(target, None, 'local')
        before = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
        with mock.patch.object(install.atomic_publish, 'probe_directory', side_effect=OSError(errno.ENOTSUP, 'fixture no atomic publication')) as probe:
            with self.assertRaisesRegex(ValueError, 'cannot safely publish'):
                install.install(target, None, 'local')
        probe.assert_called_once_with(target)
        after = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_probe_runs_in_existing_root_not_its_possibly_different_parent(self):
        capability = {'ok': True, 'publication': 'hardlink', 'overwrite_protection': 'verified'}
        with mock.patch.object(install.atomic_publish, 'probe_directory', return_value=capability) as probe:
            report = install.probe_storage(self.root)
        probe.assert_called_once_with(self.root)
        self.assertEqual(report['device'], self.root.stat().st_dev)
        self.assertEqual(report['publication'], 'hardlink')

    def test_directory_appearing_during_probe_requires_recheck(self):
        target = self.base / 'new knowledge'
        def changed(_):
            target.mkdir()
            (target / 'unrelated.txt').write_text('preserve', encoding='utf-8')
            return {'ok': True, 'publication': 'hardlink', 'overwrite_protection': 'verified'}
        with mock.patch.object(install.atomic_publish, 'probe_directory', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed during capability'):
                install.install(target, None, 'local')
        self.assertEqual(list(target.iterdir()), [target / 'unrelated.txt'])

    def test_non_directory_parent_is_rejected_without_modification(self):
        blocker = self.base / 'file'
        blocker.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'available directory'):
            install.probe_storage(blocker / 'knowledge')
        self.assertEqual(blocker.read_bytes(), b'preserve')


if __name__ == '__main__':
    unittest.main()
