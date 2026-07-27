import os
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixture-app")
FIXTURE_MPK = os.path.join(ROOT, "tests", "fixture-app.mpk")
SCRIPT = os.path.join(ROOT, "publish.py")

sys.path.insert(0, ROOT)
import publish  # noqa: E402


class ApiCompatibilityTests(unittest.TestCase):
    def test_publisher_does_not_use_badge_registration_ping_as_health_check(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn('f"{api_base}/ping"', source)


class WaitForRevisionAdvanceTests(unittest.TestCase):
    # BadgeHub's publish endpoint returns 204 before its public
    # revision-check endpoint necessarily reflects it (confirmed on the
    # bomberboy v0.1.3/v0.1.4 releases) -- these mock get_latest_revision
    # and time.sleep so the retry/backoff logic is verified without any
    # real network calls or real delays.

    @patch("publish.time.sleep")
    @patch("publish.get_latest_revision")
    def test_succeeds_immediately_when_already_advanced(self, mock_get, mock_sleep):
        mock_get.return_value = 6
        result = publish.wait_for_revision_advance("base", "slug", 5)
        self.assertEqual(result, 6)
        mock_sleep.assert_not_called()

    @patch("publish.time.sleep")
    @patch("publish.get_latest_revision")
    def test_retries_until_it_advances(self, mock_get, mock_sleep):
        mock_get.side_effect = [5, 5, 6]
        result = publish.wait_for_revision_advance("base", "slug", 5)
        self.assertEqual(result, 6)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("publish.time.sleep")
    @patch("publish.get_latest_revision")
    def test_gives_up_after_all_retries_exhausted(self, mock_get, mock_sleep):
        mock_get.return_value = 5
        result = publish.wait_for_revision_advance("base", "slug", 5)
        self.assertEqual(result, 5)  # still the stale value, not None
        self.assertEqual(mock_get.call_count, 5)
        self.assertEqual(mock_sleep.call_count, 4)  # no sleep after the last attempt

    @patch("publish.time.sleep")
    @patch("publish.get_latest_revision")
    def test_no_previous_revision_accepts_first_result(self, mock_get, mock_sleep):
        # First-ever publish for a project: previous_revision is None, so
        # any non-None result should be accepted immediately.
        mock_get.return_value = 1
        result = publish.wait_for_revision_advance("base", "slug", None)
        self.assertEqual(result, 1)
        mock_sleep.assert_not_called()


class QuotePathTests(unittest.TestCase):
    def test_subdirectory_slash_is_percent_encoded(self):
        # A raw "/" in the URL splits BadgeHub's single-segment {filePath}
        # route param into an extra unmatched segment (404 "Cannot POST") --
        # this is what actually broke the first real (non-dry-run) publish.
        self.assertEqual(publish.quote_path("sounds/BombDrop.wav"), "sounds%2FBombDrop.wav")

    def test_flat_filename_is_unchanged(self):
        self.assertEqual(publish.quote_path("MANIFEST.JSON"), "MANIFEST.JSON")


class PublishDryRunTests(unittest.TestCase):
    def _run(self, extra_env=None):
        env = os.environ.copy()
        # Tests run inside GitHub Actions CI inherit real GITHUB_SERVER_URL /
        # GITHUB_REPOSITORY values for *this* repo -- clear them so the new
        # git-url default (see GitUrlDefaultTests) doesn't leak in here.
        env.pop("GITHUB_SERVER_URL", None)
        env.pop("GITHUB_REPOSITORY", None)
        env.update(
            {
                "BADGEHUB_API_BASE": "https://badgehub.eu/api/v3",
                "BADGEHUB_SLUG": "fixture-app",
                "BADGEHUB_API_TOKEN": "test-token",
                "BADGEHUB_APP_DIR": FIXTURE,
                "BADGEHUB_DRY_RUN": "true",
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, env=env)

    def test_dry_run_succeeds_and_uploads_expected_files(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MANIFEST.JSON", result.stdout)
        self.assertIn("fixture_app.py", result.stdout)
        self.assertIn("Published 'fixture-app'", result.stdout)

    def test_dry_run_logs_subdirectory_files_percent_encoded(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uploaded sounds/beep.txt", result.stdout)
        self.assertIn("draft/files/sounds%2Fbeep.txt", result.stdout)

    def test_dry_run_never_touches_the_network(self):
        # An unresolvable host would fail immediately on any real request,
        # so this only passes if dry-run truly never attempts one.
        result = self._run({"BADGEHUB_API_BASE": "https://this-host-does-not-exist.invalid/api/v3"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mpk_path_is_uploaded_when_given(self):
        result = self._run({"BADGEHUB_MPK_PATH": FIXTURE_MPK})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uploaded fixture-app.mpk", result.stdout)
        self.assertIn("draft/files/fixture-app.mpk", result.stdout)

    def test_mpk_path_is_not_uploaded_when_omitted(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(".mpk", result.stdout)

    def test_warns_when_mpk_path_omitted_and_targeting_mpos(self):
        # Default badges is "mpos_api_0", the one platform confirmed to need
        # a .mpk/.zip among the uploaded files to be installable at all.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mpos_api_0", result.stderr)

    def test_no_warning_when_not_targeting_mpos(self):
        result = self._run({"BADGEHUB_BADGES": "why2025"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("mpos_api_0", result.stderr)

    def test_no_warning_when_mpk_path_given(self):
        result = self._run({"BADGEHUB_MPK_PATH": FIXTURE_MPK})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Warning", result.stderr)

    def test_git_url_defaults_to_calling_repo(self):
        result = self._run(
            {
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "tjorim/bomberboy-mpos",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"git_url": "https://github.com/tjorim/bomberboy-mpos"', result.stdout)

    def test_git_url_omitted_when_no_repo_env(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("git_url", result.stdout)

    def test_explicit_git_url_overrides_repo_env(self):
        result = self._run(
            {
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "tjorim/bomberboy-mpos",
                "BADGEHUB_GIT_URL": "https://example.com/fork",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"git_url": "https://example.com/fork"', result.stdout)


if __name__ == "__main__":
    unittest.main()
