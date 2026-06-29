# SmartDiffContextBuilder

`SmartDiffContextBuilder` is a command-line tool that compiles context-aware git diff payloads optimized for LLMs. By analyzing a git diff, it automatically extracts modified function blocks, traces upstream callers and downstream callees, mines relevant unit tests, tracks cross-language FFI boundaries, and performs macro expansion. The result is serialized into a structured context payload.

---

## Key Features

1. **Raw Git Diff Analysis**: Captures the exact modified regions in unified diff format.
2. **Core Logic Extraction**: Automatically resolves and extracts the full source block of modified functions using Tree-sitter AST queries or syntax-aware regex fallbacks.
3. **Upstream Caller BFS Tracing (`--caller-depth`)**: Recursively crawls up the call graph using LSP references and AST/regex search to capture containing functions that call into your modified logic.
4. **Downstream Callee BFS Tracing (`--callee-depth`)**: Recursively crawls down the call graph to extract the body and definition of functions called by your modified logic.
5. **Validating Unit Test Mining**: Automatically isolates and includes tests referencing the modified functions to provide the LLM with direct validation context.
6. **Cross-Language FFI Linkages**: Traces boundaries across Rust (wasm/no_mangle), C/C++ (extern "C"), and Python (pybind11).
7. **C/C++ Macro Expansion**: Pre-expands macros using clang and source-maps the expansion back to identify macro call sites.
8. **Compilation Database Support**: Parses `compile_commands.json` to link corresponding source and header translation units.
9. **Commit Range Worktree Checkouts**: Safely analyzes sequential commits in a temporary detached worktree (without branch conflicts), preserving your local development context.
10. **Fast File Filtering with ripgrep**: Uses `rg` for high-speed dependency and test file filtering, with graceful fallback to manual scanning if ripgrep is not installed.

---

## Dependencies & Requirements

To leverage the full suite of SmartDiffContextBuilder features, ensure the following dependencies are met.

### System & Language Runtime
- **Python**: Version `3.12` or newer (required).

### Python Libraries
- Install the required runtime packages with `pip install -r requirements.txt`.
- **Optional AST support**: `tree-sitter` 0.21.0 or newer plus the language-specific bindings used by your repository (for example, `tree-sitter-python`, `tree-sitter-rust`, `tree-sitter-javascript`, or `tree-sitter-typescript`). Without them, analysis falls back to regex-based parsing.

### External Toolchains (Optional but highly recommended)
- **ripgrep (`rg`)**: Used for fast dependency and test filtering. Falls back to manual scanning if not installed.
- **Git**: Required for change tracking and worktree range analysis.
- **Language Servers (LSPs)** — used for accurate upstream caller tracing:
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

## Usage

Run the tool from the root of your git repository.

### Basic Workspace Scan
Analyze the current uncommitted changes:
```bash
python smart_diff_context_builder.py
```

### Depth-Controlled Call Graph Traversal
Limit upstream caller tracing to 2 levels and downstream callee tracing to 1 level:
```bash
python smart_diff_context_builder.py --caller-depth 2 --callee-depth 1
```

### Commit Range Analysis
Analyze the difference across a sequence of commits in a temporary worktree:

| Format | Example | Meaning |
|---|---|---|
| `-N` | `--commit-range -3` | Last 3 commits relative to HEAD |
| `START..END` | `--commit-range abc123..def456` | Explicit commit range |
| `START+N` | `--commit-range abc123+2` | N commits forward from START |
| `END-N` | `--commit-range HEAD-2` | N commits back from END |

```bash
python smart_diff_context_builder.py --commit-range -3
```

> **Note:** When running in a clean worktree, starting a language server may take several minutes
> while the project is indexed. Worktree scans allow at least 120 seconds for initialization and
> 300 seconds for reference queries. Use `--no-language-server` to skip LSP and avoid this delay.

### Output Limits
Restrict source-block and payload sizes:
```bash
python smart_diff_context_builder.py --max-lines 800 --max-mb 1.5
```

### Skipping Language Server / FFI / Macro Expansion
```bash
python smart_diff_context_builder.py --no-language-server --skip-ffi --skip-macro-expansion
```

### Config File
Generate a commented config file capturing all current settings, then load it later:
```bash
python smart_diff_context_builder.py --caller-depth 2 --create-config .smdc_config.json
python smart_diff_context_builder.py --config .smdc_config.json
```

---

## CLI Reference

### Output Control

