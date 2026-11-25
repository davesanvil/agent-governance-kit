from pathlib import Path
import re
import tomllib
from typing import Any
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".gitleaks.toml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "gitleaks.yml"


def load_rules() -> dict[str, dict[str, Any]]:
    data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = data.get("rules", [])
    return {str(rule["id"]): rule for rule in rules}


def extract_workflow_fallback_toml() -> str:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    lines = workflow_text.splitlines()
    config_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.strip() == "id: config":
            config_idx = idx
            break
    if config_idx is None:
        raise AssertionError("gitleaks.yml is missing step 'id: config'")

    run_idx: int | None = None
    for idx in range(config_idx + 1, len(lines)):
        if lines[idx].strip() == "run: |":
            run_idx = idx
            break
    if run_idx is None:
        raise AssertionError("gitleaks.yml step 'id: config' is missing a 'run: |' block")

    block_lines: list[str] = []
    base_indent: int | None = None
    for line in lines[run_idx + 1 :]:
        if line.strip() == "":
            block_lines.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if base_indent is None:
            base_indent = indent
        if indent < base_indent:
            break
        block_lines.append(line[base_indent:])

    heredoc_start: int | None = None
    delimiter: str | None = None
    for idx, line in enumerate(block_lines):
        match = re.search(r"cat\s+<<'([A-Za-z0-9_]+)'\s*>\s*\"\$fallback\"", line)
        if match is not None:
            heredoc_start = idx + 1
            delimiter = match.group(1)
            break
    if heredoc_start is None or delimiter is None:
        raise AssertionError("gitleaks.yml fallback step is missing quoted heredoc into \"$fallback\"")

    heredoc_lines: list[str] = []
    for line in block_lines[heredoc_start:]:
        # Non-tab-stripped heredoc terminators must begin at column 0 of the script.
        if line == delimiter:
            return "\n".join(heredoc_lines) + "\n"
        heredoc_lines.append(line)
    raise AssertionError(f"gitleaks.yml fallback heredoc is missing column-0 terminator {delimiter!r}")


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

    def test_tailscale_key_rule(self) -> None:
        rule = load_rules()["tailscale-key"]
        self.assertIn("tskey-", rule["keywords"])
        self.assertTrue(rule.get("allowlists"))

        # Assemble fixtures dynamically so the test file itself never trips secret scanners.
        auth_sample = "tskey-" + "auth-" + "k123456CNTRL-" + "AbCdEf0123456789xyz"
        api_sample = "tskey-" + "api-" + "k987654CNTRL-" + "MnOpQrStUvWxYz012345"
        client_sample = "tskey-" + "client-" + "k112233CNTRL-" + "9876543210abcdef_-XY"
        oauth_sample = "tskey-" + "oauth-" + "k445566CNTRL-" + "ZzYyXxWwVvUuTtSs1234"
        with_test_segment = "tskey-" + "auth-" + "k123456CNTRL-" + "AbCdTeStEf0123456789"
        mixed_placeholder_and_real = (
            "tskey-auth-k123456CNTRL-" + ("X" * 20) + " " + auth_sample
        )

        for label, sample in (
            ("auth_key", auth_sample),
            ("api_key", api_sample),
            ("client_key", client_sample),
            ("oauth_key", oauth_sample),
            ("with_test_in_segment", with_test_segment),
            ("mixed_placeholder_and_real", mixed_placeholder_and_real),
        ):
            with self.subTest(positive=label):
                self.assertTrue(rule_matches_without_allowlist(rule, sample))

        for label, sample in (
            ("too_short_segment", "tskey-" + "auth-" + "k123456CNTRL-" + "AbCdEf012345678"),
            ("unknown_kind", "tskey-" + "other-" + "k123456CNTRL-" + "AbCdEf0123456789xyz"),
            ("synthetic_all_upper_x", "tskey-auth-k123456CNTRL-" + ("X" * 20)),
            ("synthetic_all_lower_x", "tskey-auth-k123456CNTRL-" + ("x" * 20)),
            ("synthetic_all_0", "tskey-api-k123456CNTRL-" + ("0" * 20)),
            ("synthetic_example_prefix", "tskey-auth-k123456CNTRL-EXAMPLE" + "AbCdEf0123456789"),
            ("synthetic_dummy_prefix", "tskey-client-k123456CNTRL-DUMMY" + "AbCdEf0123456789"),
            ("synthetic_placeholder_prefix", "tskey-oauth-k123456CNTRL-PLACEHOLDER" + "AbCdEf0123456789"),
            ("synthetic_redacted_suffix", "tskey-auth-k123456CNTRL-" + "AbCdEf0123456789" + "REDACTED"),
            ("synthetic_example_suffix", "tskey-api-k123456CNTRL-" + "AbCdEf0123456789" + "EXAMPLE"),
        ):
            with self.subTest(negative=label):
                self.assertFalse(rule_matches_without_allowlist(rule, sample))

    def test_workflow_fallback_config_matches_root_config(self) -> None:
        workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("INPUT_CONFIG_PATH: ${{ inputs.config-path }}", workflow_text)
        self.assertIn("GITLEAKS_CONFIG: ${{ steps.config.outputs.path }}", workflow_text)
        self.assertIn('fallback="${RUNNER_TEMP}/govkit-default.gitleaks.toml"', workflow_text)

        root_data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        fallback_data = tomllib.loads(extract_workflow_fallback_toml())

        self.assertEqual(fallback_data.get("extend"), root_data.get("extend"))
        self.assertEqual(fallback_data.get("rules"), root_data.get("rules"))


if __name__ == "__main__":
    unittest.main()
