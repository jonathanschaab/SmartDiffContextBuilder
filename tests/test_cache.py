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
        cache = LRUFileCache(capacity=2)
        
        # Load first file
        lines1 = cache.get_lines(self.file_path)
        self.assertEqual(lines1, ["line 1\n", "line 2\n"])
        self.assertIn(self.file_path, cache.cache)

        # Create more files to force eviction
        f2 = os.path.join(self.temp_dir.name, "test2.txt")
        f3 = os.path.join(self.temp_dir.name, "test3.txt")
        with open(f2, "w", newline="\n", encoding="utf-8") as f: f.write("2")
        with open(f3, "w", newline="\n", encoding="utf-8") as f: f.write("3")

        cache.get_lines(f2)
        cache.get_lines(f3)

        # First file should be evicted
        self.assertNotIn(self.file_path, cache.cache)
        self.assertIn(f2, cache.cache)
        self.assertIn(f3, cache.cache)

    def test_get_content_and_bytes(self):
        cache = LRUFileCache(capacity=5)
        content = cache.get_content(self.file_path)
        self.assertEqual(content, "line 1\nline 2\n")
        
        bytes_val = cache.get_bytes(self.file_path)
        self.assertEqual(bytes_val, b"line 1\nline 2\n")

    def test_nonexistent_file(self):
        cache = LRUFileCache(capacity=5)
        bad_path = os.path.join(self.temp_dir.name, "doesnotexist.txt")
        self.assertEqual(cache.get_lines(bad_path), [])
        self.assertEqual(cache.get_content(bad_path), "")
        self.assertEqual(cache.get_bytes(bad_path), b"")
