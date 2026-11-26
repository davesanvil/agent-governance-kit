from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

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


def extract_manifest_checker_script(text: str) -> str:
    marker = "python3 - \"$BUNDLE_PATH\" \"$EXPECTED_VERSION_CODE\" <<'PY'\n"
    start = text.find(marker)
    if start == -1:
        raise AssertionError("missing AAB manifest versionCode inspector block")
    body_start = start + len(marker)
    end = text.find("\n          PY\n", body_start)
    if end == -1:
        raise AssertionError("missing terminating PY heredoc marker")
    return textwrap.dedent(text[body_start:end]) + "\n"


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb_varint(field_num: int, value: int) -> bytes:
    return encode_varint((field_num << 3) | 0) + encode_varint(value)


def pb_bytes(field_num: int, payload: bytes) -> bytes:
    return encode_varint((field_num << 3) | 2) + encode_varint(len(payload)) + payload


def build_synthetic_aab(
    path: Path,
    version_code: int | None,
    *,
    use_compiled_item: bool = True,
    root_name: str = "manifest",
    child_version_code: int | None = None,
) -> None:
    attrs: list[bytes] = [pb_bytes(2, b"package") + pb_bytes(3, b"com.example.app")]
    if version_code is not None:
        attr = pb_bytes(2, b"versionCode") + pb_varint(5, 0x0101021B)
        if use_compiled_item:
            prim = pb_varint(6, version_code)
            attr += pb_bytes(6, pb_bytes(7, prim))
        else:
            attr += pb_bytes(3, str(version_code).encode("utf-8"))
        attrs.append(attr)

    elem = pb_bytes(3, root_name.encode("utf-8"))
    for attr in attrs:
        elem += pb_bytes(4, attr)
    if child_version_code is not None:
        child_attr = (
            pb_bytes(2, b"versionCode")
            + pb_varint(5, 0x0101021B)
            + pb_bytes(6, pb_bytes(7, pb_varint(6, child_version_code)))
        )
        child_elem = pb_bytes(3, b"uses-sdk") + pb_bytes(4, child_attr)
        elem += pb_bytes(5, pb_bytes(1, child_elem))

    xml_node = pb_bytes(1, elem)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("base/manifest/AndroidManifest.xml", xml_node)


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
            "user_fraction",
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

    def test_upload_jobs_serialize_per_package_share_steps_and_validate_guards(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("persist-credentials: false", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("umask 077", text)
        self.assertIn("if: always()", text)
        self.assertIn("Draft App API quirk", text)
        self.assertIn("[[ \"$BUNDLE_PATH\" == *[*?\\[\\]{},\\\\]* ]]", text)
        self.assertIn("^[1-9][0-9]{0,9}$", text)
        self.assertIn("^[0-9]{1,9}$", text)
        self.assertIn("10#$VERSION_CODE_OVERRIDE", text)
        self.assertIn("10#$VERSION_CODE_OFFSET", text)
        self.assertIn("userFraction:", text)

        publish_env = extract_job_block(text, "publish", "publish_no_env")
        publish_no_env = extract_job_block(text, "publish_no_env", None)

        self.assertIn("environment: ${{ inputs.play_environment }}", publish_env)
        self.assertNotIn("environment:", publish_no_env)
        expected_group = "group: play-upload-${{ github.repository }}-${{ inputs.package_name }}"
        self.assertIn(expected_group, publish_env)
        self.assertIn(expected_group, publish_no_env)
        self.assertNotIn("inputs.track", publish_env.split("\n    outputs:\n", 1)[0])
        self.assertNotIn("inputs.track", publish_no_env.split("\n    outputs:\n", 1)[0])
        self.assertIn("cancel-in-progress: false", publish_env)
        self.assertIn("cancel-in-progress: false", publish_no_env)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish_env)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish_no_env)

        env_steps = publish_env.split("\n    steps:\n", 1)[1]
        no_env_steps = publish_no_env.split("\n    steps:\n", 1)[1]
        self.assertEqual(env_steps, no_env_steps)

    def test_embedded_aab_manifest_version_code_inspector(self) -> None:
        script = extract_manifest_checker_script(WORKFLOW_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            matching_aab = tmp_dir / "matching.aab"
            string_aab = tmp_dir / "string.aab"
            mismatch_aab = tmp_dir / "mismatch.aab"
            missing_vc_aab = tmp_dir / "missing_vc.aab"

            build_synthetic_aab(matching_aab, 205, child_version_code=999)
            build_synthetic_aab(string_aab, 205, use_compiled_item=False)
            build_synthetic_aab(mismatch_aab, 50)
            build_synthetic_aab(missing_vc_aab, None, child_version_code=205)

            ok = subprocess.run([sys.executable, "-", str(matching_aab), "205"], input=script, text=True, capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn("versionCode=205", ok.stdout)

            ok_str = subprocess.run([sys.executable, "-", str(string_aab), "205"], input=script, text=True, capture_output=True)
            self.assertEqual(ok_str.returncode, 0, ok_str.stderr)

            bad = subprocess.run([sys.executable, "-", str(mismatch_aab), "205"], input=script, text=True, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("does not match ANDROID_VERSION_CODE (205)", bad.stderr)

            missing = subprocess.run([sys.executable, "-", str(missing_vc_aab), "205"], input=script, text=True, capture_output=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("versionCode attribute missing", missing.stderr)


if __name__ == "__main__":
    unittest.main()
