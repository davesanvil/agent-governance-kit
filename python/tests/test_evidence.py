import unittest

from govkit.evidence import Claim, DuplicateClaim, Overall, Status, render, rollup

REQUIRED = ["tests", "read_back", "health"]


def ok(check_id: str) -> Claim:
    return Claim(check_id, Status.VERIFIED, method="test run", evidence=f"logs/{check_id}.txt")


ALL_OK = [ok(c) for c in REQUIRED]


class RollupTest(unittest.TestCase):
    def test_all_verified_is_green(self) -> None:
        self.assertIs(rollup(REQUIRED, ALL_OK).overall, Overall.GREEN)

    def test_missing_claim_is_not_run_not_success(self) -> None:
        report = rollup(REQUIRED, ALL_OK[:2])
        self.assertIs(report.overall, Overall.INCOMPLETE)
        self.assertIs(report.results[2].status, Status.NOT_RUN)

    def test_one_failure_is_red(self) -> None:
        claims = ALL_OK[:2] + [Claim("health", Status.FAILED, method="probe", evidence="logs/health.txt")]
        self.assertIs(rollup(REQUIRED, claims).overall, Overall.RED)

    def test_failure_beats_not_run_and_unknown(self) -> None:
        claims = [Claim("tests", Status.FAILED, method="test run"), Claim("read_back", Status.UNKNOWN, method="x")]
        self.assertIs(rollup(REQUIRED, claims).overall, Overall.RED)

    def test_empty_requirements_never_green(self) -> None:
        self.assertIs(rollup([], []).overall, Overall.INCOMPLETE)

    def test_duplicate_required_ids_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rollup(["tests", "tests"], [])

    def test_unrequired_pass_is_listed_not_counted(self) -> None:
        report = rollup(["tests"], [ok("tests"), ok("bonus")])
        self.assertIs(report.overall, Overall.GREEN)
        self.assertEqual([r.check_id for r in report.unrequired], ["bonus"])

    def test_unrequired_failure_is_still_red(self) -> None:
        report = rollup(["tests"], [ok("tests"), Claim("extra", Status.FAILED, method="probe", evidence="x.log")])
        self.assertIs(report.overall, Overall.RED)
        self.assertIn("not required: extra (failed)", render(report))


class WeakEvidenceTest(unittest.TestCase):
    def assert_downgraded(self, claim: Claim, reason: str) -> None:
        report = rollup(REQUIRED, ALL_OK[:2] + [claim])
        self.assertIs(report.overall, Overall.INCOMPLETE)
        self.assertIs(report.results[2].status, Status.UNKNOWN)
        self.assertIn(reason, report.results[2].reason)

    def test_verified_without_evidence(self) -> None:
        self.assert_downgraded(Claim("health", Status.VERIFIED, method="probe"), "no evidence")

    def test_verified_with_blank_evidence(self) -> None:
        self.assert_downgraded(Claim("health", Status.VERIFIED, method="probe", evidence="  "), "no evidence")

    def test_verified_without_method(self) -> None:
        # An arbitrary pointer with no stated method is not verification.
        self.assert_downgraded(Claim("health", Status.VERIFIED, method="", evidence="anything"), "no method")

    def test_second_hand_report(self) -> None:
        claim = Claim("health", Status.VERIFIED, method="probe", evidence="they said ok", firsthand=False)
        self.assert_downgraded(claim, "second-hand")


class DuplicateClaimTest(unittest.TestCase):
    def test_failed_then_verified_cannot_become_green(self) -> None:
        # With last-write-wins, a later VERIFIED would overwrite an observed FAILED.
        claims = [Claim("tests", Status.FAILED, method="test run", evidence="logs/run1.txt"), *ALL_OK]
        with self.assertRaises(DuplicateClaim):
            rollup(REQUIRED, claims)

    def test_verified_then_failed_is_also_rejected(self) -> None:
        claims = [*ALL_OK, Claim("tests", Status.FAILED, method="test run", evidence="logs/run2.txt")]
        with self.assertRaisesRegex(DuplicateClaim, "tests"):
            rollup(REQUIRED, claims)


class RenderTest(unittest.TestCase):
    def test_render_keeps_every_check_and_reason(self) -> None:
        claims = [ok("tests"), Claim("health", Status.VERIFIED, method="probe")]
        text = render(rollup(REQUIRED, claims))
        self.assertEqual(
            text.splitlines(),
            [
                "overall: incomplete",
                "verified: tests",
                "not_run: read_back (no claim)",
                "unknown: health (claimed verified: no evidence pointer)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
