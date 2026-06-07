import os
import unittest
import tempfile
from context_builder.cache import LRUFileCache
from context_builder.test_miner import get_coverage_data, mine_relevant_unit_tests

class TestTestMiner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

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
