# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring,protected-access

import unittest
from context_builder.volume_manager import VolumeManager


class TestVolumeManagerLowercase(unittest.TestCase):

    def test_uppercase_extensions_map_to_correct_language(self):
        vm = VolumeManager(fmt="markdown", max_lines=1000, max_mb=10.0)

        # 1. Modified core logic with .PY extension
        vm.add_modified_object("foo.PY", "my_func", "def my_func():\n    pass")

        # 2. Data and state with .CPP extension
        vm.add_data_state("data.CPP", 10, "int x = 42;")

        # 3. Downstream callee with .GO extension
        vm.local_callees.append({
            "file": "callee.GO",
            "function_name": "helper",
            "code": "func helper() {}",
            "distance": 1
        })

        # 4. Unit test with .JS extension
        vm.unit_tests.append({
            "file": "test.JS",
            "line": 5,
            "code": "describe('test', () => {});"
        })

        payload, ext = vm._flush_markdown_payload()

        self.assertEqual(ext, "md")
        # Check that .PY mapped to python
        self.assertIn("```python\ndef my_func():\n    pass\n```", payload)
        # Check that .CPP mapped to cpp
        self.assertIn("```cpp\nint x = 42;\n```", payload)
        # Check that .GO mapped to go
        self.assertIn("```go\nfunc helper() {}\n```", payload)
        # Check that .JS mapped to javascript
        self.assertIn("```javascript\ndescribe('test', () => {});\n```", payload)
