"""Unit tests covering dotted imports, UTF-8 BOM, config dictionary exceptions,
and FFI None capture guards.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from context_builder.config import CONFIG, reset_config


class TestSafetyRefactors(unittest.TestCase):
    """Test suite for security, correctness, and reliability refactors."""

    def setUp(self):
        reset_config()

    def tearDown(self):
        reset_config()

    @patch("importlib.import_module")
    def test_dotted_module_import(self, mock_import_module):
        """Verify that dynamic imports can handle dotted module names using importlib."""
        from context_builder.ast_engine import HAS_TREESITTER, AstEngine

        if not HAS_TREESITTER:
            self.skipTest("tree-sitter not installed")

        mock_module = MagicMock()
        mock_import_module.return_value = mock_module
        mock_lang = MagicMock()
        setattr(mock_module, "dummy", mock_lang)

        engine = AstEngine()
        orig_bindings = CONFIG["bindings"].copy()
        try:
            CONFIG["bindings"] = {".dummy": ("pkg.sub", "dummy")}
            engine.initialize()
            mock_import_module.assert_called_with("pkg.sub")
            self.assertIn(".dummy", engine.languages)
        finally:
            CONFIG["bindings"] = orig_bindings

    def test_utf8_bom_support(self):
        """Verify that UTF-8 BOM signature is successfully stripped from config files."""
        from context_builder.config import load_json_with_comments

        jsonc_content = "\ufeff// UTF-8 BOM comment\n{\n  \"format\": \"md\"\n}\n"

        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(jsonc_content.encode("utf-8"))
            temp_path = f.name

        try:
            cfg = load_json_with_comments(temp_path)
            self.assertEqual(cfg["format"], "md")
        finally:
            os.remove(temp_path)

    def test_config_non_dict_structure(self):
        """Verify that a non-dict config structure causes a graceful error and exits."""
        from context_builder.cli import main

        json_content = '["invalid", "list"]'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json_content)
            temp_path = f.name

        try:
            with patch("sys.argv", ["smart_diff_context_builder.py", "--config", temp_path]):
                with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                    with self.assertRaises(SystemExit):
                        main()
                    mock_exit.assert_called_with(1)
        finally:
            os.remove(temp_path)

    def test_config_non_dict_override(self):
        """Verify passing non-dict override to dict config key raises an error and exits.
        """
        from context_builder.cli import main

        json_content = '{"lang_map": "not_a_dictionary"}'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json_content)
            temp_path = f.name

        try:
            with patch("sys.argv", ["smart_diff_context_builder.py", "--config", temp_path]):
                with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                    with self.assertRaises(SystemExit):
                        main()
                    mock_exit.assert_called_with(1)
        finally:
            os.remove(temp_path)

    def test_cli_non_dict_override(self):
        """Verify that passing a non-dict CLI override to a dict key raises an error and exits."""
        from context_builder.cli import main

        with patch("sys.argv", ["smart_diff_context_builder.py", "--lang-map", '"not_a_dict"']):
            with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                with self.assertRaises(SystemExit):
                    main()
                mock_exit.assert_called_with(1)

    def test_ffi_optional_capture_none(self):
        """Verify that FFI optional capture groups that return None do not crash and are skipped."""
        from context_builder.cache import get_global_cache
        from context_builder.preprocessor import build_ffi_registry

        orig_patterns = CONFIG.get("ffi_patterns")
        # Group 1 is optional: (none_group)?my_symbol
        CONFIG["ffi_patterns"] = [r"(none_group)?(my_symbol)"]

        with tempfile.TemporaryDirectory() as temp_dir_path:
            file_path = os.path.join(temp_dir_path, "source.c")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("my_symbol\n")

            cache = get_global_cache()
            cache.get_content(file_path)

            try:
                symbols = build_ffi_registry([file_path], file_cache=cache)
                self.assertNotIn(None, symbols)
                self.assertNotIn("my_symbol", symbols)
            finally:
                if orig_patterns is not None:
                    CONFIG["ffi_patterns"] = orig_patterns
