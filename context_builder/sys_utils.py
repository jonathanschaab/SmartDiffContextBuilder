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

def ripgrep_filter(files, token, fixed_strings=True):
    """Filter list of files to only those containing the given token using ripgrep.

    Args:
        files (list): List of files to filter.
        token (str): Search token/pattern.
        fixed_strings (bool): Treat token as literal string instead of regex.

    Returns:
        list: Filtered list of files.
    """
    if not files:
        return []
    if not HAS_RG:
        return files
    from .config import CONFIG  # pylint: disable=import-outside-toplevel

    timeout = CONFIG.get("ripgrep_timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
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
            # Normalize path separators to forward slashes to prevent mismatches on Windows
            rg_files = {f.replace("\\", "/") for f in res.stdout.splitlines()}
            return [f for f in files if f.replace("\\", "/") in rg_files]
        if res.returncode == 1:
            return []
        if res.returncode == 2:
            warn_once(
                "ripgrep_error",
                f"ripgrep exited with an error code 2. Stderr: {res.stderr.strip()}"
            )
    except FileNotFoundError:
        warn_once(
            "ripgrep_missing",
            "ripgrep is not installed on the system. For significantly faster context "
            "construction in large repositories, please install ripgrep (rg)."
        )
    except subprocess.TimeoutExpired:
        warn_once(
            "ripgrep_timeout",
            f"ripgrep search timed out after {timeout} seconds. "
            "You can increase this limit by using the --ripgrep-timeout option "
            "or by setting 'ripgrep_timeout' in your config file."
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        warn_once(
            "ripgrep_fail",
            f"ripgrep failed unexpectedly: {e}. Falling back to manual scanning."
        )
    # Fallback to scanning all files if ripgrep execution fails unexpectedly
    return files


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
