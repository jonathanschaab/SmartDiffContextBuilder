"""Shared path helpers for cross-platform repository analysis.

These utilities centralize path normalization and case-sensitivity policy so
multiple scanners can reuse the same behavior instead of each inventing its own
heuristics.
"""

# pylint: disable=cyclic-import

import os
import re
import subprocess


_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_WINDOWS_UNC_PATTERN = re.compile(r"^(?:\\\\|//)[^/\\]+[/\\][^/\\]+")
_PATH_CASE_RULE_CACHE = {}
_ROOT_CASE_CACHE = {}


def _get_config_value(key, default=None):
    """Read a config value lazily to avoid import cycles at module load time."""
    from .config import CONFIG  # pylint: disable=import-outside-toplevel

    return CONFIG.get(key, default)


def _warn_once(key, message):
    """Delegate warnings lazily to avoid import cycles at module load time."""
    from .sys_utils import warn_once  # pylint: disable=import-outside-toplevel

    warn_once(key, message)


def _run_git_probe_process(cmd, timeout, **kwargs):
    """Delegate git subprocess execution lazily to avoid import cycles."""
    from .sys_utils import run_git_process  # pylint: disable=import-outside-toplevel

    return run_git_process(
        cmd,
        timeout=timeout,
        timeout_key="git_probe_timeout",
        timeout_option="--git-probe-timeout",
        **kwargs,
    )


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


def normalize_root_for_explicit_match(value, case_sensitive):
    """Normalize a root path for prefix/equality checks using explicit case policy."""
    normalized = to_forward_slashes(value).rstrip("/")
    if not case_sensitive:
        normalized = normalized.lower()
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
    is_abs = False
    if path_value:
        is_abs = (
            os.path.isabs(path_value)
            or is_windows_drive_path(path_value)
            or is_windows_unc_path(path_value)
        )
    if path_value and root_path and not is_abs:
        root_norm = normalize_case_rule_path(root_path)
        rel_norm = normalize_case_rule_path(path_value).lstrip("/")
        if root_norm and rel_norm:
            joined = f"{root_norm}/{rel_norm}"
            if joined not in seen:
                yield joined


def get_path_case_override(path_value, root_path=None):
    """Return an explicit case-sensitivity override for a path, if configured."""
    rules = _get_config_value("path_case_rules") or []
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
    from .config import DEFAULT_GIT_PROBE_TIMEOUT  # pylint: disable=import-outside-toplevel
    from .sys_utils import validate_timeout_setting  # pylint: disable=import-outside-toplevel

    timeout_val = _get_config_value("git_probe_timeout", DEFAULT_GIT_PROBE_TIMEOUT)
    timeout = validate_timeout_setting(
        timeout_val,
        DEFAULT_GIT_PROBE_TIMEOUT,
        "git_probe_timeout",
        "--git-probe-timeout",
    )

    try:
        result = _run_git_probe_process(
            ["git", "-C", root_path, "config", "--bool", "core.ignorecase"],
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result is None:
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


def path_is_within_root(path_value, root_path, case_sensitive=None):
    """Return whether a path is equal to or contained within a root path."""
    if case_sensitive is None:
        case_sensitive = is_path_case_sensitive(path_value, root_path=root_path)
    normalized_path = to_forward_slashes(path_value).rstrip("/")
    normalized_root = to_forward_slashes(root_path).rstrip("/")
    if not case_sensitive:
        normalized_path = normalized_path.lower()
        normalized_root = normalized_root.lower()
    return (
        normalized_path == normalized_root
        or normalized_path.startswith(normalize_root_for_explicit_match(root_path, case_sensitive))
    )


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


def find_artifact_path(filename, base_dir=None):
    """Find the most recently modified instance of filename.

    Checks in base_dir and its configured build subdirectories.
    """
    if base_dir is None:
        base_dir = os.getcwd()

    candidates = []
    # Check base_dir
    p = os.path.join(base_dir, filename)
    if os.path.exists(p):
        candidates.append(p)

    # Check configured build directories
    build_dirs = _get_config_value("build_directories", [])
    if isinstance(build_dirs, (list, tuple, set)):
        for b_dir in build_dirs:
            if isinstance(b_dir, str) and b_dir:
                try:
                    if os.path.isabs(b_dir):
                        p = os.path.join(b_dir, filename)
                    else:
                        p = os.path.join(base_dir, b_dir, filename)
                    if os.path.exists(p):
                        candidates.append(p)
                except (ValueError, TypeError, OSError):
                    pass

    if not candidates:
        return None

    # Return the one with the newest mtime, fallback to 0.0 if getmtime
    # raises OSError (e.g. in mocked tests).
    def safe_getmtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    return max(candidates, key=safe_getmtime)
