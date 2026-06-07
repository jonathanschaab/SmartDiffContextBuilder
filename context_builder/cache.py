"""Module cache provides a Least Recently Used (LRU) file cache for text and bytes contents.

This prevents redundant disk reads and speeds up processing across multiple passes.
"""

import os
from collections import OrderedDict


class LRUFileCache:
    """A Least Recently Used (LRU) file cache for storing file lines, contents, and bytes."""

    def __init__(self, capacity):
        """Initialize the LRU cache with a specific capacity limit.

        Args:
            capacity (int): The maximum number of files to cache.
        """
        self.cache = OrderedDict()
        self.capacity = capacity

    def _load(self, file_path):
        """Load the file from disk if not cached, and move it to the end of the LRU.

        Args:
            file_path (str): Path to the file.

        Returns:
            dict: Cache entry dictionary with 'lines', 'content', and 'bytes'.
        """
        if file_path in self.cache:
            self.cache.move_to_end(file_path)
            return self.cache[file_path]

        if not os.path.exists(file_path):
            return {"lines": [], "content": "", "bytes": b""}

        try:
            with open(file_path, "rb") as f:
                bytes_content = f.read()
        except IOError:
            return {"lines": [], "content": "", "bytes": b""}

        content = bytes_content.decode("utf-8", errors="ignore")
        lines = content.splitlines(keepends=True)

        entry = {"lines": lines, "content": content, "bytes": bytes_content}
        self.cache[file_path] = entry
        self.cache.move_to_end(file_path)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return entry

    def get_lines(self, file_path):
        """Retrieve lines of the file.

        Args:
            file_path (str): Path to the file.

        Returns:
            list: List of lines in the file.
        """
        return self._load(file_path)["lines"]

    def get_content(self, file_path):
        """Retrieve full string content of the file.

        Args:
            file_path (str): Path to the file.

        Returns:
            str: Full decoded string content.
        """
        return self._load(file_path)["content"]

    def get_bytes(self, file_path):
        """Retrieve raw bytes of the file.

        Args:
            file_path (str): Path to the file.

        Returns:
            bytes: Raw bytes content of the file.
        """
        return self._load(file_path)["bytes"]


# Global dictionary holder to avoid 'global' keyword warning in get_global_cache
_CACHE_HOLDER = {}


def get_global_cache(capacity=100):
    """Get or create the global LRUFileCache singleton.

    Args:
        capacity (int): The capacity of the cache. Defaults to 100.

    Returns:
        LRUFileCache: The singleton file cache instance.
    """
    if "default" not in _CACHE_HOLDER:
        _CACHE_HOLDER["default"] = LRUFileCache(capacity)
    return _CACHE_HOLDER["default"]
