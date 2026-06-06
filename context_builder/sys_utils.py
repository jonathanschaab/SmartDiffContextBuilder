import os
import subprocess
import sys

WARNED_MISSING_DEPS = set()

def warn_once(key, message):
    if key not in WARNED_MISSING_DEPS:
        print(f"\n[Notice] {message}")
        WARNED_MISSING_DEPS.add(key)

def run_command(cmd, exit_on_fail=False, timeout=None):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=timeout)
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
        return ""

def get_git_diff_files(start_ref=None, end_ref=None):
    # Gets all files modified in the specified commit range, or current diff
    if start_ref and end_ref:
        out = run_command(["git", "diff", "--name-only", start_ref, end_ref])
    else:
        out = run_command(["git", "diff", "--name-only", "HEAD"])
    return [f for f in out.splitlines() if f.strip() and os.path.exists(f)]

def get_git_tracked_files():
    stdout = run_command(["git", "ls-files"], exit_on_fail=True)
    return [l.strip() for l in stdout.splitlines() if l.strip()]

HAS_RG = bool(run_command(["rg", "--version"]))

def ripgrep_filter(files, token):
    try:
        res = subprocess.run(["rg", "-l", "-F", token], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        # rg exits with 0 if matches are found, 1 if no matches are found, and 2 if an error occurs.
        if res.returncode == 0:
            # Normalize path separators to forward slashes to prevent mismatches on Windows
            rg_files = {f.replace("\\", "/") for f in res.stdout.splitlines()}
            return [f for f in files if f.replace("\\", "/") in rg_files]
        elif res.returncode == 1:
            return []
    except Exception:
        pass
    # Fallback to scanning all files if ripgrep execution fails unexpectedly (e.g. timeout, missing binary, permissions)
    return files

def is_in_repo(file_path):
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
        return os.path.abspath(common).lower() == repo_root.lower() and os.path.exists(file_path)
    except Exception:
        return False
