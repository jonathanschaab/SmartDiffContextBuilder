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
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
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

    def test_generate_commented_config(self):
        reset_config()
        active = ["format", "max_lines"]
        config_str = generate_commented_config(active)
        
        # Check active fields are uncommented
        self.assertIn('"format": "md"', config_str)
        self.assertIn('"max_lines": 1500', config_str)
        
        # Check inactive fields are commented out
        self.assertIn('// "max_mb": 2.0', config_str)
        self.assertIn('// "base_name": "ContextLens"', config_str)
