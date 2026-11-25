from pathlib import Path
import re
import tomllib
from typing import Any
import unittest

CONFIG_PATH = Path(__file__).resolve().parents[2] / ".gitleaks.toml"


def load_rules() -> dict[str, dict[str, Any]]:
    data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = data.get("rules", [])
    return {str(rule["id"]): rule for rule in rules}


class GitleaksConfigTest(unittest.TestCase):
    def test_extends_default_rules(self) -> None:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(data.get("extend", {}).get("useDefault"))

    def test_onepassword_service_account_token_rule(self) -> None:
        rule = load_rules()["onepassword-service-account-token-extended"]
        self.assertIn("ops_", rule["keywords"])
        pattern = re.compile(str(rule["regex"]))

        # Assemble fixtures dynamically so the test file itself never trips secret scanners.
        standard_b64 = "ops_" + ("AbCdEf0123456789+/" * 8) + "=="
        base64url = "ops_" + ("AbCdEf0123456789-_" * 8)
        short_eyj = "ops_" + "eyJ" + ("aB09_-" * 20)
        dotted_payload = "ops_" + ("A1b2." * 20) + "="

        for label, sample in (
            ("standard_b64", standard_b64),
            ("base64url", base64url),
            ("short_eyj_under_250", short_eyj),
            ("dotted_payload", dotted_payload),
        ):
            with self.subTest(positive=label):
                self.assertLess(len(short_eyj), 250)
                self.assertIsNotNone(pattern.search(sample))

        op_ref = ":".join(("op", "//vault/item/field"))
        for label, sample in (
            ("op_reference_uri", op_ref),
            ("too_short_ops", "ops_" + "A" * 99),
            ("short_identifier", "ops_example_label"),
        ):
            with self.subTest(negative=label):
                self.assertIsNone(pattern.search(sample))

    def test_onepassword_secret_key_rule(self) -> None:
        rule = load_rules()["onepassword-secret-key"]
        self.assertIn("A3-", rule["keywords"])
        pattern = re.compile(str(rule["regex"]))

        hyphenated = "A3-" + "AB12CD" + "-" + "EF34GH-IJ56KL-MN78OP-QR90ST"
        compact_min = "A3-" + "Z9Y8X7" + "-" + "W6V5U4T3S2R1Q0P9O8N7"

        for label, sample in (
            ("hyphenated", hyphenated),
            ("compact_min", compact_min),
        ):
            with self.subTest(positive=label):
                self.assertIsNotNone(pattern.search(sample))

        op_ref = ":".join(("op", "//vault/item/field"))
        for label, sample in (
            ("op_reference_uri", op_ref),
            ("short_suffix", "A3-" + "AB12CD" + "-" + "EF34GH123"),
            ("short_middle", "A3-" + "AB12C" + "-" + "EF34GH-IJ56KL-MN78OP-QR90ST"),
            ("lowercase", "a3-" + "ab12cd" + "-" + "ef34gh-ij56kl-mn78op-qr90st"),
        ):
            with self.subTest(negative=label):
                self.assertIsNone(pattern.search(sample))


if __name__ == "__main__":
    unittest.main()
