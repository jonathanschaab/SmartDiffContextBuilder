import os
from collections import OrderedDict

class LRUFileCache:
    def __init__(self, capacity):
        self.cache = OrderedDict()
        self.capacity = capacity

    def _load(self, file_path):
        if file_path in self.cache:
            self.cache.move_to_end(file_path)
            return self.cache[file_path]
            
        if not os.path.exists(file_path):
            return {'lines': [], 'content': '', 'bytes': b''}
            
        try:
            with open(file_path, 'rb') as f:
                bytes_content = f.read()
        except IOError:
            return {'lines': [], 'content': '', 'bytes': b''}
            
        content = bytes_content.decode('utf-8', errors='ignore')
        lines = content.splitlines(keepends=True)
        
        entry = {'lines': lines, 'content': content, 'bytes': bytes_content}
        self.cache[file_path] = entry
        self.cache.move_to_end(file_path)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return entry

    def get_lines(self, file_path):
        return self._load(file_path)['lines']

    def get_content(self, file_path):
        return self._load(file_path)['content']

    def get_bytes(self, file_path):
        return self._load(file_path)['bytes']

# Singleton cache instance
DEFAULT_CACHE = None

def get_global_cache(capacity=100):
    global DEFAULT_CACHE
    if DEFAULT_CACHE is None:
        DEFAULT_CACHE = LRUFileCache(capacity)
    return DEFAULT_CACHE
