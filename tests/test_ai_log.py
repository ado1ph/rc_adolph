import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class ConversationArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "docs" / "adr").mkdir(parents=True)
        for name in (
            "ai-collaboration.md", "references.md", "verification.md",
            "architecture.md", "production-runbook.md",
        ):
            (self.root / "docs" / name).write_text("Summary\n")
        (self.root / "GLOSSARY.md").write_text("Glossary\n")
        source = Path(__file__).resolve().parents[1] / "scripts" / "package_ai_log.py"
        spec = importlib.util.spec_from_file_location("package_ai_log", source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.__file__ = str(self.root / "scripts" / "package_ai_log.py")

    def package(self, *arguments):
        with patch("sys.argv", ["package_ai_log.py", *arguments]), patch("builtins.print"):
            self.module.main()
        return zipfile.ZipFile(self.root / "artifacts" / "ai-session.zip")

    def test_default_includes_actual_excerpt_with_partial_label(self):
        excerpt = self.root / "docs" / "ai-conversation-excerpts.md"
        excerpt.write_text("User: actual request\nAssistant: actual reply\n")
        with self.package() as archive:
            self.assertEqual(
                archive.read("transcript/" + excerpt.name).decode(),
                excerpt.read_text(),
            )
            self.assertIn("PARTIAL CONVERSATION", archive.read("STATUS.txt").decode())
            self.assertIn("NOT a complete", archive.read("STATUS.txt").decode())

    def test_missing_excerpt_remains_summary_only(self):
        with self.package() as archive:
            self.assertIn("SUMMARY ONLY", archive.read("STATUS.txt").decode())
            self.assertFalse(any(name.startswith("transcript/") for name in archive.namelist()))

    def test_explicit_export_is_preserved_without_claiming_completeness(self):
        export = self.root / "actual-export.md"
        export.write_text("Actual exported text\n")
        with self.package("--transcript", str(export)) as archive:
            self.assertEqual(archive.read("transcript/actual-export.md"), export.read_bytes())
            self.assertIn("completeness and redaction must be checked",
                          archive.read("STATUS.txt").decode())


if __name__ == "__main__":
    unittest.main()
