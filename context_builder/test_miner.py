"""Module test_miner provides utilities to find and extract relevant unit tests.

It scans repository test files using AST or regex patterns to match test cases.
"""

import os
import re
import xml.etree.ElementTree as ET

from .ast_engine import AST_ENGINE, extract_function_bounds_regex
from .cache import get_global_cache
from .languages import get_language_profile
from .sys_utils import iter_scan_progress, ripgrep_filter, warn_once
from .path_utils import find_artifact_path

try:
    import tree_sitter
    TreeSitterQueryError = tree_sitter.QueryError
except ImportError:
    tree_sitter = None
    TreeSitterQueryError = ValueError


def get_coverage_data():
    """Parse coverage.xml to map files to lists of covered line numbers.

    Returns:
        dict: Mapping of normalized filename to list of covered lines.
    """
    cov_map = {}
    cov_path = find_artifact_path("coverage.xml")
    if cov_path:
        try:
            tree = ET.parse(cov_path)
            root = tree.getroot()
            for cls in root.iter("class"):
                filename = cls.get("filename")
                if filename:
                    normalized_filename = filename.replace("\\", "/")
                    cov_map[normalized_filename] = [
                        int(line.get("number"))
                        for line in cls.iter("line")
                        if int(line.get("hits", 0)) > 0
                    ]
        except Exception as exc:  # pylint: disable=broad-exception-caught
            warn_once("coverage_xml_parse_fail", f"Failed to parse coverage.xml: {exc}")
    return cov_map


def _process_ast_capture(
    node, source_bytes, test_pattern, lines, seen_bodies, discovered_tests, file_path
):
    """Process a single AST node capture."""
    func_node = node
    while func_node and func_node.type not in [
        "function_item",
        "function_definition",
    ]:
        func_node = func_node.parent
    if not func_node:
        return
    node_text = source_bytes[
        func_node.start_byte : func_node.end_byte
    ].decode("utf-8", errors="ignore")
    if not test_pattern.search(node_text):
        return
    start_l = func_node.start_point[0]
    end_l = func_node.end_point[0] + 1
    test_body = "".join(lines[start_l:end_l])
    normalized = test_body.strip()
    if normalized not in seen_bodies:
        seen_bodies.add(normalized)
        discovered_tests.append({
            "file": file_path,
            "line": start_l + 1,
            "code": test_body,
        })


def _mine_ast_tests(
    file_path, ext, test_pattern, source_bytes, lines, seen_bodies, discovered_tests
):
    """Scan file using AST query to find tests."""
    test_query = get_language_profile(file_path).test_query
    if not test_query:
        return False

    if tree_sitter is None:
        return False

    try:
        tree = AST_ENGINE.parsers[ext].parse(source_bytes)
        query = tree_sitter.Query(AST_ENGINE.languages[ext], test_query)
        captures = query.captures(tree.root_node)
        for node, _ in captures:
            _process_ast_capture(
                node,
                source_bytes,
                test_pattern,
                lines,
                seen_bodies,
                discovered_tests,
                file_path,
            )
        return True
    except (RuntimeError, ValueError, TreeSitterQueryError) as exc:
        warn_once("test_query_fail", f"AST test query failed on {file_path}: {exc}")
        return False


def _mine_regex_tests(
    file_path, lines, test_pattern, seen_bodies, discovered_tests, file_cache
):
    """Scan file line-by-line using regex to find tests."""
    for idx, line in enumerate(lines):
        # Match using test_pattern to avoid repeated compilation
        if test_pattern.search(line) and any(
            term in line.lower() for term in ["test", "it(", "describe"]
        ):
            start, end = extract_function_bounds_regex(
                file_path, idx + 1, file_cache=file_cache
            )
            if start is not None:
                test_body = "".join(lines[start:end])
                normalized = test_body.strip()
                if normalized not in seen_bodies:
                    seen_bodies.add(normalized)
                    discovered_tests.append({
                        "file": file_path,
                        "line": start + 1,
                        "code": test_body,
                    })


def _mine_single_file(
    file_path,
    test_pattern,
    current_source_file,
    seen_bodies,
    discovered_tests,
    file_cache,
):
    """Process a single file for mining tests."""
    path_lower = file_path.lower()
    profile = get_language_profile(file_path)
    is_test_file = (
        "test" in path_lower
        or "spec" in path_lower
        or (
            profile.tests_can_share_source_file
            and file_path == current_source_file
        )
    )
    if not is_test_file:
        return

    lines = file_cache.get_lines(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    ast_success = False
    if AST_ENGINE.is_supported(ext):
        source_bytes = file_cache.get_bytes(file_path)
        ast_success = _mine_ast_tests(
            file_path,
            ext,
            test_pattern,
            source_bytes,
            lines,
            seen_bodies,
            discovered_tests,
        )

    if not ast_success:
        _mine_regex_tests(
            file_path,
            lines,
            test_pattern,
            seen_bodies,
            discovered_tests,
            file_cache,
        )


def mine_relevant_unit_tests(
    func_name, repo_files, current_source_file=None, file_cache=None
):
    """Scan repo_files to find unit tests that call or relate to func_name.

    Args:
        func_name (str): Function/macro name.
        repo_files (list): List of repository files.
        current_source_file (str, optional): The current source file being modified.
        file_cache (LRUFileCache, optional): Cache instance.

    Returns:
        list: Discovered test dictionaries with file, line, and code keys.
    """
    if file_cache is None:
        file_cache = get_global_cache()
    discovered_tests = []
    seen_bodies = set()
    if not func_name or len(func_name) < 3:
        return discovered_tests

    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore).
    lead_b = r"\b" if func_name[0].isalnum() or func_name[0] == "_" else ""
    trail_b = r"\b" if func_name[-1].isalnum() or func_name[-1] == "_" else ""
    test_pattern = re.compile(
        rf"(?:\btest_|{lead_b}){re.escape(func_name)}(?:_[A-Za-z0-9_]+)?{trail_b}"
    )

    files_to_scan = ripgrep_filter(
        repo_files, func_name,
        fallback_hint=f"tests referencing '{func_name}'"
    )
    if current_source_file and current_source_file not in files_to_scan:
        files_to_scan.append(current_source_file)

    for file_path in iter_scan_progress(
        files_to_scan,
        label=f"Scanning tests referencing '{func_name}'",
        min_files=100,
    ):
        _mine_single_file(
            file_path,
            test_pattern,
            current_source_file,
            seen_bodies,
            discovered_tests,
            file_cache,
        )

    return discovered_tests
