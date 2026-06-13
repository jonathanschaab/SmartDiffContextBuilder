"""Shared path helpers for cross-platform repository analysis.

These utilities keep Windows/Linux path quirks in one place so higher-level
logic can stay focused on scanning and rewriting behavior.
"""

import re


_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


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
