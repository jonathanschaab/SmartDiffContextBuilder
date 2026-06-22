# pylint: disable=too-many-public-methods

"""Tests for language profile resolution and fallback behavior."""

import unittest

from context_builder.config import CONFIG
from context_builder.languages import UNKNOWN_LANGUAGE, get_language_profile
from context_builder.languages.base import LanguageProfile


class TestLanguageProfiles(unittest.TestCase):
    """Verify language-specific policy stays behind the registry boundary."""

    def test_python_profile(self):
        """Python uses indentation, hash comments, and pylsp."""
        profile = get_language_profile("src/example.py")

        self.assertEqual(profile.name, "python")
        self.assertTrue(profile.uses_indentation_blocks)
        self.assertEqual(profile.comment_prefix, "#")
        self.assertEqual(profile.lsp_command, ("pylsp",))
        self.assertEqual(
            profile.strip_strings_and_comments("def foo(): # comment"),
            "def foo(): ",
        )
        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "# ... [Body Omitted] ...",
        )

    def test_python_multiline_string_stripping(self):
        """Python profile correctly strips single-line and multiline triple-quoted strings."""
        profile = get_language_profile("src/example.py")

        # Single-line triple-quoted string
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table"""'),
            "query = ",
        )
        # Single-line triple-quoted string with single quotes
        self.assertEqual(
            profile.strip_strings_and_comments("query = '''SELECT * FROM table'''"),
            "query = ",
        )
        # Triple-quoted string starting line (unclosed fallback)
        # Strips start and all text following it on that line
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table'),
            "query = ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments('query = """'),
            "query = ",
        )
        # Triple-quoted string with backslash escapes (e.g. \n, \", \\)
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table\\n"""'),
            "query = ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table\\\""""'),
            "query = ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table\\\\" """'),
            "query = ",
        )
        # Triple-quoted string containing standard quotes
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM "table" """'),
            "query = ",
        )
        # Triple-quoted string with an inline comment
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table""" # comment'),
            "query =  ",
        )
        # Preserve newlines in multiline comments / strings
        content = 'query = """\nSELECT *\nFROM "table"\n"""'
        self.assertEqual(
            profile.strip_block_comments(content),
            'query = \n\n\n',
        )

        # ReDoS test: 1000 backslashes in an unclosed multiline string
        # This test ensures there is no exponential backtracking / hang
        redos_str = 'query = """' + '\\' * 1000
        self.assertEqual(
            profile.strip_strings_and_comments(redos_str),
            "query = ",
        )
        self.assertEqual(
            profile.strip_block_comments(redos_str),
            redos_str,
        )

    def test_c_family_profile(self):
        """C-family files expose preprocessing and compile database support."""
        for file_path in (
            "main.c",
            "main.cc",
            "main.cpp",
            "main.cxx",
            "main.h",
            "main.hpp",
            "main.hxx",
        ):
            profile = get_language_profile(file_path)
            self.assertEqual(profile.name, "c-family")
            self.assertTrue(profile.supports_macro_expansion)
            self.assertTrue(profile.supports_compile_commands)
            self.assertTrue(profile.uses_c_style_definitions)
            self.assertEqual(
                profile.lsp_command,
                ("clangd", "--background-index"),
            )
            self.assertEqual(
                profile.format_omission_comment("Body Omitted"),
                "/* ... [Body Omitted] ... */",
            )

    def test_c_family_boundaries(self):
        """CFamilyProfile boundaries use negative lookbehinds/lookaheads for
        destructors and identifiers.
        """
        # pylint: disable=protected-access
        profile = get_language_profile("main.cpp")

        # Test _get_boundaries for normal class name
        lead_b, trail_b = profile._get_boundaries("MyClass")
        self.assertEqual(lead_b, r'(?<![a-zA-Z0-9_])')
        self.assertEqual(trail_b, r'(?![a-zA-Z0-9_])')

        # Test _get_boundaries for destructor
        lead_b, trail_b = profile._get_boundaries("~MyClass")
        self.assertEqual(lead_b, r'(?<![a-zA-Z0-9_])')
        self.assertEqual(trail_b, r'(?![a-zA-Z0-9_])')

        # Verify call pattern matches / doesn't match boundaries
        pattern = profile.get_call_pattern("~MyClass")

        self.assertTrue(pattern.search("obj.~MyClass()"))
        self.assertTrue(pattern.search("ptr->~MyClass()"))
        self.assertTrue(pattern.search("MyClass::~MyClass()"))
        self.assertTrue(pattern.search("~MyClass()"))

        self.assertFalse(pattern.search("other_~MyClass()"))
        self.assertFalse(pattern.search("abc~MyClass()"))

    def test_known_non_c_profiles(self):
        """Other registered languages retain their comment and LSP policy."""
        self.assertEqual(
            get_language_profile("lib.rs").lsp_command,
            ("rust-analyzer",),
        )
        self.assertTrue(
            get_language_profile("lib.rs").tests_can_share_source_file
        )
        self.assertIsNotNone(get_language_profile("tests.py").test_query)
        self.assertEqual(
            get_language_profile("app.ts").lsp_command,
            ("typescript-language-server", "--stdio"),
        )
        self.assertEqual(get_language_profile("main.go").name, "go")
        self.assertEqual(get_language_profile("script.sh").comment_prefix, "#")
        self.assertEqual(get_language_profile("Makefile").comment_prefix, "#")
        self.assertEqual(
            get_language_profile("makefile-client").comment_prefix,
            "#",
        )
        self.assertEqual(get_language_profile("build.bat").comment_prefix, "REM")

    def test_batch_rem_comments_are_case_insensitive_distinct_tokens(self):
        """Batch REM comments ignore casing without matching longer words."""
        profile = get_language_profile("build.bat")

        for marker in ("REM", "rem", "Rem"):
            self.assertEqual(
                profile.strip_strings_and_comments(f"echo before & {marker} after"),
                "echo before & ",
            )
        for command in ("PREMIUM", "REMARK", "REMEDY"):
            self.assertEqual(
                profile.strip_strings_and_comments(f"echo {command}"),
                f"echo {command}",
            )
        self.assertEqual(
            profile.strip_strings_and_comments('echo "REM is text"'),
            "echo ",
        )

    def test_batch_double_colon_comments(self):
        """Batch double-colon :: comments are correctly stripped."""
        profile = get_language_profile("build.bat")

        # Basic stripping from the start
        self.assertEqual(
            profile.strip_strings_and_comments(":: this is a comment"),
            "",
        )
        # Stripping after code/commands
        self.assertEqual(
            profile.strip_strings_and_comments("echo hello & :: comment"),
            "echo hello & ",
        )
        # Double colon inside double quotes is not stripped as a comment
        self.assertEqual(
            profile.strip_strings_and_comments('echo "this is :: not a comment"'),
            "echo ",
        )
        # Mix of REM and :: strips at the earliest marker
        self.assertEqual(
            profile.strip_strings_and_comments("echo before & :: comment & rem comment2"),
            "echo before & ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("echo before & rem comment & :: comment2"),
            "echo before & ",
        )
        # Double colon as an argument should not be stripped
        self.assertEqual(
            profile.strip_strings_and_comments("echo hello :: world"),
            "echo hello :: world",
        )
        # Double colon in variable assignment should not be stripped
        self.assertEqual(
            profile.strip_strings_and_comments("set var=value::suffix"),
            "set var=value::suffix",
        )
        # Stripping after other separators like | or (
        self.assertEqual(
            profile.strip_strings_and_comments("echo hello | :: comment"),
            "echo hello | ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("(:: comment)"),
            "(",
        )
        # Double colon comments prefixed with @ should be stripped
        self.assertEqual(
            profile.strip_strings_and_comments("@:: comment"),
            "",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("echo hello & @:: comment"),
            "echo hello & ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("echo hello & @  :: comment"),
            "echo hello & ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("@:: comment & rem comment2"),
            "",
        )
        # @ prefixing a regular command should not strip double colons within arguments
        self.assertEqual(
            profile.strip_strings_and_comments("@echo hello :: world"),
            "@echo hello :: world",
        )

    def test_unknown_language_fallback(self):
        """Unknown extensions use the explicit conservative fallback profile."""
        profile = get_language_profile("source.custom")

        self.assertIs(profile, UNKNOWN_LANGUAGE)
        self.assertFalse(profile.supports_macro_expansion)
        self.assertFalse(profile.supports_compile_commands)
        self.assertIsNone(profile.lsp_command)
        self.assertEqual(profile.comment_prefix, "//")
        self.assertEqual(
            profile.strip_strings_and_comments('value("text"); // comment'),
            "value(); ",
        )
        self.assertEqual(
            profile.extract_function_name("widget()", 4, 8),
            "widget",
        )

    def test_custom_block_comment_delimiters_are_used(self):
        """Profiles can override block-comment delimiters for omission text."""
        class HtmlLikeProfile(LanguageProfile):
            """Minimal block-comment override used for omission formatting tests."""
            supports_block_comments = True
            block_comment_start = "<!--"
            block_comment_end = "-->"

        profile = HtmlLikeProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "<!-- ... [Body Omitted] ... -->",
        )

    def test_missing_comment_markers_fall_back_to_plain_text(self):
        """Profiles without comment markers omit text without rendering 'None'."""
        class MarkerlessProfile(LanguageProfile):
            """Profile with no usable comment markers for defensive formatting."""
            supports_block_comments = False
            line_comment = None
            comment_prefix = None

        profile = MarkerlessProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "... [Body Omitted] ...",
        )

    def test_missing_block_delimiters_fall_back_to_line_comments(self):
        """Block-comment profiles without delimiters reuse line comments safely."""
        class IncompleteBlockProfile(LanguageProfile):
            """Profile missing block delimiters but still declaring line comments."""
            supports_block_comments = True
            block_comment_start = None
            block_comment_end = ""
            line_comment = "#"
            comment_prefix = "#"

        profile = IncompleteBlockProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "# ... [Body Omitted] ...",
        )

    def test_missing_block_delimiters_and_line_comments_fall_back_to_plain_text(self):
        """Profiles missing all comment markers degrade to plain omission text."""
        class MarkerlessBlockProfile(LanguageProfile):
            """Profile with no valid block or line comment markers."""
            supports_block_comments = True
            block_comment_start = None
            block_comment_end = None
            line_comment = None
            comment_prefix = None

        profile = MarkerlessBlockProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "... [Body Omitted] ...",
        )

    def test_extension_lookup_is_case_insensitive(self):
        """Registry lookups normalize extension casing."""
        self.assertEqual(get_language_profile(".PY").name, "python")
        self.assertEqual(get_language_profile("HEADER.HPP").name, "c-family")

    def test_hidden_files_with_multiple_dots_use_final_extension(self):
        """Hidden filenames are resolved as paths rather than pure extensions."""
        self.assertEqual(get_language_profile(".test.py").name, "python")
        self.assertEqual(get_language_profile(".config.js").name, "javascript")

    def test_windows_paths_resolve_on_any_platform(self):
        """Backslash paths use the same language resolution on every OS."""
        self.assertEqual(
            get_language_profile(r"src\package\module.py").name,
            "python",
        )
        self.assertEqual(
            get_language_profile(r"src\build\Makefile-client").comment_prefix,
            "#",
        )
        self.assertEqual(
            get_language_profile(r"src\.config.js").name,
            "javascript",
        )

    def test_cpp_raw_string_stripping(self):
        """C++ profile correctly strips single-line and multiline raw string literals."""
        profile = get_language_profile("main.cpp")
        self.assertEqual(
            profile.strip_strings_and_comments('R"delimiter(my_func();)delimiter"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('R"(my_func();)"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('R"foo(my_func("hello");)foo"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('u8R"foo(bar)foo"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('LR"--(STUV)--"'),
            '',
        )

        # Raw string prefixes that are part of larger identifiers should not
        # be matched as raw strings
        self.assertEqual(
            profile.strip_strings_and_comments('fooR"(bar)"'),
            'fooR',
        )

        # Multiline raw string in content should be stripped by
        # strip_block_comments preserving newlines
        content = 'R"foo(\nmy_func("hello");\n)foo"'
        self.assertEqual(profile.strip_block_comments(content), '\n\n')

    def test_rust_raw_string_stripping(self):
        """Rust profile correctly strips single-line and multiline raw string literals."""
        profile = get_language_profile("lib.rs")
        self.assertEqual(
            profile.strip_strings_and_comments('r#"// my_func()"#'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('r"// my_func()"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('br#"// my_func()"#'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('r##"my_func("hello")"##'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('cr"foo"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('cr#"foo"#'),
            '',
        )

        # Raw string prefixes that are part of larger identifiers should not
        # be matched as raw strings
        self.assertEqual(
            profile.strip_strings_and_comments('bar"hello"'),
            'bar',
        )

        # Multiline raw string in content should be stripped by
        # strip_block_comments preserving newlines
        content = 'r#"\nline 1\nline 2"#'
        self.assertEqual(profile.strip_block_comments(content), '\n\n')

    def test_rust_lifetimes_and_character_literals(self):
        """Rust lifetimes are preserved and valid char literals are stripped."""
        profile = get_language_profile("lib.rs")

        # Test character literals are correctly stripped
        self.assertEqual(profile.strip_strings_and_comments("'a'"), "")
        self.assertEqual(profile.strip_strings_and_comments("'\\n'"), "")
        self.assertEqual(profile.strip_strings_and_comments("'\\u{1f600}'"), "")

        # Test lifetimes are preserved
        self.assertEqual(
            profile.strip_strings_and_comments("fn foo<'a, 'b>()"),
            "fn foo<'a, 'b>()",
        )

        # Test block comments on the same line as multiple lifetimes are stripped
        content = "fn foo<'a /* comment */, 'b>()"
        self.assertEqual(
            profile.strip_block_comments(content),
            "fn foo<'a , 'b>()",
        )

    def test_rust_nested_block_comments(self):
        """Rust profile correctly handles nested block comments."""
        profile = get_language_profile("lib.rs")

        # Test single-line nested block comments in strip_strings_and_comments
        self.assertEqual(
            profile.strip_strings_and_comments("/* Outer /* Inner */ fn false_positive() {} */"),
            "",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("fn active() {} /* Outer /* Inner */ */"),
            "fn active() {} ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("/* Outer /* Inner */ */ fn active() {}"),
            " fn active() {}",
        )

        # Test multiline nested block comments in strip_block_comments
        content = (
            "fn active_1() {}\n"
            "/* Outer\n"
            "  /* Inner\n"
            "  */\n"
            "  fn inactive() {}\n"
            "*/\n"
            "fn active_2() {}"
        )
        # 5 lines are inside the block comment, so they should be replaced by 5 newlines
        expected = (
            "fn active_1() {}\n"
            "\n\n\n\n\n"
            "fn active_2() {}"
        )
        self.assertEqual(
            profile.strip_block_comments(content),
            expected,
        )

        # Test nesting inside a line comment (should not be treated as nested block comments)
        self.assertEqual(
            profile.strip_strings_and_comments("// /* nesting start but inside line comment"),
            "",
        )

        # Test nested block comments containing string literals with delimiters (blindness fix)
        content_with_string = (
            "/* Outer\n"
            '  let s = "*/";\n'
            "*/\n"
            "fn active() {}"
        )
        expected_with_string = (
            "\n"
            "\n"
            "\n"
            "fn active() {}"
        )
        self.assertEqual(
            profile.strip_block_comments(content_with_string),
            expected_with_string,
        )

        # Test nested block comments containing raw string literals with delimiters
        content_with_raw_string = (
            "/* Outer\n"
            '  let s = r#"*/"#;\n'
            "*/\n"
            "fn active() {}"
        )
        self.assertEqual(
            profile.strip_block_comments(content_with_raw_string),
            expected_with_string,
        )


    def test_prefix_delimiter_matching_and_supports_check(self):
        """Verify sorting delims by length descending and checking supports flags."""
        class PrefixOverlappingProfile(LanguageProfile):
            """Profile with overlapping delimiters where one is prefix of another."""
            block_comment_start = "/*"
            block_comment_end = "/*/"  # overlapping, block_comment_start is prefix
            line_comment = "//"
            supports_block_comments = True
            supports_nested_block_comments = True

        profile = PrefixOverlappingProfile()
        self.assertEqual(
            profile.strip_strings_and_comments("/* Outer /*/"),
            "",
        )

        class NestedDisabledProfile(LanguageProfile):
            """Profile with nested block comments disabled but configured."""
            block_comment_start = "/*"
            block_comment_end = "*/"
            supports_block_comments = False
            supports_nested_block_comments = True

        profile_disabled = NestedDisabledProfile()
        self.assertEqual(
            profile_disabled.strip_block_comments("/* Outer /* Inner */ */"),
            "/* Outer /* Inner */ */",
        )

    def test_unclosed_string_literals_do_not_span_lines(self):
        """Unclosed standard strings do not match across newlines in strip_block_comments."""
        profile = get_language_profile("main.cpp")
        content = '"unclosed\n/* comment containing my_func(); */\n"closed"'
        # The block comment should be stripped, but standard string shouldn't cross lines
        self.assertEqual(
            profile.strip_block_comments(content),
            '"unclosed\n\n"closed"'
        )

    def test_c_family_macro_definition_patterns(self):
        """CFamilyProfile successfully extracts definition patterns for macro-generated
        and macro-prefixed definitions.
        """
        profile = get_language_profile("main.cpp")
        func_name = "myTarget"
        patterns = profile.get_definition_patterns(func_name)

        # We expect 7 patterns now (5 original + 2 macro heuristics)
        self.assertEqual(len(patterns), 7)

        macro_arg_pattern = patterns[5]
        macro_prefix_pattern = patterns[6]

        # Test macro argument pattern
        self.assertTrue(macro_arg_pattern.search("TEST_F(MyClass, myTarget)"))
        self.assertTrue(macro_arg_pattern.search("TEST(MyClass, myTarget) {"))
        self.assertTrue(macro_arg_pattern.search(
            "DECLARE_DYNAMIC_MULTICAST_DELEGATE(FMyDelegate, myTarget, Param1)"
        ))
        self.assertFalse(macro_arg_pattern.search("myTarget(foo);"))
        self.assertFalse(macro_arg_pattern.search("void myTarget()"))

        # Test macro prefixed pattern
        self.assertTrue(macro_prefix_pattern.search("UFUNCTION(BlueprintCallable) void myTarget()"))
        self.assertTrue(macro_prefix_pattern.search("UFUNCTION() void myTarget()"))
        self.assertTrue(macro_prefix_pattern.search(
            "UFUNCTION(BlueprintCallable) DEPRECATED(5.0) void myTarget()"
        ))
        self.assertTrue(macro_prefix_pattern.search(
            "UFUNCTION(BlueprintCallable) std::map<int, std::string> myTarget()"
        ))
        self.assertTrue(macro_prefix_pattern.search(
            "UFUNCTION(BlueprintCallable) std::pair<int, int> MyClass<T, U>::myTarget()"
        ))
        self.assertFalse(macro_prefix_pattern.search("void myTarget()"))
        self.assertFalse(macro_prefix_pattern.search(
            "UFUNCTION(BlueprintCallable) void otherFunc()"
        ))

    def test_modern_js_ts_extensions(self):
        """JavaScript and TypeScript profiles resolve modern extensions correctly."""
        js_extensions = [".js", ".jsx", ".mjs", ".cjs"]
        ts_extensions = [".ts", ".tsx", ".mts", ".cts"]

        for ext in js_extensions:
            profile = get_language_profile(f"example{ext}")
            self.assertEqual(profile.name, "javascript")
            self.assertEqual(profile.comment_prefix, "//")
            self.assertEqual(
                profile.strip_strings_and_comments("const x = 1; // comment"),
                "const x = 1; ",
            )

        for ext in ts_extensions:
            profile = get_language_profile(f"example{ext}")
            self.assertEqual(profile.name, "typescript")
            self.assertEqual(profile.comment_prefix, "//")
            self.assertEqual(
                profile.lsp_command,
                ("typescript-language-server", "--stdio"),
            )
            self.assertEqual(
                profile.strip_strings_and_comments("const x: number = 1; // comment"),
                "const x: number = 1; ",
            )

        for ext in js_extensions + ts_extensions:
            self.assertIn(ext, CONFIG['bindings'])
            self.assertIn(ext, CONFIG['lang_map'])
            self.assertIn(ext, CONFIG['dependency_query_strings'])
            self.assertIn(ext, CONFIG['callee_query_strings'])

    def test_java_profile(self):
        """Java profile resolves correctly, strips comments and matches definitions."""
        # pylint: disable=too-many-statements
        profile = get_language_profile("example.java")
        self.assertEqual(profile.name, "java")
        self.assertEqual(profile.comment_prefix, "//")
        self.assertEqual(profile.lsp_command, ("jdtls",))

        # Test comment stripping
        self.assertEqual(
            profile.strip_strings_and_comments("int x = 1; // comment"),
            "int x = 1; ",
        )
        self.assertEqual(
            profile.strip_strings_and_comments("String s = \"hello // comment\";"),
            "String s = ;",
        )

        # Test block comments stripping
        content = "int a = 1; /* block\ncomment */ int b = 2;"
        self.assertEqual(
            profile.strip_block_comments(content),
            "int a = 1; \n int b = 2;",
        )

        # Test definition patterns
        # pylint: disable=protected-access
        patterns = profile.get_definition_patterns("MyTarget")

        class_pat = patterns[0]
        annotation_pat = patterns[1]
        method_pat = patterns[2]
        constructor_pat = patterns[3]

        # Class / Interface / Enum / Record
        self.assertTrue(class_pat.search("public class MyTarget {"))
        self.assertTrue(class_pat.search("interface MyTarget<T>"))
        self.assertTrue(class_pat.search("enum MyTarget"))
        self.assertTrue(class_pat.search("record MyTarget(int x)"))
        self.assertFalse(class_pat.search("class MyTargetOther"))

        # Annotation (@interface)
        self.assertTrue(annotation_pat.search("public @interface MyTarget"))
        self.assertFalse(annotation_pat.search("@interface MyTargetOther"))

        # Methods / Constructors (with modifiers/return type)
        self.assertTrue(method_pat.search("public void MyTarget()"))
        self.assertTrue(method_pat.search("private static int MyTarget(int x, String y)"))
        self.assertTrue(method_pat.search("synchronized <T> T[] MyTarget()"))
        self.assertTrue(method_pat.search("public Map<String, Object> MyTarget(int x)"))
        self.assertTrue(method_pat.search("Map.Entry<K, V> MyTarget()"))
        self.assertTrue(method_pat.search("List<@NonNull String> MyTarget(String... args)"))
        self.assertTrue(method_pat.search("public static List<@NonNull String> MyTarget(int x)"))
        self.assertTrue(method_pat.search("public List<?> MyTarget()"))
        self.assertTrue(method_pat.search("public <T extends A & B> T MyTarget()"))
        self.assertTrue(method_pat.search("List<? extends Runnable & Serializable> MyTarget()"))
        self.assertFalse(method_pat.search("void MyTargetOther()"))

        # Package-private constructors (no modifiers/return type, no semicolon)
        self.assertTrue(constructor_pat.search("MyTarget()"))
        self.assertTrue(constructor_pat.search("MyTarget(double val) throws Exception {"))
        self.assertFalse(constructor_pat.search("MyTargetOther()"))

        # Negative tests to verify keyword-preceded calls are not matched as definitions
        self.assertFalse(method_pat.search("return MyTarget();"))
        self.assertFalse(method_pat.search("throw MyTarget();"))
        self.assertFalse(method_pat.search("new MyTarget()"))
        self.assertFalse(method_pat.search("else MyTarget();"))
        self.assertFalse(method_pat.search("case MyTarget():"))
        self.assertFalse(method_pat.search("if (MyTarget())"))
        self.assertFalse(method_pat.search("while (MyTarget())"))
        self.assertFalse(method_pat.search("for (MyTarget(); ;)"))
        self.assertFalse(method_pat.search("assert MyTarget();"))

        # Negative tests to verify constructor pattern doesn't match keyword-preceded calls
        self.assertFalse(constructor_pat.search("return MyTarget();"))
        self.assertFalse(constructor_pat.search("throw MyTarget();"))
        self.assertFalse(constructor_pat.search("new MyTarget()"))
        self.assertFalse(constructor_pat.search("else MyTarget();"))

        # Verify a simple method call on a line by itself is not matched as a definition
        self.assertFalse(method_pat.search("MyTarget();"))
        self.assertFalse(constructor_pat.search("MyTarget();"))

        self.assertIn(".java", CONFIG['bindings'])
        self.assertIn(".java", CONFIG['lang_map'])
        self.assertIn(".java", CONFIG['dependency_query_strings'])
        self.assertIn(".java", CONFIG['callee_query_strings'])

        # Test Tree-sitter query validity for Java (verify method_reference lacks 'name:')
        dep_query = CONFIG['dependency_query_strings']['.java']
        callee_query = CONFIG['callee_query_strings']['.java']

        self.assertIn("method_invocation name: (identifier)", dep_query)
        self.assertIn("method_reference (_) (identifier)", dep_query)
        self.assertNotIn("method_reference name: (identifier)", dep_query)

        # Confirm dependency query is properly wrapped in outer parens
        self.assertTrue(dep_query.startswith("(("), f"Query should start with '((': {dep_query}")
        self.assertTrue(dep_query.endswith("))"), f"Query should end with '))': {dep_query}")

        self.assertIn("method_invocation name: (identifier)", callee_query)
        self.assertIn("method_reference (_) (identifier)", callee_query)
        self.assertNotIn("method_reference name: (identifier)", callee_query)

        # Test Java Text Blocks (multiline string literals) stripping
        self.assertEqual(
            profile.strip_strings_and_comments('String text = """hello""";'),
            "String text = ;",
        )
        content = 'String text = """\nline 1\nline 2\n""";'
        self.assertEqual(
            profile.strip_block_comments(content),
            'String text = \n\n\n;',
        )

    def test_java_tree_sitter_queries_compile(self):
        """Verify that Java Tree-sitter query strings compile successfully without syntax errors."""
        # pylint: disable=import-outside-toplevel
        try:
            import tree_sitter
            import tree_sitter_java
            lang = tree_sitter.Language(tree_sitter_java.language())

            dep_query = CONFIG['dependency_query_strings']['.java'].format(
                escaped_func_name="myMethod"
            )
            callee_query = CONFIG['callee_query_strings']['.java']

            # These should compile without raising QuerySyntaxError or other exceptions
            tree_sitter.Query(lang, dep_query)
            tree_sitter.Query(lang, callee_query)
        except ImportError:
            # Fallback if libraries are not present, but they are installed in this environment
            pass


if __name__ == "__main__":
    unittest.main()
