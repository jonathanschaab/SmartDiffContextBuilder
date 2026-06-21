"""Module sys_utils provides system utility functions for running commands and filtering files."""

import os
import stat
import subprocess
import sys
import time

from .config import CONFIG, DEFAULT_GIT_TIMEOUT
from .languages import get_language_profile

WARNED_MISSING_DEPS = set()

_IGNORED_DIRS = {
    "node_modules",
    "target",
    ".git",
    "sdk",
    "venv",
    ".venv",
    "env",
    "build",
    "out",
    "vendor",  # Go local dependencies
    "bin",     # C# / .NET compiled binaries
    "obj",     # C# / .NET intermediate object files
    ".gradle", # Java / Kotlin caches
}

_IGNORED_DIRS_CACHE = [None, None]


def warn_once(key, message):
    """Print a notice warning once per key to avoid stdout pollution.

    Args:
        key (str): Unique key for the warning.
        message (str): Warning message to display.
    """
    if key not in WARNED_MISSING_DEPS:
        print(f"\n[Notice] {message}")
        WARNED_MISSING_DEPS.add(key)


def validate_timeout_setting(value, default, config_key, cli_option):
    """Validate a positive numeric timeout and warn on invalid config values."""
    is_valid = (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value > 0
    )
    if is_valid:
        return value
    warn_once(
        f"{config_key}_invalid",
        f"Configured {config_key} ({value}) must be a positive number. "
        f"Falling back to {default} seconds. You can set this limit using "
        f"{cli_option} or by setting '{config_key}' in your config file.",
    )
    return default


