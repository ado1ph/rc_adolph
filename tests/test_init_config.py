import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Install production dependencies")
class InitConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "scripts" / "init_config.py"
        spec = importlib.util.spec_from_file_location("init_config", source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.__file__ = str(self.root / "scripts" / "init_config.py")

    def run_main(self, uid=10001):
        output = io.StringIO()
        with patch("sys.argv", ["init_config", "--origin", "https://supplier.example",
                                "--development"]), patch.object(
            self.module.os, "getuid", return_value=uid,
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.module.main()
        return output.getvalue()

    def test_private_files_and_no_printed_tokens(self):
        output = self.run_main()
        directory = self.root / ".secrets"
        for path in list(directory.iterdir()) + [self.root / ".env"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for path in (directory, self.root / ".data", self.root / ".data/prometheus"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        clients = json.loads((directory / "auth.json").read_text())
        self.assertEqual(len(clients), 4)
        for role in ("sender", "operator", "reconciler", "metrics"):
            token = (directory / (role + "-token")).read_text().strip()
            self.assertNotIn(token, output)
            self.assertGreaterEqual(len(token), 32)

    def test_existing_env_fails_before_creating_secrets(self):
        env = self.root / ".env"
        env.write_text("existing")
        with self.assertRaises(SystemExit):
            self.run_main()
        self.assertFalse((self.root / ".secrets").exists())
        self.assertEqual(env.read_text(), "existing")

    def test_existing_secrets_untouched(self):
        self.run_main()
        env = (self.root / ".env").read_bytes()
        keys = (self.root / ".secrets/keys.json").read_bytes()
        with self.assertRaises(SystemExit):
            self.run_main()
        self.assertEqual((self.root / ".env").read_bytes(), env)
        self.assertEqual((self.root / ".secrets/keys.json").read_bytes(), keys)

    def test_root_refused_without_writing(self):
        with self.assertRaises(SystemExit):
            self.run_main(uid=0)
        self.assertEqual(os.listdir(self.root), [])
