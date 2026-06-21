"""Module cache provides a Least Recently Used (LRU) file cache for text and bytes contents.

This prevents redundant disk reads and speeds up processing across multiple passes.
"""

import os
import sys
from collections import OrderedDict

try:
    _EMPTY_STR_SIZE = sys.getsizeof("")
except (AttributeError, NameError, NotImplementedError):
    _EMPTY_STR_SIZE = 49  # Fallback for non-CPython runtimes, which bypass it anyway.


class LRUFileCache:
    """A Least Recently Used (LRU) file cache for storing file lines, contents, and bytes."""

    def __init__(self, max_size_mb=200.0, capacity=None):
        """Initialize the LRU cache with a specific limit in MB.

        Args:
            max_size_mb (float): The maximum cumulative memory footprint in MB. Defaults to 200.0.
            capacity (float): Deprecated alias for max_size_mb. Used for
                backward compatibility.
        """
        self.cache = OrderedDict()
        limit = capacity if capacity is not None else max_size_mb
        if limit is None or limit <= 0:
            limit = 200.0
        self.max_size_bytes = int(limit * 1024 * 1024)
        self.current_size_bytes = 0

    def _get_entry_memory_usage(self, bytes_content, content, lines):
        """Calculate the estimated deep memory usage of a cache entry in bytes.

        Args:
            bytes_content (bytes): Raw bytes content of the file.
            content (str): Decoded string content of the file.
            lines (list): List of lines in the file.

        Returns:
            int: Estimated memory footprint in bytes.
        """
        try:
            # On non-CPython runtimes (e.g. PyPy), sys.getsizeof is not reliable.
            # Fall back to a standard multiplier heuristic.
            if sys.implementation.name != "cpython":
                return int(len(bytes_content) * 4.5)

            # Estimate lines list size in O(1) time:
            # - List object base + pointer array overhead: sys.getsizeof(lines)
            # - Line strings overhead: (len(lines) - 1) * _EMPTY_STR_SIZE + sys.getsizeof(content)
            # This is 100% exact for ASCII strings on all Python platforms/versions.
            line_strings_size = (
                (len(lines) - 1) * _EMPTY_STR_SIZE + sys.getsizeof(content)
                if lines
                else 0
            )
            estimated_lines_size = sys.getsizeof(lines) + line_strings_size
            return (
                sys.getsizeof(bytes_content)
                + sys.getsizeof(content)
                + estimated_lines_size
                + 150  # Estimating entry dict structure overhead
            )
        except Exception:  # pylint: disable=broad-except
            return int(len(bytes_content) * 4.5)

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

        size_bytes = self._get_entry_memory_usage(bytes_content, content, lines)
        entry = {
            "lines": lines,
            "content": content,
            "bytes": bytes_content,
            "size_bytes": size_bytes,
        }
        self.cache[file_path] = entry
        self.cache.move_to_end(file_path)
        self.current_size_bytes += size_bytes
        self.evict_to_limit()
        return entry

    def evict_to_limit(self):
        """Evict oldest cache entries if total memory footprint exceeds the threshold."""
        while self.cache and self.current_size_bytes > self.max_size_bytes:
            _, popped_entry = self.cache.popitem(last=False)
            self.current_size_bytes -= popped_entry.get(
                "size_bytes", len(popped_entry["bytes"])
            )

    def resize(self, max_size_mb):
        """Resize the cache limit in MB, performing validation and immediate evictions.

        Args:
            max_size_mb (float): The new maximum cumulative memory footprint in MB.
        """
        if max_size_mb is None or max_size_mb <= 0:
            max_size_mb = 200.0
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        self.evict_to_limit()

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

    def get_stripped_content(self, file_path, profile):
        """Retrieve block-comment-stripped content of the file, caching the result.

        Args:
            file_path (str): Path to the file.
            profile (LanguageProfile): The language profile of the file.

        Returns:
            str: Block-comment-stripped string content.
        """
        entry = self._load(file_path)
        if "stripped_content" not in entry:
            stripped = profile.strip_block_comments(entry["content"])
            entry["stripped_content"] = stripped
            try:
                if sys.implementation.name != "cpython":
                    added_bytes = len(stripped)
                else:
                    added_bytes = sys.getsizeof(stripped)
            except Exception:  # pylint: disable=broad-except
                added_bytes = len(stripped)
            entry["size_bytes"] += added_bytes
            if file_path in self.cache:
                self.current_size_bytes += added_bytes
                self.evict_to_limit()
        return entry["stripped_content"]

    def get_stripped_lines(self, file_path, profile):
        """Retrieve block-comment-stripped lines of the file, caching the result.

        Args:
            file_path (str): Path to the file.
            profile (LanguageProfile): The language profile of the file.

        Returns:
            list: List of stripped lines in the file.
        """
        entry = self._load(file_path)
        if "stripped_lines" not in entry:
            stripped_content = self.get_stripped_content(file_path, profile)
            stripped_lines = stripped_content.splitlines(keepends=True)
            entry["stripped_lines"] = stripped_lines
            try:
                if sys.implementation.name != "cpython":
                    added_bytes = len(stripped_content) * 2
                else:
                    line_strings_size = (
                        (len(stripped_lines) - 1) * _EMPTY_STR_SIZE
                        + sys.getsizeof(stripped_content)
                        if stripped_lines
                        else 0
                    )
                    added_bytes = sys.getsizeof(stripped_lines) + line_strings_size
            except Exception:  # pylint: disable=broad-except
                added_bytes = len(stripped_content) * 2
            entry["size_bytes"] += added_bytes
            if file_path in self.cache:
                self.current_size_bytes += added_bytes
                self.evict_to_limit()
        return entry["stripped_lines"]


# Global dictionary holder to avoid 'global' keyword warning in get_global_cache
_CACHE_HOLDER = {}


def get_global_cache(max_size_mb=None):
    """Get or create the global LRUFileCache singleton.

    Args:
        max_size_mb (float): The maximum cumulative memory footprint in MB.
            Defaults to None, falling back internally to 200.

    Returns:
        LRUFileCache: The singleton file cache instance.
    """
    if "default" not in _CACHE_HOLDER:
        limit = max_size_mb if max_size_mb is not None else 200.0
        _CACHE_HOLDER["default"] = LRUFileCache(max_size_mb=limit)
    elif max_size_mb is not None:
        _CACHE_HOLDER["default"].resize(max_size_mb)
    return _CACHE_HOLDER["default"]
