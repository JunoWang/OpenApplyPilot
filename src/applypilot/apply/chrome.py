"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import hashlib
import json
import logging
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def find_chrome_profile_by_email(user_data_dir: Path, email: str) -> str | None:
    """Return the Chrome profile directory associated with ``email``.

    Chrome keeps this mapping in the root ``Local State`` file.  Only profile
    metadata is read here; cookies, passwords, and browsing data are never
    inspected.
    """
    if not email.strip():
        return None

    local_state_path = user_data_dir / "Local State"
    try:
        local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    target = email.strip().casefold()
    info_cache = local_state.get("profile", {}).get("info_cache", {})
    for directory, metadata in info_cache.items():
        if not isinstance(metadata, dict):
            continue
        account_email = str(metadata.get("user_name", "")).strip().casefold()
        if account_email == target and (user_data_dir / directory).is_dir():
            return directory
    return None


def _select_source_profile(user_data_dir: Path) -> tuple[str, str]:
    """Resolve the configured Chrome profile directory and account email."""
    profile = config.load_profile()
    browser = profile.get("browser", {})
    personal = profile.get("personal", {})
    requested_directory = str(browser.get("chrome_profile_directory", "")).strip()
    requested_email = str(
        browser.get("chrome_account_email") or personal.get("email") or ""
    ).strip()

    if requested_directory:
        if Path(requested_directory).name != requested_directory:
            raise ValueError("browser.chrome_profile_directory must be a Chrome profile directory name")
        if not (user_data_dir / requested_directory).is_dir():
            raise FileNotFoundError(
                f"Configured Chrome profile directory not found: {requested_directory}"
            )
        return requested_directory, requested_email

    if requested_email:
        matched = find_chrome_profile_by_email(user_data_dir, requested_email)
        if matched:
            return matched, requested_email
        if browser.get("chrome_account_email"):
            raise RuntimeError(
                "No local Chrome profile matches browser.chrome_account_email. "
                "Open Chrome with that account once or update profile.json."
            )

    if (user_data_dir / "Default").is_dir():
        return "Default", requested_email
    raise FileNotFoundError(f"No usable Chrome profile found under {user_data_dir}")


def setup_worker_profile(worker_id: int) -> tuple[Path, str]:
    """Create an isolated Chrome profile for a worker.

    On first run, clones only the configured account's profile from the user's
    real Chrome data. Subsequent runs reuse that account-specific worker copy.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Tuple of worker user-data directory and Chrome profile directory name.
    """
    source = config.get_chrome_user_data()
    source_profile, account_email = _select_source_profile(source)
    identity = hashlib.sha256(
        f"{source.resolve()}\0{source_profile}\0{account_email.casefold()}".encode()
    ).hexdigest()[:10]
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}-{identity}"
    marker_path = profile_dir / ".openapplypilot-profile.json"
    if marker_path.exists() and (profile_dir / source_profile).is_dir():
        return profile_dir, source_profile

    logger.info(
        "[worker-%d] Copying selected Chrome profile %s (first time setup)...",
        worker_id,
        source_profile,
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)

    # Copy only the browser state needed to preserve the selected account and
    # website sessions. Login Data and every other password-store file are
    # deliberately excluded.
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    root_items = [source / "Local State"]
    for item in root_items:
        if not item.exists():
            continue
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    source_profile_dir = source / source_profile
    destination_profile_dir = profile_dir / source_profile
    destination_profile_dir.mkdir(parents=True, exist_ok=True)
    session_items = (
        "Preferences",
        "Secure Preferences",
        "Cookies",
        "Cookies-journal",
        "Network",
        "Local Storage",
        "Session Storage",
        "WebStorage",
    )
    for name in session_items:
        item = source_profile_dir / name
        if not item.exists():
            continue
        dst = destination_profile_dir / name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item),
                    str(dst),
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("Cache", "Code Cache", "GPUCache"),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip files Chrome currently has locked

    if not (destination_profile_dir / "Preferences").is_file() or not (
        profile_dir / "Local State"
    ).is_file():
        raise RuntimeError(f"Could not copy selected Chrome profile: {source_profile}")
    marker_path.write_text(
        json.dumps({"source_profile": source_profile}, indent=2),
        encoding="utf-8",
    )
    marker_path.chmod(0o600)
    return profile_dir, source_profile


def _suppress_restore_nag(profile_dir: Path, profile_name: str = "Default") -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / profile_name / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir, profile_name = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir, profile_name)

    chrome_exe = config.get_chrome_path()

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if headless:
        cmd.append("--headless=new")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
