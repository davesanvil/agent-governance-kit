from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "android-play-internal.yml"


def extract_job_block(text: str, job_name: str, next_job_name: str | None) -> str:
    start_marker = f"\n  {job_name}:\n"
    start = text.find(start_marker)
    if start == -1:
        raise AssertionError(f"missing job {job_name!r}")
    if next_job_name is None:
        return text[start:]
    end_marker = f"\n  {next_job_name}:\n"
    end = text.find(end_marker, start + len(start_marker))
    if end == -1:
        raise AssertionError(f"missing next job {next_job_name!r}")
    return text[start:end]


class AndroidPlayWorkflowTest(unittest.TestCase):
    def test_required_inputs_and_secrets_declared(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        for input_name in (
            "package_name",
            "bundle_task",
            "bundle_path",
            "test_task",
            "track",
            "release_status",
            "version_code_offset",
            "version_code_override",
            "java_version",
            "require_secrets",
            "upload_to_play",
            "play_environment",
            "expected_aab_sha256",
            "upload_bundle_artifact",
        ):
            self.assertIn(f"      {input_name}:", text)

        for secret_name in (
            "ANDROID_KEYSTORE_BASE64",
            "ANDROID_KEYSTORE_PASSWORD",
            "ANDROID_KEY_ALIAS",
            "ANDROID_KEY_PASSWORD",
            "PLAY_SERVICE_ACCOUNT_JSON",
        ):
            self.assertIn(f"      {secret_name}:", text)

    def test_all_actions_are_sha_pinned_with_version_comment(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        uses_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("uses:")]
        self.assertGreaterEqual(len(uses_lines), 8)
        pin_rx = re.compile(r"^uses:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}\s+#\s+v\d+")
        for line in uses_lines:
            self.assertRegex(line, pin_rx, f"unpinned action reference: {line}")

    def test_upload_jobs_share_identical_steps_and_never_cancel_in_flight(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("persist-credentials: false", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("umask 077", text)
        self.assertIn("if: always()", text)
        self.assertIn("Draft App API quirk", text)

        publish_env = extract_job_block(text, "publish", "publish_no_env")
        publish_no_env = extract_job_block(text, "publish_no_env", None)

        self.assertIn("environment: ${{ inputs.play_environment }}", publish_env)
        self.assertNotIn("environment:", publish_no_env)
        self.assertIn("cancel-in-progress: false", publish_env)
        self.assertIn("cancel-in-progress: false", publish_no_env)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish_env)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish_no_env)

        env_steps = publish_env.split("\n    steps:\n", 1)[1]
        no_env_steps = publish_no_env.split("\n    steps:\n", 1)[1]
        self.assertEqual(env_steps, no_env_steps)


if __name__ == "__main__":
    unittest.main()
