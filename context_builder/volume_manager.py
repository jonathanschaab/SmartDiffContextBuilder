"""Module volume_manager manages the collection, sorting, and output generation of context payloads.

It structures the payload with sections like Raw Diff, Core Logic, Callees, Tests, Callers, and FFI.
"""

import json
import os

from .ast_engine import LANG_MAP, split_massive_block_ast


class VolumeManager:
    """Manages context payload creation, token budget enforcement, and markdown generation."""

    def __init__(self, fmt, max_lines, max_mb, base_name="SmartDiffContextBuilder", output_dir="."):
        """Initialize the volume manager with volume constraints.

        Args:
            fmt (str): Format name (e.g., 'markdown').
            max_lines (int): Max lines parameter for block truncation.
            max_mb (float): Max size in megabytes.
            base_name (str): Base filename for output.
            output_dir (str): Output directory path.
        """
        self.fmt = fmt.lower()
        self.max_lines = max_lines
        self.max_bytes = max_mb * 1024 * 1024
        self.base_name = base_name
        self.output_dir = output_dir
        self.raw_diff_text = ""

        # Categorical Storage for Funnel Sorting
        self.modified_objects = []
        self.unit_tests = []
        self.local_callers = []
        self.ffi_linkages = []
        self.local_callees = []

    def set_raw_diff(self, diff_text):
        """Set the raw diff text to include in the payload.

        Args:
            diff_text (str): Git diff output.
        """
        self.raw_diff_text = diff_text

    def add_modified_object(self, file_path, func_name, source_block):
        """Add a modified function/block to the payload core logic list.

        Args:
            file_path (str): Path to the source file.
            func_name (str): Function or block name.
            source_block (str): Code content of the function/block.
        """
        subunits = split_massive_block_ast(source_block, file_path, self.max_lines - 100)
        for sub in subunits:
            self.modified_objects.append({
                "file": file_path,
                "function_name": func_name + sub["suffix"],
                "source_block": sub["text"],
            })

    def add_callers(
        self,
        category_list,
        callers_dict,
        category_label,
        confidence="HIGH",
        distance=0,
    ):
        """Add detected caller references to the appropriate funnel category list.

        Args:
            category_list (list): Reference list to append items to.
            callers_dict (dict): Dictionary mapping file paths to matched occurrences.
            category_label (str): Label string describing the linkage type.
            confidence (str): Confidence string ('HIGH' or 'LOW').
            distance (int): Distance from modified core logic in bfs traversal.
        """
        for f, occs in callers_dict.items():
            for occ in occs:
                # Ghost Detection: Tag FFI bridges with a warning
                is_ffi = "FFI Bridge" in occ["code"]
                conf = "LOW (Ghost Risk)" if is_ffi else confidence
                category_list.append({
                    "file": f,
                    "type": category_label,
                    "line": occ["line"],
                    "code": occ["code"],
                    "confidence": conf,
                    "distance": distance,
                })

    def _flush_json_payload(self):
        """Build JSON payload and return (payload_string, extension)."""
        data = {
            "raw_diff": self.raw_diff_text,
            "modified_core_logic": self.modified_objects,
            "downstream_called_functions": self.local_callees,
            "validating_unit_tests": self.unit_tests,
            "upstream_dependent_callers": self.local_callers,
            "cross_language_ffi_linkages": self.ffi_linkages,
        }
        payload = json.dumps(data, indent=2)
        if len(payload.encode("utf-8")) > self.max_bytes:
            limit_mb = self.max_bytes / (1024 * 1024)
            print(
                f"\n[Warning] Payload exceeded size limit. JSON file may exceed "
                f"{limit_mb:.2f} MB limit."
            )
        return payload, "json"

    def _flush_markdown_payload(self):
        """Build markdown payload and return (payload_string, extension)."""
        payload = f"# LLM Context Payload\n## 1. Raw Diff\n```diff\n{self.raw_diff_text}\n```\n"
        payload_bytes = len(payload.encode("utf-8"))
        truncated = False

        # Helper to safely append to payload while enforcing byte limits
        def try_append(section_header, items, format_fn):
            nonlocal payload, payload_bytes, truncated
            if truncated or not items:
                return

            section_payload = ""
            section_bytes = 0

            header_bytes = len(section_header.encode("utf-8"))

            for item in items:
                formatted_item = format_fn(item)
                item_bytes = len(formatted_item.encode("utf-8"))

                # Header cost is only added once when the first item is successfully added
                header_cost = header_bytes if section_bytes == 0 else 0

                total_sz = payload_bytes + section_bytes + header_cost + item_bytes
                if total_sz > self.max_bytes:
                    truncated = True
                    break

                if section_bytes == 0:
                    section_payload = section_header
                    section_bytes = header_bytes

                section_payload += formatted_item
                section_bytes += item_bytes

            if section_payload:
                payload += section_payload
                payload_bytes += section_bytes

        # Level 1: Core Logic
        def format_object(obj):
            lang = LANG_MAP.get(os.path.splitext(obj["file"])[1], "text")
            return (
                f"### `{obj['file']}` -> `{obj['function_name']}()`\n"
                f"```{lang}\n{obj['source_block']}\n```\n"
            )

        try_append("## 2. Modified Core Logic\n", self.modified_objects, format_object)

        # Level 1.5: Downstream Callees
        def format_callee(c):
            lang = LANG_MAP.get(os.path.splitext(c["file"])[1], "text")
            return (
                f"### `{c['file']}` -> `{c['function_name']}()` "
                f"(Distance {c['distance']})\n```{lang}\n{c['code']}\n```\n"
            )

        try_append("## 3. Downstream Called Functions\n", self.local_callees, format_callee)

        # Level 2: Unit Tests
        def format_test(t):
            lang = LANG_MAP.get(os.path.splitext(t["file"])[1], "text")
            return f"### `{t['file']}` (Line {t['line']})\n```{lang}\n{t['code']}\n```\n"

        try_append("## 4. Validating Unit Tests\n", self.unit_tests, format_test)

        def format_dependency(dependency):
            return (
                f"- `{dependency['file']}` "
                f"(L{dependency['line']}, Distance {dependency['distance']}): "
                f"`{dependency['code']}` "
                f"**[Confidence: {dependency['confidence']}]**\n"
            )

        # Levels 3 and 4 share the same dependency record shape.
        try_append(
            "## 5. Upstream Dependent Callers\n",
            self.local_callers,
            format_dependency,
        )
        try_append(
            "## 6. Cross-Language FFI Linkages\n",
            self.ffi_linkages,
            format_dependency,
        )

        if truncated:
            limit_mb = self.max_bytes / (1024 * 1024)
            notice = (
                f"\n\n> [!WARNING]\n> Payload truncated because it exceeded "
                f"the size limit of {limit_mb:.2f} MB.\n"
            )
            payload += notice
            print(
                f"\n[Warning] Payload exceeded size limit. Truncated to fit within "
                f"{limit_mb:.2f} MB."
            )
        return payload, "md"

    def flush_all_volumes(self):
        """Build payloads chronologically and write to a file (markdown or JSON)."""
        # Sort callers, callees, and FFI linkages by distance from modified logic
        self.local_callers.sort(key=lambda x: x.get("distance", 0))
        self.local_callees.sort(key=lambda x: x.get("distance", 0))
        self.ffi_linkages.sort(key=lambda x: x.get("distance", 0))

        if self.fmt == "json":
            payload, ext = self._flush_json_payload()
        else:
            payload, ext = self._flush_markdown_payload()

        out_path = os.path.join(self.output_dir, f"{self.base_name}_final.{ext}")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"\n[SmartDiffContextBuilder] Successfully generated {out_path}")
