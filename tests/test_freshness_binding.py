import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import install
import kb
import retrieve


class FreshnessBindingTests(unittest.TestCase):
    def test_new_session_refresh_finds_correction_even_with_cached_current_hit(self):
        import correction
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'local'
            install.install(root, 'fiction/knowledge', 'local')
            kb.run(kb.build_parser().parse_args(['--root', str(root), 'capture', '--id', 'fixture',
                '--kind', 'fact', '--scope', 'fiction', '--statement', 'preview port 4310',
                '--source', 'fictional:user', '--authority', 'user-stated']))
            retrieve.retrieve(root, 'preview port', sync_fn=lambda *a, **kw: {'ok': True})
            other = Path(directory) / 'other-device'
            shutil.copytree(root, other)
            changed = correction.correct(other, 'fixture', 'preview port 4311', 'fictional:other-device')
            def pull(*args, **kwargs):
                self.assertFalse(kwargs['push'])
                shutil.copyfile(other / changed['ledger'], root / changed['ledger'])
                return {'ok': True, 'pulled': True}
            callback = mock.Mock(side_effect=pull)
            cached = retrieve.retrieve(root, 'preview port', sync_fn=callback)
            self.assertEqual(cached['results'][0]['statement'], 'preview port 4310')
            callback.assert_not_called()
            current = retrieve.retrieve(root, 'preview port', fresh=True, sync_fn=callback)
            self.assertEqual(current['results'][0]['statement'], 'preview port 4311')
            self.assertEqual(current['freshness']['status'], 'verified')
            callback.assert_called_once()

    def test_fresh_lookup_never_overrides_offline_or_disabled_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'local'
            install.install(root, 'fiction/knowledge', 'local')
            callback = mock.Mock(side_effect=AssertionError('network must stay disabled'))
            offline = retrieve.retrieve(root, 'marker', fresh=True, offline=True, sync_fn=callback)
            self.assertEqual(offline['freshness']['status'], 'offline')
            config = root / 'config/kb.json'
            settings = json.loads(config.read_text())
            settings['hot_sync']['enabled'] = False
            config.write_text(json.dumps(settings))
            disabled = retrieve.retrieve(root, 'marker', fresh=True, sync_fn=callback)
            self.assertEqual(disabled['freshness']['sync_attempts'], 0)
            self.assertEqual(disabled['freshness']['status'], 'not-configured')
            callback.assert_not_called()

    def test_failed_fresh_lookup_keeps_snapshot_without_claiming_remote_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'local'
            install.install(root, 'fiction/knowledge', 'local')
            kb.run(kb.build_parser().parse_args(['--root', str(root), 'capture', '--id', 'fixture',
                '--kind', 'fact', '--scope', 'fiction', '--statement', 'preview port 4310',
                '--source', 'fictional:user', '--authority', 'user-stated']))
            retrieve.retrieve(root, 'preview port', sync_fn=lambda *a, **kw: {'ok': True})
            callback = mock.Mock(return_value={'ok': False, 'status': 'network-unavailable'})
            result = retrieve.retrieve(root, 'preview port', fresh=True, sync_fn=callback)
            self.assertEqual(result['results'][0]['statement'], 'preview port 4310')
            self.assertEqual(result['freshness']['status'], 'failed:network-unavailable')
            callback.assert_called_once()

    def test_copied_changed_or_unbound_receipt_requires_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'first'
            install.install(root, 'fiction/knowledge', 'local')
            kb.run(kb.build_parser().parse_args(['--root', str(root), 'capture', '--id', 'fixture',
                '--kind', 'fact', '--scope', 'fiction', '--statement', 'marker green',
                '--source', 'fictional:user', '--authority', 'user-stated']))
            callback = mock.Mock(return_value={'ok': True})
            self.assertEqual(retrieve.retrieve(root, 'marker', sync_fn=callback)['freshness']['status'], 'verified')
            callback.reset_mock()
            self.assertEqual(retrieve.retrieve(root, 'marker', sync_fn=callback)['freshness']['status'], 'recently-verified')
            callback.assert_not_called()
            copied = Path(directory) / 'second'
            shutil.copytree(root, copied)
            self.assertEqual(retrieve.retrieve(copied, 'marker', sync_fn=callback)['freshness']['status'], 'verified')
            callback.assert_called_once()
            callback.reset_mock()
            with mock.patch.object(retrieve.device_identity, 'platform_fingerprint', return_value='another-device'):
                self.assertEqual(retrieve.retrieve(root, 'marker', sync_fn=callback)['freshness']['status'], 'verified')
            callback.assert_called_once()
            callback.reset_mock()
            state = root / 'generated/retrieval-freshness.json'
            state.write_text(json.dumps({'remote_verified_at': kb.iso_now()}))
            self.assertEqual(retrieve.retrieve(root, 'marker', sync_fn=callback)['freshness']['status'], 'verified')
            callback.assert_called_once()

    def test_malformed_receipt_does_not_claim_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'local'
            install.install(root, None, 'local')
            state = root / 'generated/retrieval-freshness.json'
            state.parent.mkdir(exist_ok=True)
            state.write_text('[]')
            result = retrieve.retrieve(root, 'missing')
            self.assertEqual(result['freshness']['status'], 'not-configured')
            self.assertIsNone(result['freshness']['remote_verified_at'])

    def test_disabled_sync_does_not_use_configured_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'local'
            install.install(root, 'fiction/knowledge', 'local')
            path = root / 'config/kb.json'
            config = json.loads(path.read_text()); config['hot_sync']['enabled'] = False
            path.write_text(json.dumps(config))
            callback = mock.Mock(side_effect=AssertionError('network must remain disabled'))
            result = retrieve.retrieve(root, 'missing', sync_fn=callback)
            self.assertEqual(result['freshness']['sync_attempts'], 0)
            callback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
