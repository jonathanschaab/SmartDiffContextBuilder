import os
import re
import xml.etree.ElementTree as ET
from .sys_utils import warn_once, run_command, ripgrep_filter, HAS_RG
from .ast_engine import AST_ENGINE, extract_function_bounds_regex
from .cache import get_global_cache

def get_coverage_data():
    cov_map = {}
    if os.path.exists("coverage.xml"):
        try:
            tree = ET.parse("coverage.xml")
            root = tree.getroot()
            for cls in root.iter('class'):
                filename = cls.get('filename')
                cov_map[filename] = [int(line.get('number')) for line in cls.iter('line') if int(line.get('hits', 0)) > 0]
        except Exception as exc:
            warn_once("coverage_xml_parse_fail", f"Failed to parse coverage.xml: {exc}")
    return cov_map

def mine_relevant_unit_tests(func_name, repo_files, current_source_file=None, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    discovered_tests = []
    seen_bodies = set()
    if not func_name or len(func_name) < 3: return discovered_tests

    files_to_scan = ripgrep_filter(repo_files, func_name) if HAS_RG else repo_files
    if current_source_file and current_source_file not in files_to_scan: files_to_scan.append(current_source_file)

    for file_path in files_to_scan:
        path_lower = file_path.lower()
        is_test_file = ("test" in path_lower or "spec" in path_lower or (file_path.endswith('.rs') and file_path == current_source_file))
        if not is_test_file: continue

        lines = file_cache.get_lines(file_path)
        ext = os.path.splitext(file_path)[1]

        ast_success = False
        if AST_ENGINE.is_supported(ext):
            source_bytes = file_cache.get_bytes(file_path)
            tree = AST_ENGINE.parsers[ext].parse(source_bytes)
            test_queries = {
                '.rs': '(attribute_item (attribute (identifier) @attr (#eq? @attr "test")))',
                '.py': '(function_definition name: (identifier) @name (#match? @name "^test_"))',
            }
            if ext in test_queries:
                try:
                    query = AST_ENGINE.languages[ext].query(test_queries[ext])
                    captures = query.captures(tree.root_node)
                    for node, _ in captures:
                        func_node = node
                        while func_node and func_node.type not in ['function_item', 'function_definition']:
                            func_node = func_node.parent
                        if func_node:
                            # Using regex with word boundaries (\b) and optional test prefix/suffix to avoid false positive substring matching (e.g. matching 'run' in 'runner' but matching 'test_run' or 'run_test')
                            node_text = source_bytes[func_node.start_byte:func_node.end_byte].decode('utf-8', errors='ignore')
                            if re.search(rf'\b(?:test_)?{re.escape(func_name)}(?:_test)?\b', node_text):
                                start_l, end_l = func_node.start_point[0], func_node.end_point[0] + 1
                                test_body = "".join(lines[start_l:end_l])
                                normalized = test_body.strip()
                                if normalized not in seen_bodies:
                                    seen_bodies.add(normalized)
                                    discovered_tests.append({"file": file_path, "line": start_l + 1, "code": test_body})
                    ast_success = True
                except Exception as exc:
                    warn_once("test_query_fail", f"AST test query failed on {file_path}: {exc}")

        if not ast_success:
            for idx, line in enumerate(lines):
                # Using regex with word boundaries (\b) and optional test prefix/suffix to avoid false positive substring matching (e.g. matching 'run' in 'runner' but matching 'test_run' or 'run_test')
                if re.search(rf'\b(?:test_)?{re.escape(func_name)}(?:_test)?\b', line) and any(term in line.lower() for term in ["test", "it(", "describe"]):
                    start, end = extract_function_bounds_regex(file_path, idx + 1, file_cache=file_cache)
                    if start is not None:
                        test_body = "".join(lines[start:end])
                        normalized = test_body.strip()
                        if normalized not in seen_bodies:
                            seen_bodies.add(normalized)
                            discovered_tests.append({"file": file_path, "line": start + 1, "code": test_body})
                        
    return discovered_tests
