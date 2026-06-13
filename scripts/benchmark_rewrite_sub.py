"""Benchmark lambda vs escaped-string replacement in compile_commands rewrite."""
# pylint: disable=missing-module-docstring,import-outside-toplevel

import copy
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from context_builder.cli import (  # noqa: E402
    _build_worktree_root_replacements,
    _rewrite_compile_commands_payload_with_replacements,
)


def _rewrite_with_lambda(payload, replacements):
    """Current implementation: lambda binds target_root per replacement."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            payload[key] = _rewrite_with_lambda(value, replacements)
        return payload
    if isinstance(payload, list):
        for idx, item in enumerate(payload):
            payload[idx] = _rewrite_with_lambda(item, replacements)
        return payload
    if not isinstance(payload, str):
        return payload
    rewritten = payload
    for source_root, target_root, pattern, case_sensitive in replacements:
        haystack = rewritten if case_sensitive else rewritten.lower()
        needle = source_root if case_sensitive else source_root.lower()
        if needle not in haystack:
            continue
        rewritten = pattern.sub(
            lambda _match, replacement=target_root: replacement,
            rewritten,
        )
    return rewritten


def _rewrite_with_escaped_string(payload, replacements):
    """Proposed implementation: escape backslashes for C-level re.sub."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            payload[key] = _rewrite_with_escaped_string(value, replacements)
        return payload
    if isinstance(payload, list):
        for idx, item in enumerate(payload):
            payload[idx] = _rewrite_with_escaped_string(item, replacements)
        return payload
    if not isinstance(payload, str):
        return payload
    rewritten = payload
    for source_root, target_root, pattern, case_sensitive in replacements:
        haystack = rewritten if case_sensitive else rewritten.lower()
        needle = source_root if case_sensitive else source_root.lower()
        if needle not in haystack:
            continue
        rewritten = pattern.sub(
            target_root.replace("\\", "\\\\"),
            rewritten,
        )
    return rewritten


def _make_synthetic_payload(entry_count, original_root=r"C:\repo"):
    """Build a compile_commands.json-like payload with repeated path strings."""
    forward_root = original_root.replace("\\", "/")
    entries = []
    for idx in range(entry_count):
        rel = f"src/module_{idx % 200}/file_{idx}.cpp"
        entries.append(
            {
                "directory": f"{forward_root}/build",
                "file": f"{original_root}\\{rel.replace('/', chr(92))}",
                "command": (
                    f'clang++ -I {forward_root}/include '
                    f'"{forward_root}/{rel}" -o {forward_root}/build/o_{idx}.o'
                ),
                "arguments": [
                    "clang++",
                    f"{original_root}\\{rel.replace('/', chr(92))}",
                    "-I",
                    f"{forward_root}/include",
                ],
                "output": f"{forward_root}/build/o_{idx}.o",
            }
        )
    return entries


def _time_callable(func, *args, rounds=5):
    timings = []
    for _ in range(rounds):
        payload = copy.deepcopy(args[0])
        start = time.perf_counter()
        func(payload, *args[1:])
        timings.append(time.perf_counter() - start)
    return timings


def _summarize(label, timings):
    mean = statistics.mean(timings)
    stdev = statistics.pstdev(timings) if len(timings) > 1 else 0.0
    print(f"  {label:28s}  mean={mean:.4f}s  stdev={stdev:.4f}s  min={min(timings):.4f}s")
    return mean


def _micro_benchmark_sub(replacements, sample_strings, rounds=50):
    """Benchmark only the pattern.sub call on many strings."""
    lambda_timings = []
    escaped_timings = []
    for _ in range(rounds):
        strings = list(sample_strings)

        start = time.perf_counter()
        for source_root, target_root, pattern, case_sensitive in replacements:
            for idx, text in enumerate(strings):
                haystack = text if case_sensitive else text.lower()
                needle = source_root if case_sensitive else source_root.lower()
                if needle not in haystack:
                    continue
                strings[idx] = pattern.sub(
                    lambda _match, replacement=target_root: replacement,
                    text,
                )
        lambda_timings.append(time.perf_counter() - start)

        strings = list(sample_strings)
        start = time.perf_counter()
        for source_root, target_root, pattern, case_sensitive in replacements:
            escaped = target_root.replace("\\", "\\\\")
            for idx, text in enumerate(strings):
                haystack = text if case_sensitive else text.lower()
                needle = source_root if case_sensitive else source_root.lower()
                if needle not in haystack:
                    continue
                strings[idx] = pattern.sub(escaped, text)
        escaped_timings.append(time.perf_counter() - start)

    return lambda_timings, escaped_timings


def _assert_equivalent(payload_a, payload_b):
    if payload_a != payload_b:
        raise AssertionError("Lambda and escaped-string rewrites produced different output")


def main():
    entry_count = 50_000
    original_root = r"C:\repo"
    worktree_root = r"D:\worktree"
    replacements = _build_worktree_root_replacements(original_root, worktree_root)
    payload = _make_synthetic_payload(entry_count, original_root)

    print("=== Correctness check (synthetic payload) ===")
    lambda_result = _rewrite_with_lambda(copy.deepcopy(payload), replacements)
    escaped_result = _rewrite_with_escaped_string(copy.deepcopy(payload), replacements)
    _assert_equivalent(lambda_result, escaped_result)
    print("  Lambda and escaped-string outputs match.")

    print("\n=== Correctness check (production helper) ===")
    prod_result = _rewrite_compile_commands_payload_with_replacements(
        copy.deepcopy(payload),
        replacements,
    )
    _assert_equivalent(prod_result, lambda_result)
    print("  Production helper matches lambda reference.")

    sample_strings = [
        entry["command"] for entry in payload[:5000]
    ] + [
        entry["file"] for entry in payload[:5000]
    ]

    print(f"\n=== Micro benchmark (pattern.sub on {len(sample_strings)} strings) ===")
    lambda_micro, escaped_micro = _micro_benchmark_sub(replacements, sample_strings)
    lambda_micro_mean = _summarize("lambda", lambda_micro)
    escaped_micro_mean = _summarize("escaped string", escaped_micro)
    print(f"  speedup: {lambda_micro_mean / escaped_micro_mean:.2f}x")

    print(f"\n=== Macro benchmark ({entry_count} compile_commands entries) ===")
    lambda_macro = _time_callable(_rewrite_with_lambda, payload, replacements)
    escaped_macro = _time_callable(_rewrite_with_escaped_string, payload, replacements)
    prod_macro = _time_callable(
        _rewrite_compile_commands_payload_with_replacements,
        payload,
        replacements,
    )
    lambda_macro_mean = _summarize("lambda (reference)", lambda_macro)
    escaped_macro_mean = _summarize("escaped string", escaped_macro)
    prod_macro_mean = _summarize("production helper", prod_macro)
    print(f"  escaped vs lambda speedup: {lambda_macro_mean / escaped_macro_mean:.2f}x")
    print(f"  production vs lambda delta: {prod_macro_mean / lambda_macro_mean:.3f}x")


if __name__ == "__main__":
    main()
