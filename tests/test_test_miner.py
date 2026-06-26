# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,consider-using-with,line-too-long
# pylint: disable=protected-access

import os
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from context_builder.config import reset_config
from context_builder.cache import LRUFileCache
from context_builder import test_miner
from context_builder.test_miner import get_coverage_data, mine_relevant_unit_tests

class TestTestMiner(unittest.TestCase):
    def setUp(self):
        reset_config()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()
        reset_config()

    def test_get_coverage_data(self):
        # Create dummy coverage.xml
        xml_content = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.5">\n'
            '  <packages>\n'
            '    <package name="src">\n'
            '      <classes>\n'
            '        <class name="a.py" filename="src/a.py">\n'
            '          <lines>\n'
            '            <line number="5" hits="1"/>\n'
            '            <line number="10" hits="0"/>\n'
            '          </lines>\n'
            '        </class>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        with open("coverage.xml", "w", encoding="utf-8") as f:
            f.write(xml_content)

        cov = get_coverage_data()
        self.assertIn("src/a.py", cov)
        self.assertEqual(cov["src/a.py"], [5])

    def test_get_coverage_data_in_build_directory(self):
        # Create a build directory
        os.makedirs("build", exist_ok=True)
        # Create dummy coverage.xml in build directory
        xml_content = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.5">\n'
            '  <packages>\n'
            '    <package name="src">\n'
            '      <classes>\n'
            '        <class name="a.py" filename="src/a.py">\n'
            '          <lines>\n'
            '            <line number="5" hits="1"/>\n'
            '            <line number="10" hits="0"/>\n'
            '          </lines>\n'
            '        </class>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        with open(os.path.join("build", "coverage.xml"), "w", encoding="utf-8") as f:
            f.write(xml_content)

        cov = get_coverage_data()
        self.assertIn("src/a.py", cov)
        self.assertEqual(cov["src/a.py"], [5])

    def test_get_coverage_data_in_build_directory_newest_mtime(self):
        # Create build and out directories
        os.makedirs("build", exist_ok=True)
        os.makedirs("out", exist_ok=True)

        xml_build = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.5">\n'
            '  <packages>\n'
            '    <package name="src">\n'
            '      <classes>\n'
            '        <class name="a.py" filename="src/a.py">\n'
            '          <lines>\n'
            '            <line number="5" hits="1"/>\n'
            '          </lines>\n'
            '        </class>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        xml_out = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.5">\n'
            '  <packages>\n'
            '    <package name="src">\n'
            '      <classes>\n'
            '        <class name="a.py" filename="src/a.py">\n'
            '          <lines>\n'
            '            <line number="15" hits="1"/>\n'
            '          </lines>\n'
            '        </class>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        build_path = os.path.join("build", "coverage.xml")
        out_path = os.path.join("out", "coverage.xml")

        with open(build_path, "w", encoding="utf-8") as f:
            f.write(xml_build)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml_out)

        # Make out/coverage.xml newer than build/coverage.xml
        os.utime(build_path, (1000.0, 1000.0))
        os.utime(out_path, (2000.0, 2000.0))

        # Should parse coverage.xml from out/ (newer mtime)
        cov = get_coverage_data()
        self.assertIn("src/a.py", cov)
        self.assertEqual(cov["src/a.py"], [15])

    def test_mine_relevant_unit_tests_regex(self):
        # Test regex-based unit test mining
        test_code = (
            "def test_my_func():\n"
            "    # test body\n"
            "    my_func()\n"
        )
        file_path = "test_a.py"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(test_code)

        cache = LRUFileCache(capacity=5)
        # Seed cache
        cache.get_content(file_path)

        tests = mine_relevant_unit_tests("my_func", [file_path], file_cache=cache)
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0]["file"], file_path)
        self.assertEqual(tests[0]["line"], 1)

    def test_mine_relevant_unit_tests_regex_substring_avoidance(self):
        # Create a test file containing "test_runner" but we are looking for "run"
        test_code = (
            "def test_runner():\n"
            "    # test body\n"
            "    runner()\n"
        )
        file_path = "test_a.py"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(test_code)

        cache = LRUFileCache(capacity=5)
        # Seed cache
        cache.get_content(file_path)

        # We look for "run". It should NOT match "test_runner" or "runner()" because of the word boundaries
        tests = mine_relevant_unit_tests("run", [file_path], file_cache=cache)
        self.assertEqual(len(tests), 0)

    def test_coverage_data_separator_normalization(self):
        # Create a coverage.xml with backslashes in filename
        xml_content = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.5">\n'
            '  <packages>\n'
            '    <package name="src">\n'
            '      <classes>\n'
            '        <class name="a.py" filename="src\\a.py">\n'
            '          <lines>\n'
            '            <line number="5" hits="1"/>\n'
            '          </lines>\n'
            '        </class>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        with open("coverage.xml", "w", encoding="utf-8") as f:
            f.write(xml_content)

        cov = get_coverage_data()
        self.assertIn("src/a.py", cov)
        self.assertEqual(cov["src/a.py"], [5])

    @patch("context_builder.test_miner.warn_once")
    def test_get_coverage_data_warns_for_malformed_xml(self, mock_warn):
        with open("coverage.xml", "w", encoding="utf-8") as coverage_file:
            coverage_file.write("<coverage>")

        self.assertEqual(get_coverage_data(), {})
        mock_warn.assert_called_once()
        self.assertEqual(mock_warn.call_args.args[0], "coverage_xml_parse_fail")

    def test_process_ast_capture_deduplicates_function_body(self):
        source = b"fn test_target() {\n    target();\n}\n"
        function_node = SimpleNamespace(
            type="function_item",
            parent=None,
            start_byte=0,
            end_byte=len(source),
            start_point=(0, 0),
            end_point=(2, 1),
        )
        capture = SimpleNamespace(type="identifier", parent=function_node)
        discovered = []
        seen = set()
        pattern = test_miner.re.compile(r"\btarget\b")
        lines = source.decode("utf-8").splitlines(keepends=True)

        test_miner._process_ast_capture(
            capture, source, pattern, lines, seen, discovered, "tests.rs"
        )
        test_miner._process_ast_capture(
            capture, source, pattern, lines, seen, discovered, "tests.rs"
        )

        self.assertEqual(discovered, [{
            "file": "tests.rs",
            "line": 1,
            "code": source.decode("utf-8"),
        }])

    def test_process_ast_capture_ignores_unscoped_and_unrelated_nodes(self):
        source = b"fn helper() {}\n"
        lines = source.decode("utf-8").splitlines(keepends=True)
        discovered = []
        pattern = test_miner.re.compile(r"\btarget\b")
        unscoped = SimpleNamespace(type="identifier", parent=None)
        function_node = SimpleNamespace(
            type="function_definition",
            parent=None,
            start_byte=0,
            end_byte=len(source),
            start_point=(0, 0),
            end_point=(0, 14),
        )

        test_miner._process_ast_capture(
            unscoped, source, pattern, lines, set(), discovered, "test.py"
        )
        test_miner._process_ast_capture(
            function_node, source, pattern, lines, set(), discovered, "test.py"
        )

        self.assertEqual(discovered, [])

    @patch("context_builder.test_miner.get_language_profile")
    def test_mine_ast_tests_processes_query_captures(self, mock_profile):
        source = b"fn test_target() {\n    target();\n}\n"
        function_node = SimpleNamespace(
            type="function_item",
            parent=None,
            start_byte=0,
            end_byte=len(source),
            start_point=(0, 0),
            end_point=(2, 1),
        )
        capture = SimpleNamespace(type="identifier", parent=function_node)
        query = Mock()
        query.captures.return_value = [(capture, "name")]
        language = Mock()
        language.query.return_value = query
        parser = Mock()
        parser.parse.return_value = SimpleNamespace(root_node=object())
        mock_profile.return_value.test_query = "(function_item) @test"

        with patch("tree_sitter.Query", return_value=query) as mock_query_class, \
                patch.dict(test_miner.AST_ENGINE.parsers, {".rs": parser}), \
                patch.dict(test_miner.AST_ENGINE.languages, {".rs": language}):
            discovered = []
            success = test_miner._mine_ast_tests(
                "tests.rs",
                ".rs",
                test_miner.re.compile(r"\btarget\b"),
                source,
                source.decode("utf-8").splitlines(keepends=True),
                set(),
                discovered,
            )

        self.assertTrue(success)
        self.assertEqual(discovered[0]["line"], 1)
        mock_query_class.assert_called_once_with(language, "(function_item) @test")

    @patch("context_builder.test_miner.warn_once")
    @patch("context_builder.test_miner.get_language_profile")
    def test_mine_ast_tests_reports_query_failure(self, mock_profile, mock_warn):
        parser = Mock()
        parser.parse.return_value = SimpleNamespace(root_node=object())
        language = Mock()
        mock_profile.return_value.test_query = "(broken"

        with patch("tree_sitter.Query", side_effect=RuntimeError("bad query")), \
                patch.dict(test_miner.AST_ENGINE.parsers, {".py": parser}), \
                patch.dict(test_miner.AST_ENGINE.languages, {".py": language}):
            success = test_miner._mine_ast_tests(
                "test_bad.py", ".py", Mock(), b"", [], set(), []
            )

        self.assertFalse(success)
        mock_warn.assert_called_once()
        self.assertIn("test_bad.py", mock_warn.call_args.args[1])

    @patch("context_builder.test_miner.get_language_profile")
    def test_mine_ast_tests_propagates_memory_error(self, mock_profile):
        parser = Mock()
        parser.parse.side_effect = MemoryError("out of memory")
        language = Mock()
        mock_profile.return_value.test_query = "(function_definition) @test"

        with patch.dict(test_miner.AST_ENGINE.parsers, {".py": parser}), \
                patch.dict(test_miner.AST_ENGINE.languages, {".py": language}):
            with self.assertRaises(MemoryError):
                test_miner._mine_ast_tests(
                    "test_bad.py", ".py", Mock(), b"", [], set(), []
                )

    @patch("context_builder.test_miner._mine_regex_tests")
    @patch("context_builder.test_miner._mine_ast_tests", return_value=False)
    @patch.object(test_miner.AST_ENGINE, "is_supported", return_value=True)
    def test_mine_single_file_falls_back_when_ast_query_fails(
        self, _mock_supported, mock_ast_mine, mock_regex_mine
    ):
        file_cache = Mock()
        file_cache.get_lines.return_value = ["def test_target():\n", "    target()\n"]
        file_cache.get_bytes.return_value = b"def test_target():\n    target()\n"

        test_miner._mine_single_file(
            "test_target.py",
            test_miner.re.compile(r"\btarget\b"),
            None,
            set(),
            [],
            file_cache,
        )

        mock_ast_mine.assert_called_once()
        mock_regex_mine.assert_called_once()

    @patch("context_builder.test_miner._mine_regex_tests")
    def test_mine_single_file_skips_non_test_files(self, mock_regex_mine):
        test_miner._mine_single_file(
            "production.py", Mock(), None, set(), [], Mock()
        )

        mock_regex_mine.assert_not_called()

    @patch("context_builder.test_miner._mine_single_file")
    @patch("context_builder.test_miner.iter_scan_progress", side_effect=lambda files, **_: files)
    @patch("context_builder.test_miner.ripgrep_filter", return_value=[])
    def test_current_rust_source_is_scanned_without_ripgrep_match(
        self, _mock_filter, _mock_progress, mock_mine
    ):
        cache = Mock()

        result = mine_relevant_unit_tests(
            "target", ["lib.rs"], current_source_file="lib.rs", file_cache=cache
        )

        self.assertEqual(result, [])
        mock_mine.assert_called_once()
        self.assertEqual(mock_mine.call_args.args[0], "lib.rs")

    @patch("context_builder.test_miner.ripgrep_filter")
    def test_short_function_name_avoids_repository_scan(self, mock_filter):
        self.assertEqual(mine_relevant_unit_tests("id", ["test_a.py"]), [])
        mock_filter.assert_not_called()

    def test_mine_relevant_unit_tests_descriptive_suffixes(self):
        # Test that functions with descriptive suffixes like test_greet_behavior,
        # test_greet_with_name, or greet_empty are matched, but substring-similar
        # functions like test_runner are not.
        test_code = (
            "def test_greet_behavior():\n"
            "    greet()\n"
            "\n"
            "def test_greet_with_name():\n"
            "    greet()\n"
            "\n"
            "def test_greeter():\n"
            "    greeter()\n"
        )
        file_path = "test_descriptive.py"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(test_code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        tests = mine_relevant_unit_tests("greet", [file_path], file_cache=cache)
        # Should match test_greet_behavior and test_greet_with_name, but NOT test_greeter
        self.assertEqual(len(tests), 2)
        test_names = [t["code"].split("(")[0].strip() for t in tests]
        self.assertIn("def test_greet_behavior", test_names)
        self.assertIn("def test_greet_with_name", test_names)
        self.assertNotIn("def test_greeter", test_names)

    def test_mine_relevant_unit_tests_operator(self):
        """Verify that mine_relevant_unit_tests correctly mines tests for functions with
        non-word boundaries (like C++ operator overloads operator+)."""
        test_code = (
            "it('should test operator+', () => {\n"
            "    obj1.operator+(obj2);\n"
            "});\n"
            "\n"
            "it('should test other', () => {\n"
            "    other();\n"
            "});\n"
        )
        file_path = "test_ops.js"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(test_code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        tests = mine_relevant_unit_tests("operator+", [file_path], file_cache=cache)
        # Should match the JS test that has both 'it(' and 'operator+' on the first line
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0]["file"], file_path)
