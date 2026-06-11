# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,consider-using-with,line-too-long

import os
import unittest
import tempfile
from context_builder.volume_manager import VolumeManager

class TestVolumeManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_volume_manager_categorization(self):
        vm = VolumeManager(fmt="md", max_lines=100, max_mb=1.0, base_name="test_payload")
        vm.set_raw_diff("some_diff")

        vm.add_modified_object("file.py", "my_func", "def my_func():\n    pass")
        self.assertEqual(len(vm.modified_objects), 1)

        vm.add_callers(vm.local_callers, {"caller.py": [{"line": 10, "code": "my_func()"}]}, "Lexical Dependency")
        self.assertEqual(len(vm.local_callers), 1)
        self.assertEqual(vm.local_callers[0]["file"], "caller.py")
        self.assertEqual(vm.local_callers[0]["line"], 10)

    def test_volume_manager_size_truncation(self):
        # 100 bytes limit
        vm = VolumeManager(fmt="md", max_lines=100, max_mb=0.0001, base_name="test_payload")
        vm.set_raw_diff("A" * 200) # Diff itself exceeds the limit

        vm.add_modified_object("file.py", "my_func", "def my_func():\n    pass")

        vm.flush_all_volumes()

        # Check generated file
        file_path = "test_payload_final.md"
        self.assertTrue(os.path.exists(file_path))
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The modified core logic should be missing because it was truncated due to size limit
        self.assertIn("Payload truncated", content)
        self.assertNotIn("Modified Core Logic", content)

    def test_volume_manager_distance_sorting(self):
        vm = VolumeManager(fmt="md", max_lines=100, max_mb=1.0, base_name="test_payload")
        vm.set_raw_diff("some_diff")

        # Add callers with distance 2 and 1
        vm.add_callers(vm.local_callers, {"caller2.py": [{"line": 20, "code": "func2()"}]}, "Lexical Dependency", distance=2)
        vm.add_callers(vm.local_callers, {"caller1.py": [{"line": 10, "code": "func1()"}]}, "Lexical Dependency", distance=1)

        # Add FFI linkages with distance 2 and 1
        vm.add_callers(vm.ffi_linkages, {"ffi2.py": [{"line": 20, "code": "ffi2()"}]}, "FFI Linkage", distance=2)
        vm.add_callers(vm.ffi_linkages, {"ffi1.py": [{"line": 10, "code": "ffi1()"}]}, "FFI Linkage", distance=1)

        # Flush
        vm.flush_all_volumes()

        # Verify internal sorting order
        self.assertEqual(vm.local_callers[0]["file"], "caller1.py")
        self.assertEqual(vm.local_callers[1]["file"], "caller2.py")
        self.assertEqual(vm.ffi_linkages[0]["file"], "ffi1.py")
        self.assertEqual(vm.ffi_linkages[1]["file"], "ffi2.py")

        # Verify the generated file's content shows distance and correct order
        file_path = "test_payload_final.md"
        self.assertTrue(os.path.exists(file_path))
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify formatting
        self.assertIn("- `caller1.py` (L10, Distance 1)", content)
        self.assertIn("- `caller2.py` (L20, Distance 2)", content)
        self.assertIn("- `ffi1.py` (L10, Distance 1)", content)
        self.assertIn("- `ffi2.py` (L20, Distance 2)", content)

        # Verify caller1.py appears before caller2.py in the formatted text
        idx1 = content.find("caller1.py")
        idx2 = content.find("caller2.py")
        self.assertTrue(idx1 < idx2)

# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,consider-using-with,line-too-long
