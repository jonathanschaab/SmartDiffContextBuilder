# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring,too-many-public-methods

import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from context_builder.config import CONFIG, reset_config
from context_builder.path_utils import (
    _iter_case_override_candidates,  # pylint: disable=protected-access
    build_root_replacement_variants,
    clear_path_case_caches,
    detect_root_case_sensitivity,
    find_artifact_path,
    get_path_case_override,
    is_explicit_posix_style_path,
    is_path_case_sensitive,
    is_windows_drive_path,
    is_windows_style_path,
    normalize_for_path_match,
    normalize_case_rule_path,
    path_is_within_root,
    normalize_root_for_path_match,
    to_backslashes,
    to_forward_slashes,
)


class TestPathUtils(unittest.TestCase):
    def setUp(self):
        reset_config()
        clear_path_case_caches()

    def tearDown(self):
        reset_config()
        clear_path_case_caches()

    def test_slash_normalizers_are_explicit(self):
        self.assertEqual(to_forward_slashes(r"C:\repo\src\main.cpp"), "C:/repo/src/main.cpp")
        self.assertEqual(to_backslashes("C:/repo/src/main.cpp"), r"C:\repo\src\main.cpp")

    def test_normalize_for_path_match_is_case_insensitive(self):
        self.assertEqual(
            normalize_for_path_match(r"C:\Repo\Src\Main.cpp"),
            "c:/repo/src/main.cpp",
        )

    def test_normalize_root_for_path_match_adds_boundary_separator(self):
        self.assertEqual(
            normalize_root_for_path_match(r"C:\Repo"),
            "c:/repo/",
        )
        self.assertEqual(normalize_root_for_path_match(""), "")

    def test_windows_drive_detection_is_narrow(self):
        self.assertTrue(is_windows_drive_path(r"C:\repo"))
        self.assertTrue(is_windows_drive_path("d:/repo"))
        self.assertFalse(is_windows_drive_path("/repo"))
        self.assertFalse(is_windows_drive_path("relative/path"))

    def test_path_style_detection_distinguishes_posix_windows_and_ambiguous(self):
        self.assertTrue(is_windows_style_path(r"..\src\main.cpp"))
        self.assertTrue(is_windows_style_path(r"\\server\share\repo"))
        self.assertTrue(is_explicit_posix_style_path("../../mnt/c/Users"))
        self.assertFalse(is_explicit_posix_style_path("src/main.cpp"))

    def test_build_root_replacement_variants_covers_slash_styles_and_drive_case(self):
        variants = build_root_replacement_variants(r"C:\Repo", r"D:\worktree")

        self.assertIn(("C:/Repo", "D:/worktree"), variants)
        self.assertIn(("c:/Repo", "D:/worktree"), variants)
        self.assertIn((r"C:\Repo", r"D:\worktree"), variants)
        self.assertIn((r"c:\Repo", r"D:\worktree"), variants)

    def test_case_override_matches_normalized_root_path(self):
        CONFIG["path_case_rules"] = [
            {
                "pattern": r"^C:/Repo/CaseSensitive(?:/|$)",
                "case_sensitive": True,
            }
        ]
        clear_path_case_caches()

        self.assertTrue(
            get_path_case_override(
                "src/main.cpp",
                root_path=r"C:\Repo\CaseSensitive",
            )
        )

    @patch("context_builder.path_utils._run_git_probe_process")
    def test_detect_root_case_sensitivity_prefers_git_signal(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="true\n", stderr="")

        self.assertFalse(detect_root_case_sensitivity(r"C:\Repo"))
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 5.0)

    @patch("context_builder.path_utils._run_git_probe_process")
    def test_detect_root_case_sensitivity_falls_back_to_root_style(self, mock_run):
        mock_run.side_effect = OSError("git unavailable")

        self.assertFalse(detect_root_case_sensitivity(r"C:\Repo"))
        clear_path_case_caches()
        self.assertTrue(detect_root_case_sensitivity("/repo"))

    @patch("context_builder.sys_utils.warn_once")
    @patch("subprocess.run")
    def test_detect_root_case_sensitivity_warns_on_git_timeout(
        self, mock_run, mock_warn
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=5)

        self.assertFalse(detect_root_case_sensitivity(r"C:\Repo"))
        mock_warn.assert_called_once()
        self.assertEqual(mock_warn.call_args.args[0], "git_probe_timeout")
        self.assertIn("--git-probe-timeout", mock_warn.call_args.args[1])
        self.assertIn("'git_probe_timeout'", mock_warn.call_args.args[1])

    @patch("context_builder.sys_utils.warn_once")
    @patch("context_builder.path_utils._run_git_probe_process")
    def test_detect_root_case_sensitivity_warns_on_invalid_git_probe_timeout(
        self, mock_run, mock_warn
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="false\n", stderr="")
        CONFIG["git_probe_timeout"] = "bad"

        self.assertTrue(detect_root_case_sensitivity("/repo"))
        mock_warn.assert_called_once()
        self.assertEqual(mock_warn.call_args.args[0], "git_probe_timeout_invalid")
        self.assertIn("--git-probe-timeout", mock_warn.call_args.args[1])
        self.assertIn("'git_probe_timeout'", mock_warn.call_args.args[1])
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 5.0)

    @patch("context_builder.path_utils._run_git_probe_process")
    def test_is_path_case_sensitive_uses_override_before_root_heuristic(self, mock_run):
        mock_run.side_effect = OSError("git unavailable")
        CONFIG["path_case_rules"] = [
            {
                "pattern": r"^/repo/case-insensitive(?:/|$)",
                "case_sensitive": False,
            }
        ]
        clear_path_case_caches()

        self.assertFalse(
            is_path_case_sensitive(
                "src/main.cpp",
                root_path="/repo/case-insensitive",
            )
        )

    @patch("context_builder.path_utils._run_git_probe_process")
    def test_is_path_case_sensitive_treats_explicit_posix_relative_path_as_sensitive(
        self, mock_run
    ):
        mock_run.side_effect = OSError("git unavailable")

        self.assertTrue(
            is_path_case_sensitive(
                "../../mnt/c/Users",
                root_path=r"C:\Repo",
            )
        )

    def test_normalize_case_rule_path_trims_trailing_separator(self):
        self.assertEqual(
            normalize_case_rule_path(r"C:\Repo\CaseSensitive\\"),
            "C:/Repo/CaseSensitive",
        )

    def test_path_is_within_root_honors_case_sensitivity(self):
        self.assertTrue(
            path_is_within_root(
                r"C:\Repo\Src\main.cpp",
                r"C:\Repo",
                case_sensitive=False,
            )
        )
        self.assertFalse(
            path_is_within_root(
                r"c:\repo\Src\main.cpp",
                r"C:\Repo",
                case_sensitive=True,
            )
        )

    @patch("os.path.isabs", return_value=False)
    def test_iter_case_override_candidates_absolute_windows_path_on_linux(self, _mock_isabs):
        # Even if os.path.isabs returns False (simulating Linux environment),
        # a Windows absolute path should not be treated as relative and joined with root.
        candidates = list(_iter_case_override_candidates(
            r"C:\Repo\src\main.cpp",
            root_path=r"C:\Repo"
        ))
        self.assertNotIn("C:/Repo/C:/Repo/src/main.cpp", candidates)
        self.assertIn("C:/Repo/src/main.cpp", candidates)

    def test_iter_case_override_candidates_handles_none_path_value(self):
        # When path_value is None, it should not raise a TypeError
        # and should return candidates for root_path.
        candidates = list(_iter_case_override_candidates(None, root_path=r"C:\Repo"))
        self.assertIn("C:/Repo", candidates)

    def test_find_artifact_path_defensive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Non-list / Non-iterable build_directories
            CONFIG["build_directories"] = 123
            # Should not raise TypeError and return None
            self.assertIsNone(find_artifact_path("compile_commands.json", base_dir=tmpdir))

            # 2. List containing non-string elements (e.g. integers, dicts)
            CONFIG["build_directories"] = ["build", 123, None, {"a": 1}]
            # Should gracefully skip non-string elements and not crash
            self.assertIsNone(find_artifact_path("compile_commands.json", base_dir=tmpdir))

    def test_find_artifact_path_skips_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a directory with the name "coverage.xml"
            dir_path = os.path.join(tmpdir, "coverage.xml")
            os.makedirs(dir_path, exist_ok=True)

            # Should not find it since it is a directory, not a file
            self.assertIsNone(find_artifact_path("coverage.xml", base_dir=tmpdir))

            # Now create a build directory
            build_dir = os.path.join(tmpdir, "build")
            os.makedirs(build_dir, exist_ok=True)
            # Create a folder named "compile_commands.json" inside build
            folder_cc = os.path.join(build_dir, "compile_commands.json")
            os.makedirs(folder_cc, exist_ok=True)

            CONFIG["build_directories"] = ["build"]
            self.assertIsNone(find_artifact_path("compile_commands.json", base_dir=tmpdir))




if __name__ == "__main__":
    unittest.main()
