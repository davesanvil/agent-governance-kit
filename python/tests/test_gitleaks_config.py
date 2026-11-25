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


def rule_matches_without_allowlist(rule: dict[str, Any], text: str) -> bool:
    pattern = re.compile(str(rule["regex"]))
    allow_patterns: list[re.Pattern[str]] = []
    for entry in rule.get("allowlists", []):
        if entry.get("regexTarget") != "match":
            raise AssertionError(f"allowlist must target 'match', got {entry.get('regexTarget')!r}")
        for raw_rx in entry.get("regexes", []):
            allow_patterns.append(re.compile(str(raw_rx)))

    for match in pattern.finditer(text):
        matched = match.group(0)
        if not any(allow.search(matched) for allow in allow_patterns):
            return True
    return False


class GitleaksConfigTest(unittest.TestCase):
    def test_extends_default_rules(self) -> None:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(data.get("extend", {}).get("useDefault"))

    def test_onepassword_service_account_token_rule(self) -> None:
        rule = load_rules()["onepassword-service-account-token-extended"]
        self.assertIn("ops_", rule["keywords"])
        self.assertTrue(rule.get("allowlists"))

        # Assemble fixtures dynamically so the test file itself never trips secret scanners.
        standard_b64 = "ops_" + ("AbCdEf0123456789+/" * 8) + "=="
        base64url = "ops_" + ("AbCdEf0123456789-_" * 8)
        short_eyj = "ops_" + "eyJ" + ("aB09_-" * 20)
        dotted_payload = "ops_" + ("A1b2." * 20) + "="
        underscore_prefixed = "_" + "ops_" + "eyJ" + ("aB09_-" * 20)
        real_with_test_in_body = "ops_" + "eyJ" + ("AbCdEf01" * 10) + "TeSttestTEST" + ("GhIjKl23" * 10)
        self.assertLess(len(short_eyj), 250)

        for label, sample in (
            ("standard_b64", standard_b64),
            ("base64url", base64url),
            ("short_eyj_under_250", short_eyj),
            ("dotted_payload", dotted_payload),
            ("underscore_prefixed", underscore_prefixed),
            ("real_with_test_in_body", real_with_test_in_body),
        ):
            with self.subTest(positive=label):
                self.assertTrue(rule_matches_without_allowlist(rule, sample))

        op_ref = ":".join(("op", "//vault/item/field"))
        for label, sample in (
            ("op_reference_uri", op_ref),
            ("too_short_ops", "ops_" + "A" * 99),
            ("short_identifier", "ops_example_label"),
            ("synthetic_all_x", "ops_" + ("X" * 120)),
            ("synthetic_all_0", "ops_" + ("0" * 120)),
            ("synthetic_example_prefix", "ops_EXAMPLE" + ("aB09_-" * 20)),
            ("synthetic_dummy_prefix", "ops_DUMMY" + ("aB09_-" * 20)),
            ("synthetic_placeholder_suffix", "ops_eyJ" + ("aB09_-" * 20) + "PLACEHOLDER"),
            ("synthetic_redacted_suffix", "ops_eyJ" + ("aB09_-" * 20) + "REDACTED=="),
        ):
            with self.subTest(negative=label):
                self.assertFalse(rule_matches_without_allowlist(rule, sample))

    def test_onepassword_secret_key_rule(self) -> None:
        rule = load_rules()["onepassword-secret-key"]
        self.assertIn("A3-", rule["keywords"])
        self.assertTrue(rule.get("allowlists"))

        # 34-alphanumeric 1Password Secret Key formats: A3-6-6-5-5-5-5 and A3-6-11-5-5-5.
        seven_segment = "A3-" + "K8M2P9" + "-" + "Q4W7E2-R5T8Y-U3I6O-P9A4S-D7F1G"
        six_segment_11 = "A3-" + "K8M2P9" + "-" + "Q4W7E2R5T8Y-U3I6O-P9A4S-D7F1G"
        real_with_one_test_group = "A3-" + "K8M2P9" + "-" + "Q4W7E2-TEST9-U3I6O-P9A4S-D7F1G"

        for label, sample in (
            ("seven_segment", seven_segment),
            ("six_segment_11", six_segment_11),
            ("real_with_one_test_group", real_with_one_test_group),
        ):
            with self.subTest(positive=label):
                self.assertTrue(rule_matches_without_allowlist(rule, sample))

        op_ref = ":".join(("op", "//vault/item/field"))
        for label, sample in (
            ("op_reference_uri", op_ref),
            ("short_suffix", "A3-" + "AB12CD" + "-" + "EF34GH123"),
            ("twenty_hyphens", "A3-" + "AB12CD" + "-" + ("-" * 21)),
            ("doc_short_6seg_x", "A3-XXXXXX-XXXXXX-XXXXX-XXXXX-XXXXX"),
            ("doc_short_6seg_0", "A3-000000-000000-00000-00000-00000"),
            ("doc_short_6seg_dummy", "A3-AB12CD-DUMMY1-DUMMY-DUMMY-DUMMY"),
            ("doc_redacted_placeholder", "A3-AB12CD-REDACTED-PLACEHOLDER-TEST"),
            ("synthetic_7seg_x", "A3-XXXXXX-XXXXXX-XXXXX-XXXXX-XXXXX-XXXXX"),
            ("synthetic_7seg_0", "A3-000000-000000-00000-00000-00000-00000"),
            ("synthetic_7seg_dummy", "A3-AB12CD-DUMMY1-DUMMY-DUMMY-DUMMY-DUMMY"),
            ("synthetic_6seg_placeholder", "A3-AB12CD-PLACEHOLDER-XXXXX-XXXXX-XXXXX"),
            ("lowercase", "a3-" + "k8m2p9" + "-" + "q4w7e2-r5t8y-u3i6o-p9a4s-d7f1g"),
        ):
            with self.subTest(negative=label):
                self.assertFalse(rule_matches_without_allowlist(rule, sample))


if __name__ == "__main__":
    unittest.main()
