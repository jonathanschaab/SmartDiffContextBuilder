"""Module sys_utils provides system utility functions for running commands and filtering files."""

import os
import subprocess
import sys

WARNED_MISSING_DEPS = set()


def warn_once(key, message):
    """Print a notice warning once per key to avoid stdout pollution.

    Args:
        key (str): Unique key for the warning.
        message (str): Warning message to display.
    """
    if key not in WARNED_MISSING_DEPS:
        print(f"\n[Notice] {message}")
        WARNED_MISSING_DEPS.add(key)


def run_command(cmd, exit_on_fail=False, timeout=None):
    """Run a system command and return its standard output.

    Args:
        cmd (list): Command and arguments to run.
        exit_on_fail (bool): If True, exits program on process error or command missing.
        timeout (float, optional): Timeout in seconds.

    Returns:
        str: Decoded standard output.
    """
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=timeout,
        )
        return res.stdout
    except subprocess.TimeoutExpired:
        warn_once(f"timeout_{cmd[0]}", f"Command '{' '.join(cmd)}' timed out.")
        return ""
    except subprocess.CalledProcessError as e:
        if exit_on_fail:
            # Print a helpful message before exiting so users can diagnose failures
            # (e.g. running outside a git repository, missing permissions, etc.)
            print(f"\n[ContextLens Error] Command failed: {' '.join(cmd)}")
            if e.stderr and e.stderr.strip():
                print(f"  Reason: {e.stderr.strip()}")
            sys.exit(1)
        return ""
    except FileNotFoundError:
        if exit_on_fail:
            print(f"\n[ContextLens Error] Executable not found: {cmd[0]}")
            sys.exit(1)
        return ""


def get_git_diff_files(start_ref=None, end_ref=None):
    """Get all files modified in the specified commit range, or current diff.

    Args:
        start_ref (str, optional): Starting git ref.
        end_ref (str, optional): Ending git ref.

    Returns:
        list: List of modified files that exist.
    """
    if start_ref and end_ref:
        out = run_command(["git", "diff", "--name-only", start_ref, end_ref])
    else:
        out = run_command(["git", "diff", "--name-only", "HEAD"])
    return [f for f in out.splitlines() if f.strip() and os.path.exists(f)]


def get_git_tracked_files():
    """Get list of all git tracked files in the current repository.

    Returns:
        list: List of tracked file paths.
    """
    stdout = run_command(["git", "ls-files"], exit_on_fail=True)
    return [l.strip() for l in stdout.splitlines() if l.strip()]


class RipgrepChecker:  # pylint: disable=too-few-public-methods
    """Helper class to lazily check if ripgrep (rg) is installed on the system."""

    def __init__(self):
        """Initialize the checker with cached result set to None."""
        self._has_rg = None

    def __bool__(self):
        """Evaluate truthiness by checking if rg is present, warning once if missing."""
        if self._has_rg is None:
            try:
                self._has_rg = bool(run_command(["rg", "--version"], timeout=5.0))
            except Exception:  # pylint: disable=broad-exception-caught
                self._has_rg = False
            if not self._has_rg:
                warn_once(
                    "ripgrep_missing",
                    "ripgrep is not installed on the system. For significantly faster context "
                    "construction in large repositories, please install ripgrep (rg)."
                )
        return self._has_rg

HAS_RG = RipgrepChecker()


_PROGRESS_BAR_WIDTH = 25
_NORMALIZED_PATH_CACHE = {}


class FileScanCandidates(list):
    """List of files returned by ripgrep_filter with scan metadata attached."""

    def __init__(self, files, fallback_label=None):
        """Initialize candidate list.

        Args:
            files (iterable): File paths to scan.
            fallback_label (str, optional): Description for fallback scans.
        """
        super().__init__(files)
        self.fallback_label = fallback_label
        self.used_ripgrep_fallback = fallback_label is not None


