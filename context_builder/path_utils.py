"""Shared path helpers for cross-platform repository analysis.

These utilities centralize path normalization and case-sensitivity policy so
multiple scanners can reuse the same behavior instead of each inventing its own
heuristics.
"""

import os
import re
import subprocess

from .config import CONFIG


_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_WINDOWS_UNC_PATTERN = re.compile(r"^(?:\\\\|//)[^/\\]+[/\\][^/\\]+")
_PATH_CASE_RULE_CACHE = {}
_ROOT_CASE_CACHE = {}


def to_forward_slashes(value):
    """Return a string path with forward slashes."""
    return str(value or "").replace("\\", "/")


def to_backslashes(value):
    """Return a string path with backslashes."""
    return str(value or "").replace("/", "\\")


def normalize_for_path_match(value):
    """Normalize a path-like string for case-insensitive matching."""
    return to_forward_slashes(value).lower()


def normalize_root_for_path_match(value):
    """Normalize a root path and ensure a trailing separator for prefix checks."""
    normalized = normalize_for_path_match(value).rstrip("/")
    if not normalized:
        return normalized
    return normalized + "/"


def is_windows_drive_path(value):
    """Return whether a path starts with a Windows drive prefix."""
    return bool(_WINDOWS_DRIVE_PATTERN.match(str(value or "")))


def is_windows_unc_path(value):
    """Return whether a path uses a Windows UNC prefix."""
    return bool(_WINDOWS_UNC_PATTERN.match(str(value or "")))


def is_windows_style_path(value):
    """Return whether a path string clearly uses Windows path syntax."""
    text = str(value or "")
    return (
        is_windows_drive_path(text)
        or is_windows_unc_path(text)
        or ("\\" in text)
    )


def is_explicit_posix_style_path(value):
    """Return whether a path string clearly uses POSIX path syntax."""
    text = str(value or "")
    return (
        text.startswith("/")
        or text.startswith("./")
        or text.startswith("../")
    )


def normalize_case_rule_path(value):
    """Normalize a path string for config-driven case-sensitivity rules."""
    return to_forward_slashes(value).rstrip("/")


def _get_compiled_path_case_rule(pattern_text):
    """Compile and cache a regex used for path case overrides."""
    cached = _PATH_CASE_RULE_CACHE.get(pattern_text)
    if cached is not None:
        return cached
    compiled = re.compile(pattern_text)
    _PATH_CASE_RULE_CACHE[pattern_text] = compiled
    return compiled


def clear_path_case_caches():
    """Clear cached path case policy state after config changes in tests/CLI."""
    _PATH_CASE_RULE_CACHE.clear()
    _ROOT_CASE_CACHE.clear()


def _iter_case_override_candidates(path_value, root_path=None):
    """Yield normalized candidate strings that may match override rules."""
    seen = set()
    for value in (path_value, root_path):
        normalized = normalize_case_rule_path(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized
    if path_value and root_path and not os.path.isabs(path_value):
        root_norm = normalize_case_rule_path(root_path)
        rel_norm = normalize_case_rule_path(path_value).lstrip("/")
        if root_norm and rel_norm:
            joined = f"{root_norm}/{rel_norm}"
            if joined not in seen:
                yield joined


def get_path_case_override(path_value, root_path=None):
    """Return an explicit case-sensitivity override for a path, if configured."""
    rules = CONFIG.get("path_case_rules") or []
    if not isinstance(rules, list):
        return None
    candidates = tuple(_iter_case_override_candidates(path_value, root_path))
    if not candidates:
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern_text = rule.get("pattern")
        case_sensitive = rule.get("case_sensitive")
        if not isinstance(pattern_text, str) or not isinstance(case_sensitive, bool):
            continue
        try:
            pattern = _get_compiled_path_case_rule(pattern_text)
        except re.error:
            continue
        if any(pattern.search(candidate) for candidate in candidates):
            return case_sensitive
    return None


def _get_git_ignorecase(root_path):
    """Query Git's case-sensitivity hint for a repository root."""
    try:
        result = subprocess.run(
            ["git", "-C", root_path, "config", "--bool", "core.ignorecase"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip().lower()
    if value == "true":
        return False
    if value == "false":
        return True
    return None


def detect_root_case_sensitivity(root_path):
    """Resolve whether a repository root should be treated as case-sensitive."""
    normalized_root = normalize_case_rule_path(root_path)
    if not normalized_root:
        return True
    if normalized_root in _ROOT_CASE_CACHE:
        return _ROOT_CASE_CACHE[normalized_root]

    override = get_path_case_override(root_path, root_path=root_path)
    if override is not None:
        _ROOT_CASE_CACHE[normalized_root] = override
        return override

    git_hint = _get_git_ignorecase(root_path)
    if git_hint is not None:
        _ROOT_CASE_CACHE[normalized_root] = git_hint
        return git_hint

    if is_windows_style_path(root_path):
        result = False
    elif is_explicit_posix_style_path(root_path):
        result = True
    else:
        result = True
    _ROOT_CASE_CACHE[normalized_root] = result
    return result


def is_path_case_sensitive(path_value, root_path=None):
    """Return whether a path should be matched case-sensitively.

    Resolution order is:
    1. User override rules
    2. Explicit syntax on the path string itself
    3. Root/workspace case-sensitivity decision
    4. Conservative case-sensitive fallback
    """
    override = get_path_case_override(path_value, root_path=root_path)
    if override is not None:
        return override

    if is_windows_style_path(path_value):
        return False
    if is_explicit_posix_style_path(path_value):
        return True
    if root_path:
        return detect_root_case_sensitivity(root_path)
    return True


def build_root_replacement_variants(original_root, target_root):
    """Build replacement root variants for slash styles and Windows drive case."""
    if not isinstance(original_root, str) or not isinstance(target_root, str):
        return []
    original_root = original_root.rstrip("/\\")
    target_root = target_root.rstrip("/\\")
    variants = []
    for source_variant, target_variant in (
        (to_forward_slashes(original_root), to_forward_slashes(target_root)),
        (to_backslashes(original_root), to_backslashes(target_root)),
    ):
        variants.append((source_variant, target_variant))
        if is_windows_drive_path(source_variant):
            variants.append(
                (
                    source_variant[0].lower() + source_variant[1:],
                    target_variant,
                )
            )
            variants.append(
                (
                    source_variant[0].upper() + source_variant[1:],
                    target_variant,
                )
            )
    return variants
