# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,consider-using-with
# pylint: disable=import-error,too-few-public-methods

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from context_builder.cache import LRUFileCache


class TestLRUFileCache(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = os.path.join(self.temp_dir.name, "test.txt")
        # Write with explicit newline to avoid CRLF conversion on Windows
        with open(self.file_path, "w", newline="\n", encoding="utf-8") as f:
            f.write("line 1\nline 2\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cache_hit_and_eviction(self):
        # We need to measure the dynamic size of each loaded entry first.
        # We'll use a temporary, large cache to perform the measurements.
        measurer = LRUFileCache(max_size_mb=1.0)

        measurer.get_lines(self.file_path)

        # Create a second file (5 bytes)
        f2 = os.path.join(self.temp_dir.name, "test2.txt")
        with open(f2, "w", newline="\n", encoding="utf-8") as f:
            f.write("12345")
        measurer.get_lines(f2)

        # Create a third file (2 bytes)
        f3 = os.path.join(self.temp_dir.name, "test3.txt")
        with open(f3, "w", newline="\n", encoding="utf-8") as f:
            f.write("12")
        measurer.get_lines(f3)

        # Retrieve the calculated sizes directly from the cache entry dictionary
        size1 = measurer.cache[self.file_path]["size_bytes"]
        size2 = measurer.cache[f2]["size_bytes"]
        size3 = measurer.cache[f3]["size_bytes"]

        # Set cache size limit to exactly fit file1 and file2 (size1 + size2)
        limit_mb = (size1 + size2) / (1024 * 1024)
        cache = LRUFileCache(max_size_mb=limit_mb)

        # Load first file
        lines1 = cache.get_lines(self.file_path)
        self.assertEqual(lines1, ["line 1\n", "line 2\n"])
        self.assertIn(self.file_path, cache.cache)
        self.assertEqual(cache.current_size_bytes, size1)

        # Load second file
        cache.get_lines(f2)
        self.assertIn(self.file_path, cache.cache)
        self.assertIn(f2, cache.cache)
        self.assertEqual(cache.current_size_bytes, size1 + size2)

        # Load third file (which exceeds the size1 + size2 threshold)
        cache.get_lines(f3)
        # First file (oldest) must be evicted, leaving f2 and f3 (size2 + size3)
        self.assertNotIn(self.file_path, cache.cache)
        self.assertIn(f2, cache.cache)
        self.assertIn(f3, cache.cache)
        self.assertEqual(cache.current_size_bytes, size2 + size3)

    def test_get_content_and_bytes(self):
        cache = LRUFileCache(max_size_mb=5)
        content = cache.get_content(self.file_path)
        self.assertEqual(content, "line 1\nline 2\n")

        bytes_val = cache.get_bytes(self.file_path)
        self.assertEqual(bytes_val, b"line 1\nline 2\n")

    def test_nonexistent_file(self):
        cache = LRUFileCache(max_size_mb=5)
        bad_path = os.path.join(self.temp_dir.name, "doesnotexist.txt")
        self.assertEqual(cache.get_lines(bad_path), [])
        self.assertEqual(cache.get_content(bad_path), "")
        self.assertEqual(cache.get_bytes(bad_path), b"")

    def test_unreadable_file_returns_empty_entry_without_caching(self):
        cache = LRUFileCache(max_size_mb=5)

        with patch("builtins.open", side_effect=OSError("access denied")):
            self.assertEqual(cache.get_content(self.file_path), "")

        self.assertNotIn(self.file_path, cache.cache)
        self.assertEqual(cache.current_size_bytes, 0)

    def test_defensive_initialization_none(self):
        """Verify that passing None capacity and None max_size_mb defaults to 200 MB."""
        cache = LRUFileCache(capacity=None, max_size_mb=None)
        self.assertEqual(cache.max_size_bytes, 200 * 1024 * 1024)

    def test_get_global_cache_none(self):
        """Verify that calling get_global_cache with None defaults to 200 MB."""
        from context_builder.cache import _CACHE_HOLDER, get_global_cache
        _CACHE_HOLDER.clear()
        cache = get_global_cache(None)
        self.assertEqual(cache.max_size_bytes, 200 * 1024 * 1024)

    def test_get_global_cache_resize(self):
        """Verify that calling get_global_cache with a new limit resizes the cache

        and triggers immediate eviction if the new limit is smaller.
        """
        from context_builder.cache import _CACHE_HOLDER, get_global_cache
        _CACHE_HOLDER.clear()

        # 1. Initialize global cache with 5 MB
        cache = get_global_cache(5.0)
        self.assertEqual(cache.max_size_bytes, 5 * 1024 * 1024)

        # 2. Add an item and measure its size
        cache.get_lines(self.file_path)
        self.assertIn(self.file_path, cache.cache)
        item_size = cache.current_size_bytes
        self.assertGreater(item_size, 0)

        # 3. Resize global cache to be smaller than the item size (e.g. item_size - 1 bytes)
        limit_mb = (item_size - 1) / (1024 * 1024)
        get_global_cache(limit_mb)

        # The item should be immediately evicted because item_size > (item_size - 1) limit
        self.assertNotIn(self.file_path, cache.cache)
        self.assertEqual(cache.current_size_bytes, 0)

    def test_defensive_initialization_invalid(self):
        """Verify that negative/zero/None limits default defensively to 200 MB."""
        cache = LRUFileCache(max_size_mb=-5.0)
        self.assertEqual(cache.max_size_bytes, 200 * 1024 * 1024)

        cache2 = LRUFileCache(capacity=0.0)
        self.assertEqual(cache2.max_size_bytes, 200 * 1024 * 1024)

    def test_resize_method_direct(self):
        """Verify the resize method validates and updates limit correctly."""
        cache = LRUFileCache(max_size_mb=10.0)
        self.assertEqual(cache.max_size_bytes, 10 * 1024 * 1024)

        cache.resize(5.0)
        self.assertEqual(cache.max_size_bytes, 5 * 1024 * 1024)

        cache.resize(0.0)
        self.assertEqual(cache.max_size_bytes, 200 * 1024 * 1024)

        cache.resize(-1.0)
        self.assertEqual(cache.max_size_bytes, 200 * 1024 * 1024)

    @unittest.skipIf(
        sys.implementation.name != "cpython",
        "sys.getsizeof is only reliable on CPython for deep memory verification"
    )
    def test_heuristic_accuracy(self):
        """Verify that the cache's O(1) heuristic size is within a reasonable margin

        of error compared to the precise O(N) sys.getsizeof measurement.
        """
        cache = LRUFileCache(max_size_mb=1.0)
        cache.get_lines(self.file_path)

        entry = cache.cache[self.file_path]
        estimated_size = entry["size_bytes"]

        # Calculate precise O(N) size
        precise_lines_size = sys.getsizeof(entry["lines"]) + sum(
            sys.getsizeof(line) for line in entry["lines"]
        )
        precise_size = (
            sys.getsizeof(entry["bytes"])
            + sys.getsizeof(entry["content"])
            + precise_lines_size
            + 150
        )

        ratio = estimated_size / precise_size
        # The heuristic should be close (within 0.90x to 1.30x of precise size)
        self.assertTrue(
            0.90 <= ratio <= 1.30,
            f"Heuristic ratio {ratio:.2f} is outside [0.90, 1.30] range"
        )

    def test_stripped_lines_caching_memory_and_eviction(self):
        class MockProfile:
            def strip_block_comments(self, content):
                return content

        profile = MockProfile()
        measurer = LRUFileCache(max_size_mb=1.0)
        measurer.get_lines(self.file_path)
        initial_size = measurer.cache[self.file_path]["size_bytes"]

        # Call get_stripped_content, which should increase size
        measurer.get_stripped_content(self.file_path, profile)
        content_size = measurer.cache[self.file_path]["size_bytes"]
        self.assertGreater(content_size, initial_size)

        # Call get_stripped_lines, which should increase size further
        measurer.get_stripped_lines(self.file_path, profile)
        lines_size = measurer.cache[self.file_path]["size_bytes"]
        self.assertGreater(lines_size, content_size)

        # Verify eviction works with these dynamic sizes
        f2 = os.path.join(self.temp_dir.name, "test2.txt")
        with open(f2, "w", newline="\n", encoding="utf-8") as f:
            f.write("12345")
        measurer.get_lines(f2)
        size2 = measurer.cache[f2]["size_bytes"]
        self.assertGreater(size2, 0)

        # Setup cache that fits exactly the fully loaded first file,
        # but will evict if we add f2.
        limit_mb = lines_size / (1024 * 1024)
        cache = LRUFileCache(max_size_mb=limit_mb)
        cache.get_lines(self.file_path)
        cache.get_stripped_lines(self.file_path, profile)
        self.assertIn(self.file_path, cache.cache)

        # Load second file. Should evict self.file_path because total
        # size (lines_size + size2) exceeds capacity.
        cache.get_lines(f2)
        self.assertNotIn(self.file_path, cache.cache)
        self.assertIn(f2, cache.cache)
