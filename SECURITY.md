# Security Policy

## Reporting a Vulnerability

Private vulnerability reporting is enabled on this repository. To report a security issue privately, open the **Security** tab on GitHub, select **Advisories**, and click **Report a vulnerability**.

Please do not report undisclosed vulnerabilities, credentials, tokens, or sensitive logs in public issues or pull requests.

## Scope

This repository contains reference code, tests, a stdlib repository hygiene scanner (`tools/hygiene_check.py`), and a reusable secret-scanning workflow (`.github/workflows/gitleaks.yml`). Reports covering bypasses in the hygiene check, workflow permission escalations, or soundness bugs in the reference gate and rollback state machines are welcome.