| Flag | Type | Default | Description |
|---|---|---|---|
| `--format` | `md` \| `json` | `md` | Requested format; the current serializer writes Markdown |
| `--max-lines` | int | `1500` | Maximum source-block size before semantic pruning |
| `--max-mb` | float | `2.0` | Maximum payload size before truncation |
| `--base-name` | str | `SmartDiffContextBuilder` | Base name for the output file (`{base-name}_final.md`) |

### Analysis Depth

| Flag | Type | Default | Description |
|---|---|---|---|
| `--caller-depth` | int | `1` | BFS depth for upstream caller tracing |
| `--callee-depth` | int | `1` | BFS depth for downstream callee tracing |
| `--data-depth` | int | `1` | BFS depth for data flow tracing |
| `--max-interface-depth` | int | `15` | Maximum interface/inheritance depth |

### Language Server (LSP)

| Flag | Type | Default | Description |
|---|---|---|---|
| `--no-language-server` | flag | off | Disable LSP for caller tracing (falls back to AST/regex) |
| `--lsp-init-timeout` | float | `60` | LSP initialization handshake timeout in seconds |
| `--lsp-timeout` | float | `150` | LSP reference query timeout in seconds |
| `--disable-pruning` | flag | off | Disable caller graph pruning (may significantly increase output size) |

Language servers that publish standard LSP work-done progress display an indexing
progress bar in interactive terminals and periodic milestone lines in redirected
or CI output. Servers that do not publish progress continue without an indicator.

### Performance

| Flag | Type | Default | Description |
|---|---|---|---|
| `--ripgrep-timeout` | float | `10.0` | ripgrep subprocess timeout in seconds (supports fractional values) |
| `--max-cache-size-mb` | float | `200.0` | In-memory file cache limit in MB |
| `--data-flow-batch-size` | int | `32` | Max worker thread count for concurrent data flow resolution |

### Commit Range

| Flag | Type | Default | Description |
|---|---|---|---|
| `--commit-range` | str | — | Commit range to analyze (e.g. `-3`, `START..END`, `START+2`, `END-3`) |

### Feature Toggles

| Flag | Default | Description |
|---|---|---|
| `--skip-ffi` | off | Skip cross-language FFI boundary tracing |
| `--skip-macro-expansion` | off | Skip C/C++ macro pre-expansion |

### Configuration

| Flag | Type | Description |
|---|---|---|
| `--config` | path | Load settings from a JSON config file |
| `--create-config` | path | Write a commented config file reflecting current CLI settings, then exit |

### Advanced Pattern Overrides
These accept JSON strings and override internal regex/query patterns for expert use:

| Flag | Description |
|---|---|
| `--lang-map` | JSON object mapping file extensions to language names |
| `--bindings` | JSON object of tree-sitter language bindings |
| `--dependency-query-strings` | JSON object of tree-sitter dependency queries |
| `--callee-query-strings` | JSON object of tree-sitter callee queries |
| `--callee-ignored-keywords` | JSON list of keywords to ignore during callee extraction |
| `--ffi-patterns` | JSON list of FFI annotation patterns |
| `--func-decl-pattern` | Regex pattern for function declaration detection |
| `--def-pattern-template` | Regex template for function definition search |
| `--cpp-def-pattern-template` | Regex template for C++ function definition search |
| `--callee-pattern` | Regex pattern for callee call-site detection |
| `--ffi-rg-pattern` | ripgrep pattern for FFI export scanning |

---

## Output Structure

The payload is written to `{base-name}_final.md` (default: `SmartDiffContextBuilder_final.md`). Sections are ordered by proximity to the modified logic:

1. **Raw Diff**: Modified lines and hunks.
2. **Modified Core Logic**: Bodies of modified functions.
3. **Downstream Called Functions**: Code definitions for downstream functions called by core logic.
4. **Validating Unit Tests**: Isolated unit tests that reference or test the modified logic.
5. **Upstream Dependent Callers**: Functions that call into the modified logic.
6. **Cross-Language FFI Linkages**: FFI call-site locations across language boundaries.

When the payload exceeds `--max-mb`, lower-priority sections are truncated and the output includes a warning notice.

---

## Language Profiles

Language-specific behavior lives in `context_builder/languages/`. Profiles define
comment syntax, LSP commands, block style, C/C++ preprocessing capabilities, and
function-name fallback behavior. Shared scanners resolve a profile through the
registry instead of maintaining their own extension lists.

Unregistered extensions use `unknown_language.py`, which preserves the
conservative C-like fallback behavior. User-configurable tree-sitter bindings
and query strings remain in the configuration layer so custom language support
does not require editing a built-in profile.
