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