def _build_git_env(extra_env=None):
    """Build a non-interactive environment for Git subprocesses."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    if extra_env:
        env.update(extra_env)
    return env


def run_git_process(
    cmd,
    timeout=None,
    timeout_key="git_timeout",
    timeout_option="--git-timeout",
    **kwargs,
):
    """Run a Git subprocess with non-interactive defaults and a timeout."""
    resolved_timeout = timeout
    if resolved_timeout is None:
        resolved_timeout = validate_timeout_setting(
            CONFIG.get(timeout_key, DEFAULT_GIT_TIMEOUT),
            DEFAULT_GIT_TIMEOUT,
            timeout_key,
            timeout_option,
        )
    extra_env = kwargs.pop("env", None)
    check = kwargs.pop("check", False)
    try:
        return subprocess.run(
            cmd,
            timeout=resolved_timeout,
            env=_build_git_env(extra_env),
            check=check,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        warn_once(
            timeout_key,
            f"git command timed out after {resolved_timeout} seconds. You can increase "
            f"this limit using {timeout_option} or by setting '{timeout_key}' in your "
            "config file.",
        )
        return None


def run_git_command(
    cmd,
    exit_on_fail=False,
    timeout=None,
    timeout_key="git_timeout",
    timeout_option="--git-timeout",
):
    """Run a Git command and return its standard output."""
    try:
        res = run_git_process(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=timeout,
            timeout_key=timeout_key,
            timeout_option=timeout_option,
        )
        if res is None:
            return ""
        return res.stdout
    except subprocess.CalledProcessError as e:
        if exit_on_fail:
            print(f"\n[SmartDiffContextBuilder Error] Command failed: {' '.join(cmd)}")
            if e.stderr and e.stderr.strip():
                print(f"  Reason: {e.stderr.strip()}")
            sys.exit(1)
        return ""
    except FileNotFoundError:
        if exit_on_fail:
            print(f"\n[SmartDiffContextBuilder Error] Executable not found: {cmd[0]}")
            sys.exit(1)
        return ""


def run_command(cmd, exit_on_fail=False, timeout=None):
    """Run a system command and return its standard output.

    Args:
        cmd (list): Command and arguments to run.
        exit_on_fail (bool): If True, exits program on process error or command missing.
        timeout (float, optional): Timeout in seconds.

    Returns:
        str: Decoded standard output.
    """
    if cmd and cmd[0] == "git":
        return run_git_command(cmd, exit_on_fail=exit_on_fail, timeout=timeout)
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
            print(f"\n[SmartDiffContextBuilder Error] Command failed: {' '.join(cmd)}")
            if e.stderr and e.stderr.strip():
                print(f"  Reason: {e.stderr.strip()}")
            sys.exit(1)
        return ""
    except FileNotFoundError:
        if exit_on_fail:
            print(f"\n[SmartDiffContextBuilder Error] Executable not found: {cmd[0]}")
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
_RG_COMMAND_MAX_CHARS = 24000
_NORMALIZED_PATH_CACHE = {}


def _stream_is_tty(stream):
    """Return whether a stream is interactive without trusting custom wrappers."""
    try:
        return bool(stream.isatty())
    except Exception:  # pylint: disable=broad-exception-caught
        return False


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
        self._is_tty = self.active and _stream_is_tty(sys.stderr)
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
    # Read metadata before materializing arbitrary iterables because converting
    # them to a plain list discards custom fallback progress attributes.
    fallback_label = getattr(files, "fallback_label", None)
    scan_files = files if isinstance(files, list) else list(files)
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
        abs_path = os.path.normcase(os.path.abspath(os.path.join(cwd, file_path)))
        _NORMALIZED_PATH_CACHE[cache_key] = abs_path
    return abs_path


def _normalize_search_result(file_path, cwd):
    """Normalize a candidate or ripgrep result for reliable path comparison."""
    return _get_cached_absolute_path(file_path, cwd).replace("\\", "/")


def _build_rg_file_batches(base_cmd, files, max_chars=_RG_COMMAND_MAX_CHARS):
    """Split explicit file arguments into command-line-length-safe batches."""
    base_length = len(subprocess.list2cmdline(base_cmd + ["--"]))
    batches = []
    current_batch = []
    current_length = base_length
    for file_path in files:
        arg_length = len(subprocess.list2cmdline([file_path])) + 1
        if current_batch and current_length + arg_length > max_chars:
            batches.append(current_batch)
            current_batch = []
            current_length = base_length
        current_batch.append(file_path)
        current_length += arg_length
    if current_batch:
        batches.append(current_batch)
    return batches


def _run_rg_batches(base_cmd, files, cwd, timeout):
    """Run explicit-file ripgrep batches and return normalized matches."""
    batches = _build_rg_file_batches(base_cmd, files)
    matched_files = set()
    deadline = time.monotonic() + timeout
    for batch_index, batch in enumerate(batches):
        batch_timeout = timeout if batch_index == 0 else deadline - time.monotonic()
        if batch_timeout <= 0:
            raise subprocess.TimeoutExpired(cmd=base_cmd, timeout=timeout)
        cmd = base_cmd + ["--"] + batch
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # A path with undecodable bytes should not crash the batch and force
            # an exhaustive scan. Replacement may prevent that one path from
            # matching exactly, but keeps all normally decoded results usable.
            errors="replace",
            check=False,
            timeout=batch_timeout,
        )
        # rg exits with 0 if matches are found, 1 if no matches are found,
        # and 2 if an error occurs.
        if res.returncode == 0:
            matched_files.update(
                _normalize_search_result(file_path, cwd)
                for file_path in res.stdout.splitlines()
            )
            continue
        if res.returncode == 1:
            continue
        warn_once(
            "ripgrep_error",
            f"ripgrep exited with an unexpected return code {res.returncode}. "
            f"Stderr: {res.stderr.strip()}"
        )
        return None
    return matched_files


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
    timeout = CONFIG.get("ripgrep_timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (timeout > 0):  # pylint: disable=superfluous-parens
        warn_once(
            "ripgrep_timeout_invalid",
            f"Configured ripgrep_timeout ({timeout}) must be a positive number. "
            "Falling back to default (10 seconds)."
        )
        timeout = 10
    try:
        base_cmd = ["rg", "-l"]
        if fixed_strings:
            # -F treats the token as a literal fixed string rather than a regular expression
            base_cmd.append("-F")
        base_cmd.append(token)
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        matched_files = _run_rg_batches(base_cmd, files, cwd, timeout)
        if matched_files is not None:
            return [
                file_path
                for file_path in files
                if _normalize_search_result(file_path, cwd) in matched_files
            ]

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


def is_in_repo(file_path):  # pylint: disable=too-many-branches
    """Check if file_path is within the current repository and is not ignored.

    Args:
        file_path (str): File path to verify.

    Returns:
        bool: True if the file exists and is in the repo, False otherwise.
    """
    from .path_utils import (  # pylint: disable=import-outside-toplevel
        detect_root_case_sensitivity,
        normalize_for_path_match,
        path_is_within_root,
        to_forward_slashes,
    )

    if not file_path:
        return False
    try:
        abs_path = os.path.abspath(file_path)
        repo_root = os.path.abspath(".")
        case_sensitive = detect_root_case_sensitivity(repo_root)

        # Check if the path is within the repository root
        if not path_is_within_root(abs_path, repo_root, case_sensitive=case_sensitive):
            return False

        try:
            st = os.stat(file_path)
            is_dir = stat.S_ISDIR(st.st_mode)
        except OSError:
            return False

        # Check exact directory component matches relative to the repository root.
        # If the path points to a file, the last component is the filename and is excluded.
        if not case_sensitive:
            rel_path = os.path.relpath(abs_path.lower(), repo_root.lower())
            normalized_rel = normalize_for_path_match(rel_path)
        else:
            rel_path = os.path.relpath(abs_path, repo_root)
            normalized_rel = to_forward_slashes(rel_path)

        components = normalized_rel.split("/")
        dir_components = components if is_dir else components[:-1]

        ignored_dirs_config = CONFIG.get("ignored_directories")
        config_key = (
            (tuple(ignored_dirs_config), case_sensitive)
            if isinstance(ignored_dirs_config, (list, tuple, set))
            else (None, case_sensitive)
        )

        if _IGNORED_DIRS_CACHE[0] == config_key:
            ignored_dirs = _IGNORED_DIRS_CACHE[1]
        else:
            if config_key[0] is not None:
                if not case_sensitive:
                    ignored_dirs = {str(d).lower() for d in ignored_dirs_config}
                else:
                    ignored_dirs = {str(d) for d in ignored_dirs_config}
            else:
                ignored_dirs = _IGNORED_DIRS
            _IGNORED_DIRS_CACHE[0] = config_key
            _IGNORED_DIRS_CACHE[1] = ignored_dirs

        if any(c in ignored_dirs for c in dir_components):
            return False

        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def get_comment_prefix(file_path):
    """Return the configured language profile's comment prefix.

    Args:
        file_path (str): Path to the file.

    Returns:
        str: Comment prefix (e.g., "#", "//", "REM").
    """
    return get_language_profile(file_path).comment_prefix