class ScanProgressBar:  # pylint: disable=too-few-public-methods
    """Renders a lightweight progress indicator for long-running file scans.

    On interactive terminals (tty): rewrites the same line using carriage
    return so progress is shown without cluttering the log.
    On non-interactive output (CI, redirected): emits periodic status lines
    instead, avoiding escape sequences in log files.
    """

    def __init__(self, total, label, min_files=100):
        """Initialize the progress bar.

        Args:
            total (int): Total number of files to scan.
            label (str): Description shown alongside the progress indicator.
            min_files (int): Minimum file count required to activate the bar.
        """
        self.total = total
        self.label = label
        self.active = total > 0 and total >= min_files
        self._is_tty = (
            self.active
            and hasattr(sys.stderr, "isatty")
            and sys.stderr.isatty()
        )
        self._last_pct = -1
        self._last_count = 0
        # Emit roughly 20 periodic lines for non-tty output
        self._interval = max(1, total // 20) if self.active else 1

    def update(self, idx):
        """Update the progress indicator after processing the file at position idx.

        Args:
            idx (int): Zero-based index of the file just processed.
        """
        if not self.active:
            return
        if self._is_tty:
            pct = ((idx + 1) * 100) // self.total
            if pct != self._last_pct:
                filled = (pct * _PROGRESS_BAR_WIDTH) // 100
                progress_bar = "#" * filled + "-" * (_PROGRESS_BAR_WIDTH - filled)
                print(
                    f"\r  [{progress_bar}] {pct:3d}%  {self.label}  [{idx + 1}/{self.total}]",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                self._last_pct = pct
                self._last_count = idx + 1
        else:
            if idx % self._interval == 0:
                print(f"  [Scanning {idx + 1}/{self.total}]  {self.label}", file=sys.stderr)
                self._last_count = idx + 1

    def finish(self, completed=True):
        """Finalize the progress indicator after all files have been processed."""
        if not self.active:
            return
        if self._is_tty:
            if completed:
                progress_bar = "#" * _PROGRESS_BAR_WIDTH
                print(
                    f"\r  [{progress_bar}] 100%  {self.label}  [{self.total}/{self.total}]",
                    file=sys.stderr,
                )
            else:
                print(file=sys.stderr)
        elif completed and self._last_count != self.total:
            print(f"  [Scanning {self.total}/{self.total}]  {self.label}", file=sys.stderr)


def iter_scan_progress(files, label=None, min_files=100, force=False):
    """Yield files while displaying progress for long-running scans.

    Args:
        files (iterable): File paths to scan.
        label (str, optional): Description shown alongside progress.
        min_files (int): Minimum file count required to activate progress.
        force (bool): Show progress regardless of whether ripgrep fell back.

    Yields:
        str: File paths from files.
    """
    scan_files = files if isinstance(files, list) else list(files)
    fallback_label = getattr(scan_files, "fallback_label", None)
    progress_label = label or fallback_label
    should_show = force or bool(fallback_label)
    progress = ScanProgressBar(
        len(scan_files),
        progress_label or "Scanning files",
        min_files=min_files,
    )
    progress.active = progress.active and should_show
    completed = False
    try:
        for idx, file_path in enumerate(scan_files):
            yield file_path
            progress.update(idx)
        completed = True
    finally:
        progress.finish(completed=completed)


def _fallback_candidates(files, fallback_hint):
    """Return all files with metadata indicating ripgrep fallback was used."""
    return FileScanCandidates(files, fallback_hint)


def _get_cached_absolute_path(file_path, cwd):
    """Return a normalized absolute path, caching repeated repository paths."""
    cache_key = (cwd, file_path)
    abs_path = _NORMALIZED_PATH_CACHE.get(cache_key)
    if abs_path is None:
        abs_path = os.path.normcase(os.path.abspath(file_path))
        _NORMALIZED_PATH_CACHE[cache_key] = abs_path
    return abs_path


def _get_external_search_paths(files, cwd):
    """Return absolute candidate files that are outside the current directory."""
    external_paths = []
    for file_path in files:
        abs_path = _get_cached_absolute_path(file_path, cwd)
        try:
            if os.path.commonpath([cwd, abs_path]) != cwd:
                external_paths.append(file_path)
        except ValueError:
            external_paths.append(file_path)
    return external_paths


def _normalize_search_result(file_path, cwd):
    """Normalize a candidate or ripgrep result for reliable path comparison."""
    return _get_cached_absolute_path(file_path, cwd).replace("\\", "/")


def ripgrep_filter(files, token, fixed_strings=True, fallback_hint=None):
    """Filter list of files to only those containing the given token using ripgrep.

    Args:
        files (list): List of files to filter.
        token (str): Search token/pattern.
        fixed_strings (bool): Treat token as literal string instead of regex.
        fallback_hint (str, optional): Human-readable description of what is being
            searched (e.g. "callers of 'my_func'"). When provided, a prominent
            warning is printed whenever the fast ripgrep path is unavailable and
            the caller will fall back to an exhaustive scan.

    Returns:
        list: Filtered list of files.
    """
    if not files:
        return []
    if not HAS_RG:
        if fallback_hint:
            warn_once(
                "ripgrep_fallback",
                f"Fast-path search unavailable. Falling back to exhaustive repository scan "
                f"for {fallback_hint}. This may take a while in large repositories; "
                "progress will be shown for long scans.",
            )
        return _fallback_candidates(files, fallback_hint)
    from .config import CONFIG  # pylint: disable=import-outside-toplevel

    timeout = CONFIG.get("ripgrep_timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (timeout > 0):  # pylint: disable=superfluous-parens
        warn_once(
            "ripgrep_timeout_invalid",
            f"Configured ripgrep_timeout ({timeout}) must be a positive number. "
            "Falling back to default (10 seconds)."
        )
        timeout = 10
    try:
        cmd = ["rg", "-l"]
        if fixed_strings:
            # -F treats the token as a literal fixed string rather than a regular expression
            cmd.append("-F")
        cmd.append(token)
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        external_paths = _get_external_search_paths(files, cwd)
        has_local_candidates = len(external_paths) < len(files)
        cmd.append("--")
        if has_local_candidates:
            cmd.append(".")
        cmd.extend(external_paths)
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
        # rg exits with 0 if matches are found, 1 if no matches are found, and 2 if an error occurs.
        if res.returncode == 0:
            rg_files = {
                _normalize_search_result(file_path, cwd)
                for file_path in res.stdout.splitlines()
            }
            return [
                file_path
                for file_path in files
                if _normalize_search_result(file_path, cwd) in rg_files
            ]
        if res.returncode == 1:
            return []
        warn_once(
            "ripgrep_error",
            f"ripgrep exited with an unexpected return code {res.returncode}. "
            f"Stderr: {res.stderr.strip()}"
        )

    except subprocess.TimeoutExpired:
        warn_once(
            "ripgrep_timeout",
            f"ripgrep search timed out after {timeout} seconds. "
            "You can increase this limit by using the --ripgrep-timeout option "
            "or by setting 'ripgrep_timeout' in your config file."
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        warn_once(
            "ripgrep_fail",
            f"ripgrep failed unexpectedly: {exc}. Falling back to manual scanning."
        )
    # Fallback: rg failed or timed out; warn and return the unfiltered list so the
    # caller can proceed with an exhaustive scan rather than silently dropping results.
    if fallback_hint:
        warn_once(
            "ripgrep_fallback",
            f"Falling back to exhaustive repository scan for {fallback_hint}. "
            "This may take a while in large repositories; progress will be shown "
            "for long scans.",
        )
    return _fallback_candidates(files, fallback_hint)


def is_in_repo(file_path):
    """Check if file_path is within the current repository and is not ignored.

    Args:
        file_path (str): File path to verify.

    Returns:
        bool: True if the file exists and is in the repo, False otherwise.
    """
    if not file_path:
        return False
    # Normalize paths
    normalized = file_path.replace("\\", "/").lower()
    # Check ignore list patterns
    for pattern in ["node_modules/", "target/", ".git/", "/usr/include/", "/lib/", "sdk/"]:
        if pattern in normalized:
            return False
    try:
        abs_path = os.path.abspath(file_path)
        repo_root = os.path.abspath(".")
        # Safely verify if the file resides within the repository root using commonpath
        common = os.path.commonpath([repo_root, abs_path])
        # Compare normalized absolute paths case-insensitively for Windows compatibility
        return (
            os.path.abspath(common).lower() == repo_root.lower()
            and os.path.exists(file_path)
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def get_comment_prefix(file_path):
    """Return the correct comment prefix based on the file extension or name.

    Args:
        file_path (str): Path to the file.

    Returns:
        str: Comment prefix (e.g., "#", "//", "REM").
    """
    base = os.path.basename(file_path)
    if base.lower() == "makefile" or base.startswith("Makefile"):
        return "#"
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".py", ".sh", ".pl", ".mk", ".cmake"):
        return "#"
    if ext == ".bat":
        return "REM"
    return "//"
