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
    except subprocess.CalledProcessError:
        if exit_on_fail: sys.exit(1)
        return ""
    except FileNotFoundError:
        return ""

def get_git_diff_files():
    # Gets all files modified in the current diff
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
            rg_files = set(res.stdout.splitlines())
            return [f for f in files if f in rg_files]
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
        abs_path = os.path.abspath(file_path).lower()
        repo_root = os.path.abspath(".").lower()
        # Ensure it resides inside the repository root workspace
        return abs_path.startswith(repo_root) and os.path.exists(file_path)
    except Exception:
        return False
