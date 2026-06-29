# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=protected-access,import-outside-toplevel,unused-argument

import os
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from context_builder.ast_engine import (
    find_class_definition,
    get_directly_included_files,
    resolve_global_definition,
)


class TestAstCaching(unittest.TestCase):
    def test_file_cache_helpers_evict_least_recently_used_entry(self):
        from context_builder.ast_engine import _get_lru_cache, _lru_get, _lru_set

        owner = SimpleNamespace(
            custom_cache=OrderedDict([
                ("old-but-hot", 1),
                ("newer-but-cold", 2),
            ])
        )

        cache = _get_lru_cache(owner, "custom_cache")
        value, found = _lru_get(cache, "old-but-hot")
        self.assertTrue(found)
        self.assertEqual(value, 1)

        _lru_set(cache, "newest", 3, max_size=2)

        self.assertIn("old-but-hot", cache)
        self.assertIn("newest", cache)
        self.assertNotIn("newer-but-cold", cache)

    def test_file_cache_helpers_evict_when_byte_bound_is_exceeded(self):
        from context_builder.ast_engine import _lru_set

        cache = OrderedDict()
        _lru_set(cache, "old", "x" * 2048, max_size=10, max_bytes=4096)
        _lru_set(cache, "new", "y" * 2048, max_size=10, max_bytes=4096)

        self.assertNotIn("old", cache)
        self.assertIn("new", cache)

    def test_file_cache_helpers_update_byte_accounting_incrementally(self):
        from context_builder.ast_engine import _lru_set

        cache = OrderedDict()
        _lru_set(cache, "existing", "value", max_size=10, max_bytes=100000)
        original_total = getattr(cache, "_total_bytes")

        with patch(
            "context_builder.ast_engine._estimate_cache_entry_size",
            side_effect=[7, 11],
        ) as mock_estimate_size:
            _lru_set(cache, "new", "value", max_size=10, max_bytes=100000)

        self.assertEqual(mock_estimate_size.call_count, 2)
        self.assertEqual(getattr(cache, "_entry_sizes")["new"], 18)
        self.assertEqual(getattr(cache, "_total_bytes"), original_total + 18)

    def test_file_cache_helpers_estimate_size_uses_dedicated_lock(self):
        from context_builder.ast_engine import _estimate_lru_cache_size, _get_lru_cache

        class CountingRLock:
            def __init__(self):
                self.enter_count = 0

            def __enter__(self):
                self.enter_count += 1
                return self

            def __exit__(self, *_args):
                return False

        owner = SimpleNamespace(_lock=CountingRLock(), custom_cache={"key": "value"})
        cache = _get_lru_cache(owner, "custom_cache")
        cache._lock = CountingRLock()

        self.assertGreater(_estimate_lru_cache_size(cache), 0)
        self.assertEqual(cache._lock.enter_count, 1)

    def test_file_cache_helpers_upgrade_plain_dict_to_ordered_dict(self):
        from context_builder.ast_engine import _get_lru_cache, LRUCache

        owner = SimpleNamespace(custom_cache={"item": "value"})
        cache = _get_lru_cache(owner, "custom_cache")

        self.assertIsInstance(cache, LRUCache)
        self.assertIs(owner.custom_cache, cache)

        # Verify custom attributes can be set on it
        setattr(cache, "custom_attr", 42)
        self.assertEqual(getattr(cache, "custom_attr"), 42)

    def test_file_cache_helpers_use_dedicated_lock(self):
        from context_builder.ast_engine import _get_lru_cache, _lru_get, _lru_set

        class CountingRLock:
            def __init__(self):
                self.enter_count = 0

            def __enter__(self):
                self.enter_count += 1
                return self

            def __exit__(self, *_args):
                return False

        owner = SimpleNamespace(_lock=CountingRLock(), custom_cache={})
        cache = _get_lru_cache(owner, "custom_cache")
        cache._lock = CountingRLock()

        _lru_set(cache, "key", "value")
        value, found = _lru_get(cache, "key")

        self.assertTrue(found)
        self.assertEqual(value, "value")
        self.assertEqual(cache._lock.enter_count, 2)

    def test_strip_comments_only_ignores_empty_line_comment_marker(self):
        from context_builder.ast_engine import _strip_comments_only

        profile = MagicMock()
        profile.line_comment = ""

        self.assertEqual(_strip_comments_only("value = 1", profile), "value = 1")
        profile.strip_string_literals.assert_not_called()

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    @patch("context_builder.ast_engine.get_directly_included_files")
    def test_find_class_definition_caching(
        self, mock_get_includes, mock_get_git_tracked, mock_ripgrep
    ):
        mock_get_includes.return_value = []
        mock_get_git_tracked.return_value = []
        mock_ripgrep.return_value = []

        profile = MagicMock()
        profile.strip_strings_and_comments = lambda x: x

        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["class Other:", "    pass"]

        # 1. Negative result caching check
        res1 = find_class_definition("start.py", "TargetClass", profile, file_cache)
        self.assertEqual(res1, (None, None))
        self.assertTrue(mock_get_includes.called)

        # Reset mock calls
        mock_get_includes.reset_mock()
        mock_ripgrep.reset_mock()
        mock_get_git_tracked.reset_mock()
        file_cache.get_lines.reset_mock()

        # Run second time (should use cache and not call backend logic)
        res2 = find_class_definition("start.py", "TargetClass", profile, file_cache)
        self.assertEqual(res2, (None, None))

        mock_get_includes.assert_not_called()
        mock_ripgrep.assert_not_called()
        mock_get_git_tracked.assert_not_called()
        file_cache.get_lines.assert_not_called()

        # 2. Positive result caching check
        # Reset cache on file_cache instance
        if hasattr(file_cache, "find_class_definition_cache"):
            delattr(file_cache, "find_class_definition_cache")

        file_cache.get_lines.return_value = ["class TargetClass:", "    pass"]

        res3 = find_class_definition("start.py", "TargetClass", profile, file_cache)
        self.assertEqual(res3, ("start.py", 1))

        # Reset mock calls
        file_cache.get_lines.reset_mock()

        # Run subsequent call
        res4 = find_class_definition("start.py", "TargetClass", profile, file_cache)
        self.assertEqual(res4, ("start.py", 1))
        file_cache.get_lines.assert_not_called()

    @patch("context_builder.sys_utils.get_git_tracked_files")
    def test_get_directly_included_files_caching(self, mock_get_git_tracked):
        mock_get_git_tracked.return_value = ["a.py", "b.py"]

        profile = MagicMock()
        profile.name = "python"

        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["import a", "import b"]

        # Force os.path check to think the candidate files exist
        with patch("os.path.exists", return_value=True), patch(
            "os.path.isfile", return_value=True
        ):
            res1 = get_directly_included_files("start.py", profile, file_cache)
            self.assertTrue(len(res1) > 0)

            # Second call
            mock_get_git_tracked.reset_mock()
            file_cache.get_lines.reset_mock()

            res2 = get_directly_included_files("start.py", profile, file_cache)
            self.assertEqual(res1, res2)

            mock_get_git_tracked.assert_not_called()
            file_cache.get_lines.assert_not_called()

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    @patch("context_builder.ast_engine.get_directly_included_files")
    @patch("context_builder.ast_engine.build_scopes")
    @patch("context_builder.ast_engine.get_lines_directly_in_scope")
    @patch("context_builder.ast_engine.is_line_definition_of_var")
    def test_resolve_global_definition_caching(
        self,
        mock_is_def,
        mock_lines_in_scope,
        mock_build_scopes,
        mock_get_includes,
        mock_get_git_tracked,
        mock_ripgrep,
    ):
        mock_get_includes.return_value = []
        mock_get_git_tracked.return_value = []
        mock_ripgrep.return_value = []
        mock_build_scopes.return_value = (MagicMock(), [MagicMock()])
        mock_lines_in_scope.return_value = [1]
        mock_is_def.return_value = False

        profile = MagicMock()
        profile.strip_strings_and_comments = lambda x: x

        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["x = 42"]

        with patch("os.path.exists", return_value=True), patch(
            "context_builder.ast_engine.get_language_profile"
        ) as mock_get_lang_profile:
            mock_get_lang_profile.return_value = profile

            # 1. Negative result caching check
            res1 = resolve_global_definition(
                "start.py", "non_existent", profile, file_cache
            )
            self.assertEqual(res1, [])

            # Reset mocks
            mock_get_includes.reset_mock()
            mock_get_git_tracked.reset_mock()
            mock_ripgrep.reset_mock()
            file_cache.get_lines.reset_mock()

            # Second call
            res2 = resolve_global_definition(
                "start.py", "non_existent", profile, file_cache
            )
            self.assertEqual(res2, [])

            mock_get_includes.assert_not_called()
            mock_get_git_tracked.assert_not_called()
            mock_ripgrep.assert_not_called()
            file_cache.get_lines.assert_not_called()

            # 2. Positive result caching check
            if hasattr(file_cache, "resolve_global_definition_cache"):
                delattr(file_cache, "resolve_global_definition_cache")

            mock_is_def.return_value = True

            res3 = resolve_global_definition(
                "start.py", "TargetVar", profile, file_cache
            )
            self.assertEqual(len(res3), 1)
            self.assertEqual(res3[0]["line"], 1)

            # Reset mocks
            file_cache.get_lines.reset_mock()
            mock_is_def.reset_mock()

            # Run subsequent call
            res4 = resolve_global_definition(
                "start.py", "TargetVar", profile, file_cache
            )
            self.assertEqual(res3, res4)
            file_cache.get_lines.assert_not_called()
            mock_is_def.assert_not_called()

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    @patch("context_builder.ast_engine.get_directly_included_files")
    def test_cache_instance_isolation(
        self, mock_get_includes, mock_get_git_tracked, mock_ripgrep
    ):
        mock_get_includes.return_value = []
        mock_get_git_tracked.return_value = []
        mock_ripgrep.return_value = []

        profile = MagicMock()
        profile.strip_strings_and_comments = lambda x: x

        file_cache_1 = MagicMock()
        file_cache_1.get_lines.return_value = ["class Other:", "    pass"]

        file_cache_2 = MagicMock()
        file_cache_2.get_lines.return_value = ["class TargetClass:", "    pass"]

        # Call find_class_definition on file_cache_1 (negative result cached on file_cache_1)
        res1 = find_class_definition("start.py", "TargetClass", profile, file_cache_1)
        self.assertEqual(res1, (None, None))

        # Reset mock_get_includes so we can check if it gets called for file_cache_2
        mock_get_includes.reset_mock()

        # Call find_class_definition on file_cache_2
        # (should NOT use the negative cache from file_cache_1)
        res2 = find_class_definition("start.py", "TargetClass", profile, file_cache_2)
        self.assertEqual(res2, ("start.py", 1))
        # Since it was not cached on file_cache_2, it should have done the lookup and read the lines
        file_cache_2.get_lines.assert_called_once_with("start.py")

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    @patch("context_builder.ast_engine.get_directly_included_files")
    def test_find_class_definition_cache_bounding(
        self, mock_get_includes, mock_get_git_tracked, mock_ripgrep
    ):
        mock_get_includes.return_value = []
        mock_get_git_tracked.return_value = []
        mock_ripgrep.return_value = []

        profile = MagicMock()
        profile.strip_strings_and_comments = lambda x: x

        file_cache = MagicMock()
        initial_cache = {
            os.path.abspath(f"file_{i}.py"): ("somewhere.py", i)
            for i in range(1024)
        }
        file_cache.find_class_definition_cache = initial_cache
        file_cache.get_lines.return_value = ["class TargetClass:", "    pass"]

        res = find_class_definition("start.py", "TargetClass", profile, file_cache)
        self.assertEqual(res, ("start.py", 1))

        self.assertEqual(len(file_cache.find_class_definition_cache), 1024)
        self.assertNotIn(
            os.path.abspath("file_0.py"), file_cache.find_class_definition_cache
        )
        self.assertIn(
            (os.path.abspath("start.py"), "TargetClass"),
            file_cache.find_class_definition_cache,
        )

    @patch("context_builder.sys_utils.get_git_tracked_files")
    def test_get_directly_included_files_cache_bounding(self, mock_get_git_tracked):
        mock_get_git_tracked.return_value = []
        profile = MagicMock()
        profile.name = "python"

        file_cache = MagicMock()
        initial_cache = {os.path.abspath(f"file_{i}.py"): [] for i in range(1024)}
        file_cache.get_directly_included_files_cache = initial_cache
        file_cache.get_lines.return_value = []

        res = get_directly_included_files("start.py", profile, file_cache)
        self.assertEqual(res, [])

        self.assertEqual(len(file_cache.get_directly_included_files_cache), 1024)
        self.assertNotIn(os.path.abspath("file_0.py"), file_cache.get_directly_included_files_cache)
        self.assertIn(os.path.abspath("start.py"), file_cache.get_directly_included_files_cache)

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    @patch("context_builder.ast_engine.get_directly_included_files")
    @patch("context_builder.ast_engine.build_scopes")
    @patch("context_builder.ast_engine.get_lines_directly_in_scope")
    @patch("context_builder.ast_engine.is_line_definition_of_var")
    def test_resolve_global_definition_cache_bounding(
        self,
        mock_is_def,
        mock_lines_in_scope,
        mock_build_scopes,
        mock_get_includes,
        mock_get_git_tracked,
        mock_ripgrep,
    ):
        mock_get_includes.return_value = []
        mock_get_git_tracked.return_value = []
        mock_ripgrep.return_value = []
        mock_build_scopes.return_value = (MagicMock(), [MagicMock()])
        mock_lines_in_scope.return_value = [1]
        mock_is_def.return_value = True

        profile = MagicMock()
        profile.strip_strings_and_comments = lambda x: x

        file_cache = MagicMock()
        initial_cache = {
            os.path.abspath(f"file_{i}.py"): [] for i in range(1024)
        }
        file_cache.resolve_global_definition_cache = initial_cache
        file_cache.get_lines.return_value = ["x = 42"]

        with patch("os.path.exists", return_value=True), patch(
            "context_builder.ast_engine.get_language_profile", return_value=profile
        ):
            res = resolve_global_definition("start.py", "x", profile, file_cache)
            self.assertEqual(len(res), 1)

            self.assertEqual(len(file_cache.resolve_global_definition_cache), 1024)
            self.assertNotIn(
                os.path.abspath("file_0.py"),
                file_cache.resolve_global_definition_cache,
            )
            self.assertIn(
                (os.path.abspath("start.py"), "x"),
                file_cache.resolve_global_definition_cache,
            )
