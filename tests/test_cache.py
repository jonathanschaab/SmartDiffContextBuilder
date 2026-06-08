import os
import tempfile
import unittest
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
        # Set cache size limit to 20 bytes
        limit_mb = 20 / (1024 * 1024)
        cache = LRUFileCache(max_size_mb=limit_mb)

        # Load first file (14 bytes)
        lines1 = cache.get_lines(self.file_path)
        self.assertEqual(lines1, ["line 1\n", "line 2\n"])
        self.assertIn(self.file_path, cache.cache)
        self.assertEqual(cache.current_size_bytes, 14)

        # Create a second file (5 bytes)
        f2 = os.path.join(self.temp_dir.name, "test2.txt")
        with open(f2, "w", newline="\n", encoding="utf-8") as f:
            f.write("12345")

        cache.get_lines(f2)
        self.assertIn(self.file_path, cache.cache)
        self.assertIn(f2, cache.cache)
        self.assertEqual(cache.current_size_bytes, 19)

        # Create a third file (2 bytes) to exceed 20 bytes threshold
        f3 = os.path.join(self.temp_dir.name, "test3.txt")
        with open(f3, "w", newline="\n", encoding="utf-8") as f:
            f.write("12")

        cache.get_lines(f3)
        # Total would be 14 + 5 + 2 = 21 bytes.
        # First file (14 bytes) must be evicted, leaving f2 and f3 (5 + 2 = 7 bytes)
        self.assertNotIn(self.file_path, cache.cache)
        self.assertIn(f2, cache.cache)
        self.assertIn(f3, cache.cache)
        self.assertEqual(cache.current_size_bytes, 7)

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

