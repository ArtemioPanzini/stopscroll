import json
import os
import tempfile
import unittest
from pathlib import Path
from stopscroll.seed import initial_state
from stopscroll.store import load, save


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'state.json')
        self.state = initial_state(1000, '2026-11-15')

    def test_save_sets_mode_0600(self):
        save(self.state, self.path)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(load(self.path), self.state)

    def test_second_save_keeps_bak(self):
        save(self.state, self.path)
        self.state.delay_min = 60
        save(self.state, self.path)
        self.assertEqual(load(self.path + '.bak').delay_min, 45)
        self.assertEqual(os.stat(self.path + '.bak').st_mode & 0o777, 0o600)
        self.assertEqual(load(self.path).delay_min, 60)

    def test_load_falls_back_to_bak(self):
        save(self.state, self.path)
        save(self.state, self.path)
        Path(self.path).write_bytes(b'{')
        self.assertEqual(load(self.path), self.state)

    def test_load_returns_none_when_both_bad(self):
        self.assertIsNone(load(self.path))
        for contents in ('{', '{}', 'null', '{"version": 2}', '\\udcff'):
            Path(self.path).write_text(contents)
            Path(self.path + '.bak').write_bytes(b'\xff')
            self.assertIsNone(load(self.path))
