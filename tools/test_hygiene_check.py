import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hygiene_check import load_denylist, scan_repo, scan_text  # noqa: E402

# Planted values are assembled at runtime so this file does not trip the check it tests.
PRIVATE_IP = ".".join(["10", "1", "2", "3"])
ULA = "fd00" + ":1::5"
NUMERIC_V6 = "2600" + ":1000::1"
EMAIL = "someone" + "@" + "mail" + ".invalid"
NOREPLY = "1+someone" + "@users.noreply.github.com"
HOME = "/" + "home/someone/notes"
LOOPBACK = ".".join(["127", "0", "0", "1"])
KEY_HEADER = "-----BEGIN " + "PRIVATE KEY-----"


def kinds(text: str) -> set[str]:
    return {f.kind for f in scan_text(text, "x", [])}


class TextRulesTest(unittest.TestCase):
    def test_catches_mechanical_leaks(self) -> None:
        self.assertEqual(kinds(f"reach {PRIVATE_IP}"), {"ip-address"})
        self.assertEqual(kinds(f"upstream {ULA}"), {"ip-address"})
        self.assertEqual(kinds(f"resolver {NUMERIC_V6}"), {"ip-address"})
        self.assertEqual(kinds(f"bracketed [{NUMERIC_V6}]"), {"ip-address"})
        self.assertEqual(kinds(f"mail {EMAIL}"), {"email"})
        self.assertEqual(kinds("see https://" + "status" + ".internal/x"), {"url"})
        self.assertEqual(kinds(f"path {HOME}"), {"home-path"})
        self.assertEqual(kinds(KEY_HEADER), {"private-key"})
        self.assertEqual(kinds("api_key = " + "abcdefghijklmnop1234"), {"secret"})

    def test_allows_documentation_values(self) -> None:
        clean = (
            "docs at https://example.com/a, mail dev@example.com, server 192.0.2.10, "
            f"2001:db8::1, localhost {LOOPBACK}, author {NOREPLY}, slices xs[::2] xs[1::2] f(x)[3::]"
        )
        self.assertEqual(scan_text(clean, "x", []), [])

    def test_output_masks_the_match(self) -> None:
        (finding,) = scan_text(f"reach {PRIVATE_IP}", "f.md", [])
        self.assertNotIn(PRIVATE_IP, str(finding))

    def test_denylist_is_whole_word_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "deny.txt"
            path.write_text("# comment\nBlueberry\n")
            deny = load_denylist(path)
        self.assertEqual(len(scan_text("a blueberry pie", "x", deny)), 1)
        self.assertEqual(scan_text("blueberrypie", "x", deny), [])


class RepoTest(unittest.TestCase):
    def git(self, root: Path, *args: str) -> None:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)

    def make_repo(self, root: Path, email: str, message: str) -> None:
        self.git(root, "init", "-q", "-b", "main")
        (root / "a.txt").write_text("hello\n")
        self.git(root, "add", "a.txt")
        self.git(root, "-c", "user.name=Someone", "-c", f"user.email={email}", "commit", "-q", "-m", message)

    def test_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.make_repo(Path(d), NOREPLY, "Initial commit")
            self.assertEqual(scan_repo(Path(d), []), [])

    def test_personal_email_and_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.make_repo(Path(d), EMAIL, "Initial commit\n\nCo-authored-by: bot <" + NOREPLY + ">")
            found = {f.kind for f in scan_repo(Path(d), [])}
        self.assertEqual(found, {"commit-email", "commit-trailer"})

    def test_untracked_files_are_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.make_repo(Path(d), NOREPLY, "Initial commit")
            (Path(d) / "local-notes.txt").write_text(f"reach {PRIVATE_IP}\n")
            self.assertEqual(scan_repo(Path(d), []), [])


if __name__ == "__main__":
    unittest.main()
