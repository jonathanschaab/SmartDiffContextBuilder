# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring

import unittest

from context_builder.path_utils import (
    build_root_replacement_variants,
    is_windows_drive_path,
    normalize_for_path_match,
    normalize_root_for_path_match,
    to_backslashes,
    to_forward_slashes,
)


class TestPathUtils(unittest.TestCase):
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

    def test_build_root_replacement_variants_covers_slash_styles_and_drive_case(self):
        variants = build_root_replacement_variants(r"C:\Repo", r"D:\worktree")

        self.assertIn(("C:/Repo", "D:/worktree"), variants)
        self.assertIn(("c:/Repo", "D:/worktree"), variants)
        self.assertIn((r"C:\Repo", r"D:\worktree"), variants)
        self.assertIn((r"c:\Repo", r"D:\worktree"), variants)


if __name__ == "__main__":
    unittest.main()
