# ContextLens (SmartDiffContextBuilder)

`ContextLens` is a command-line tool designed to compile context-aware git diff tokens optimized for LLMs. By analyzing a git diff, `ContextLens` automatically extracts modified function blocks, traces upstream callers and downstream callees, mines relevant unit tests, tracks cross-language FFI boundaries, and performs macro expansion. The result is serialized into a highly optimized context payload structured across hierarchical volumes.

---

## Key Features

1. **Raw Git Diff Analysis**: Captures the exact modified regions in unified diff format.
2. **Core Logic Extraction**: Automatically resolves and extracts the full source block of the modified functions using Tree-sitter AST queries or syntax-aware regex fallbacks.
3. **Upstream Caller BFS Tracing (`--caller-depth`)**: Recursively crawls up the call graph using LSP references and AST/regex search to capture containing functions calling into your modified logic.
4. **Downstream Callee BFS Tracing (`--callee-depth`)**: Recursively crawls down the call graph to extract the body and definition of functions called by your modified logic.
5. **Validating Unit Test Mining**: Automatically isolates and includes tests referencing the modified functions to provide the LLM with direct validation context.
6. **Cross-Language FFI Linkages**: Traces boundaries across Rust (wasm/no_mangle), C/C++ (extern "C"), and Python (pybind11).
7. **C/C++ Macro Expansion**: Pre-expands macros using clang and source-maps the expansion back to identify macro call sites.
8. **Compilation Database Support**: Parses `compile_commands.json` to link corresponding source and header translation units.
9. **Commit Range Worktree Checkouts**: Safely analyzes sequential commits in a temporary detached worktree (without branch conflicts), preserving local development server context.

---

## Dependencies & Requirements

To leverage the full suite of ContextLens features, ensure the following dependencies are met:

### System & Language Runtime
- **Python**: Version `3.12` or newer (required).

### Python Libraries
- **py-tree-sitter**: Version `0.21.0` or newer (required for AST parsing and downstream callee extraction; Node `.text` attributes are not present in older versions).
- **tree-sitter-languages** (e.g. `tree-sitter-python`, `tree-sitter-rust`, etc. for supported languages).

### External Toolchains (Optional but highly recommended)
- **ripgrep (`rg`)**: Required for fast dependency and test filtering.
- **Git**: Required for change tracking and worktree range analysis.
- **Language Servers (LSPs)**:
  - **C/C++**: `clangd`
  - **Rust**: `rust-analyzer`
  - **Python**: `pylsp`
  - **TypeScript**: `typescript-language-server`

---

## Installation & Setup

1. **Clone the repository** and navigate to the root directory.
2. **Install requirements**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Verify LSPs**: Ensure target LSP binaries (e.g. `clangd` or `rust-analyzer`) are present in your system's `PATH`.

---

## Usage Instructions

Run the context packaging tool from the root of your git repository.

### Basic Workspace Scan
Analyze the current uncommitted changes:
```bash
python smart_diff_context_builder.py
```

### Depth-Controlled Graph Traversal
Limit upstream caller tracing to 2 levels and downstream callee tracing to 2 levels:
```bash
python smart_diff_context_builder.py --caller-depth 2 --callee-depth 2
```

### Historical Commit Range Analysis
Analyze the difference across a sequence of commits in a temporary worktree:
- **Last 3 commits**: `python smart_diff_context_builder.py --commit-range -3`
- **Specific ref offset**: `python smart_diff_context_builder.py --commit-range START+2`
- **Specific range**: `python smart_diff_context_builder.py --commit-range START..END`

### Output Format & Volume Limits
Generate JSON outputs and restrict volume sizes:
```bash
python smart_diff_context_builder.py --format json --max-lines 800 --max-mb 1.5
```

---

## Payload Structure

Payloads are written to `{base-name}_final.md` (default: `ContextLens_final.md`) with the following sections sorted by distance from the modified logic:
1. **Raw Diff**: Modified lines and hunks.
2. **Modified Core Logic**: Bodies of modified functions.
3. **Downstream Called Functions**: Code definitions for downstream functions called by core logic.
4. **Validating Unit Tests**: Isolated unit tests containing or testing modified logic.
5. **Upstream Dependent Callers**: References of functions invoking the modified logic.
6. **Cross-Language FFI Linkages**: FFI caller locations.
