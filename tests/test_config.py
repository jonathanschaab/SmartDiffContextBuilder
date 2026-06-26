# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,consider-using-with
# pylint: disable=unspecified-encoding

import os
import tempfile
import unittest
from context_builder.config import (
    CONFIG,
    reset_config,
    load_json_with_comments,
    generate_commented_config,
)

class TestConfig(unittest.TestCase):
    def test_common_cpp_extensions_share_cpp_defaults(self):
        """Common C++ suffixes receive the same parser and query defaults."""
        from context_builder.config import (
            DEFAULT_BINDINGS,
            DEFAULT_CALLEE_QUERY_STRINGS,
            DEFAULT_DEPENDENCY_QUERY_STRINGS,
            DEFAULT_LANG_MAP,
        )

        for extension in (".cc", ".cxx", ".hxx"):
            self.assertEqual(DEFAULT_LANG_MAP[extension], "cpp")
            self.assertEqual(
                DEFAULT_BINDINGS[extension],
                ("tree_sitter_cpp", "language"),
            )
            self.assertEqual(
                DEFAULT_DEPENDENCY_QUERY_STRINGS[extension],
                DEFAULT_DEPENDENCY_QUERY_STRINGS[".cpp"],
            )
            self.assertEqual(
                DEFAULT_CALLEE_QUERY_STRINGS[extension],
                DEFAULT_CALLEE_QUERY_STRINGS[".cpp"],
            )

    def setUp(self):
        reset_config()

    def tearDown(self):
        reset_config()

    def test_load_json_with_comments(self):
        jsonc_content = """
        // This is a comment
        {
            # Another comment style
            "format": "json",
            "max_lines": 500
        }
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write(jsonc_content)
            temp_path = f.name

        try:
            cfg = load_json_with_comments(temp_path)
            self.assertEqual(cfg["format"], "json")
            self.assertEqual(cfg["max_lines"], 500)
        finally:
            os.remove(temp_path)

    def test_reset_config(self):
        CONFIG["format"] = "html"
        reset_config()
        self.assertEqual(CONFIG["format"], "md")
        self.assertEqual(CONFIG["lsp_init_timeout"], 60)
        self.assertEqual(CONFIG["lsp_timeout"], 150)
        self.assertEqual(CONFIG["git_timeout"], 30.0)
        self.assertEqual(CONFIG["git_probe_timeout"], 5.0)
        self.assertEqual(CONFIG["fallback_strip_lookahead"], 20)

    def test_generate_commented_config(self):
        reset_config()
        active = ["format", "max_lines"]
        config_str = generate_commented_config(active)

        # Check active fields are uncommented
        self.assertIn('"format": "md"', config_str)
        self.assertIn('"max_lines": 1500', config_str)

        # Check inactive fields are commented out
        self.assertIn('// "max_mb": 2.0', config_str)
        self.assertIn('// "base_name": "SmartDiffContextBuilder"', config_str)
        self.assertIn('// "fallback_strip_lookahead": 20', config_str)

    def test_config_dict_proxy(self):
        from context_builder.config import ConfigDictProxy
        proxy = ConfigDictProxy("lang_map")

        # Test basic MutableMapping functionality
        self.assertEqual(proxy[".py"], "python")

        # Test pop and setdefault (which would bypass dict subclasses if not delegated properly)
        val = proxy.pop(".py")
        self.assertEqual(val, "python")
        self.assertNotIn(".py", proxy)

        default_val = proxy.setdefault(".py", "python_new")
        self.assertEqual(default_val, "python_new")
        self.assertEqual(proxy[".py"], "python_new")

        # Test update
        proxy.update({".py": "python"})
        self.assertEqual(proxy[".py"], "python")
