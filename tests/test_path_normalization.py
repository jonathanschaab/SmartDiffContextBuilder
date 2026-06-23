"""Tests for path suffix matching boundary checks and path normalization in caching."""

import os
import unittest
from unittest.mock import MagicMock, patch
from context_builder.cache import LRUFileCache
from context_builder.ast_engine import (
    get_directly_included_files,
    get_class_members,
    find_class_definition,
    resolve_global_definition,
)


class TestPathNormalization(unittest.TestCase):
    """Test suite for path normalization and component boundary matching."""

    @patch("context_builder.ast_engine._get_stripped_lines", return_value=["import ast"])
    @patch("context_builder.ast_engine.get_language_profile")
    def test_path_suffix_boundary_matching(self, mock_profile_get, _mock_stripped):
        """Verify that path suffix matching in get_directly_included_files only matches
        at path component boundaries.
        """
        profile = MagicMock()
        profile.name = "python"
        profile.comment_prefix = "#"
        mock_profile_get.return_value = profile

        file_cache = MagicMock()
        # Mock file cache get_directly_included_files_cache dict to avoid cached hits
        file_cache.get_directly_included_files_cache = {}

        # 1. Test false positive rejection (toast.py vs ast)
        with patch(
            "context_builder.sys_utils.get_git_tracked_files",
            return_value=["some/dir/toast.py"],
        ):
            with patch("os.path.exists", return_value=True):
                res = get_directly_included_files(
                    "main.py", profile, file_cache
                )
                # Should not match toast.py for 'import ast'
                self.assertEqual(res, [])

        # 2. Test true positive acceptance (ast.py vs ast)
        # Clear cache for the next lookup
        file_cache.get_directly_included_files_cache = {}
        with patch(
            "context_builder.sys_utils.get_git_tracked_files",
            return_value=["some/dir/ast.py"],
        ):
            with patch("os.path.exists", return_value=True):
                res = get_directly_included_files(
                    "main.py", profile, file_cache
                )
                # Should match ast.py at boundary
                self.assertEqual(
                    res, [os.path.abspath("some/dir/ast.py")]
                )

    def test_lru_file_cache_key_normalization(self):
        """Verify LRUFileCache normalizes relative and absolute paths to absolute keys."""
        # Using a temporary or dummy file path
        rel_path = "dummy_file.py"
        abs_path = os.path.abspath(rel_path)

        cache = LRUFileCache(max_size_mb=1.0)
        # Seed cache via _load mock/stub or directly checking cache dictionary
        # Let's patch os.path.exists and open to avoid actual I/O
        with patch("os.path.exists", return_value=True):
            with patch(
                "builtins.open",
                unittest.mock.mock_open(read_data=b"dummy content"),
            ):
                # Request via relative path
                entry_rel = cache.get_lines(rel_path)
                # Request via absolute path
                entry_abs = cache.get_lines(abs_path)

                # Ensure only one entry is stored in cache, keyed by absolute path
                self.assertIn(abs_path, cache.cache)
                self.assertNotIn(rel_path, cache.cache)
                self.assertEqual(entry_rel, entry_abs)

    @patch("context_builder.ast_engine._get_stripped_lines", return_value=["class MyClass {", "}"])
    def test_ast_engine_caches_normalization(self, _mock_stripped):
        """Verify ast_engine functions normalize relative and absolute paths in their cache keys."""
        rel_path = "dummy_file.py"
        abs_path = os.path.abspath(rel_path)
        profile = MagicMock()
        profile.strip_strings_and_comments.side_effect = lambda x: x
        profile.uses_indentation_blocks = False

        file_cache = MagicMock()
        file_cache.class_members_cache = {}
        file_cache.find_class_definition_cache = {}
        file_cache.get_directly_included_files_cache = {}
        file_cache.resolve_global_definition_cache = {}

        # 1. get_class_members
        get_class_members(rel_path, "MyClass", profile, file_cache)
        # Verify cached under absolute path only
        self.assertIn((abs_path, "MyClass"), file_cache.class_members_cache)
        self.assertNotIn((rel_path, "MyClass"), file_cache.class_members_cache)

        # 2. find_class_definition
        find_class_definition(rel_path, "MyClass", profile, file_cache)
        self.assertIn(
            (abs_path, "MyClass"), file_cache.find_class_definition_cache
        )
        self.assertNotIn(
            (rel_path, "MyClass"), file_cache.find_class_definition_cache
        )

        # 3. get_directly_included_files
        get_directly_included_files(rel_path, profile, file_cache)
        self.assertIn(abs_path, file_cache.get_directly_included_files_cache)
        self.assertNotIn(
            rel_path, file_cache.get_directly_included_files_cache
        )

        # 4. resolve_global_definition
        with patch(
            "context_builder.sys_utils.get_git_tracked_files", return_value=[]
        ):
            resolve_global_definition(
                rel_path, "var", profile, file_cache
            )
            self.assertIn(
                (abs_path, "var"), file_cache.resolve_global_definition_cache
            )
            self.assertNotIn(
                (rel_path, "var"), file_cache.resolve_global_definition_cache
            )
