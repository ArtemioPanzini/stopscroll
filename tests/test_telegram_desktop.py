"""Check preference switching/restoration without changing the real desktop."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('stopscroll_desktop', ROOT / 'telegram/desktop.py')
desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(desktop)


class DesktopPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.backup = Path(self.directory.name) / 'config/desktop.json'
        self.old = 'org.telegram.desktop._original.desktop'
        self.pins = ['browser.desktop', self.old, 'editor.desktop']
        self.handlers = {scheme: self.old for scheme in desktop.SCHEMES}
        self.native_handler = desktop.telegram_handler
        native = patch.object(desktop, 'telegram_handler', return_value=True)
        native.start()
        self.addCleanup(native.stop)

    def command(self, args):
        if args[:2] == ['gsettings', 'get']:
            return repr(self.pins)
        if args[:2] == ['gsettings', 'set']:
            import ast
            self.pins = ast.literal_eval(args[-1])
            return ''
        if args[:3] == ['xdg-mime', 'query', 'default']:
            return self.handlers[args[-1]]
        if args[:2] == ['xdg-mime', 'default']:
            self.handlers[args[-1]] = args[-2]
            return ''
        self.fail('Unexpected desktop command')

    def test_activation_is_idempotent_and_restore_preserves_later_changes(self):
        with patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
            self.assertEqual(self.pins, ['browser.desktop', desktop.APP, 'editor.desktop'])
            self.assertEqual(set(self.handlers.values()), {desktop.APP})
            original = json.loads(self.backup.read_text())
            self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
            desktop.activate(self.backup)
            self.assertEqual(json.loads(self.backup.read_text()), original)
            self.pins.append('new-user-app.desktop')
            self.handlers[desktop.SCHEMES[0]] = 'user-chosen.desktop'
            desktop.restore(self.backup)
        self.assertEqual(self.pins, ['browser.desktop', self.old, 'editor.desktop', 'new-user-app.desktop'])
        self.assertEqual(self.handlers[desktop.SCHEMES[0]], 'user-chosen.desktop')
        self.assertEqual(self.handlers[desktop.SCHEMES[1]], self.old)
        self.assertFalse(self.backup.exists())

    def test_existing_canonical_launcher_and_unset_handlers_are_preserved(self):
        self.pins = ['browser.desktop', desktop.APP]
        self.handlers = {desktop.SCHEMES[0]: desktop.APP, desktop.SCHEMES[1]: ''}
        with patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
            desktop.restore(self.backup)
        self.assertEqual(self.pins, ['browser.desktop', desktop.APP])
        self.assertEqual(self.handlers[desktop.SCHEMES[1]], '')

    def test_favorite_unpinned_after_activation_is_not_readded(self):
        with patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
            self.pins = ['browser.desktop']
            desktop.restore(self.backup)
        self.assertEqual(self.pins, ['browser.desktop'])

    def test_unrelated_tonsite_handler_is_not_changed(self):
        self.handlers[desktop.SCHEMES[1]] = 'browser.desktop'
        with patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
        self.assertEqual(self.handlers[desktop.SCHEMES[0]], desktop.APP)
        self.assertEqual(self.handlers[desktop.SCHEMES[1]], 'browser.desktop')

    def test_restore_does_not_revert_preferences_activation_did_not_manage(self):
        self.pins = ['browser.desktop']
        self.handlers[desktop.SCHEMES[1]] = 'browser.desktop'
        with patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
            self.pins.append(desktop.APP)
            self.handlers[desktop.SCHEMES[1]] = desktop.APP
            desktop.restore(self.backup)
        self.assertEqual(self.pins, ['browser.desktop', desktop.APP])
        self.assertEqual(self.handlers[desktop.SCHEMES[1]], desktop.APP)

    def test_failed_restore_keeps_independent_recovery_after_helper_removal(self):
        with patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
        recovery = self.backup.with_name('desktop-recovery.py')

        def fail_settings(args):
            if args[:2] == ['gsettings', 'get']:
                raise RuntimeError('Desktop session unavailable')
            return self.command(args)

        with patch.object(desktop, 'command', fail_settings), self.assertRaises(RuntimeError):
            desktop.restore(self.backup)
        self.assertTrue(self.backup.exists())
        self.assertTrue(recovery.exists())
        self.assertEqual(json.loads(self.backup.read_text())['managed_handlers'], [])
        self.handlers[desktop.SCHEMES[1]] = desktop.APP
        spec = importlib.util.spec_from_file_location('standalone_recovery', recovery)
        standalone = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standalone)
        with patch.object(standalone, 'command', self.command):
            standalone.restore(self.backup)
        self.assertEqual(self.pins, ['browser.desktop', self.old, 'editor.desktop'])
        self.assertEqual(self.handlers[desktop.SCHEMES[1]], desktop.APP)
        self.assertFalse(self.backup.exists())
        self.assertFalse(recovery.exists())

    def test_browser_handler_does_not_replace_browser_favorite(self):
        self.handlers[desktop.SCHEMES[0]] = 'browser.desktop'
        with patch.object(desktop, 'telegram_handler', return_value=False), patch.object(desktop, 'command', self.command):
            desktop.activate(self.backup)
        self.assertEqual(self.pins, ['browser.desktop', self.old, 'editor.desktop'])
        self.assertEqual(self.handlers[desktop.SCHEMES[0]], 'browser.desktop')

    def test_native_handler_inspects_executable_not_display_name(self):
        import os
        data = Path(self.directory.name) / 'data'
        entries = data / 'applications'
        entries.mkdir(parents=True)
        (entries / 'browser.desktop').write_text('[Desktop Entry]\nName=Telegram\nExec=/usr/bin/browser\n')
        (entries / 'native.desktop').write_text('[Desktop Entry]\nExec="/some path/Telegram" -- %u\n')
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(data), 'XDG_DATA_DIRS': str(data)}):
            self.assertFalse(self.native_handler('browser.desktop'))
            self.assertTrue(self.native_handler('native.desktop'))

    def test_switching_does_not_normalize_other_favorites(self):
        self.assertEqual(desktop.replace_favorite(
            ['browser.desktop', 'browser.desktop', self.old, desktop.APP], self.old, desktop.APP),
            ['browser.desktop', 'browser.desktop', desktop.APP])
