"""Run only fixture tests, in fresh homes without subprocess/network access.

The macOS Keychain integration test is deliberately excluded.
"""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
POSIX_TESTS = ("test_meters.py", "test_swap_enabled.py", "test_autoswap.py", "test_fleet_dir.py")


def deny_external(event, args):
    if event in ("subprocess.Popen", "os.system", "os.posix_spawn", "socket.connect", "socket.bind", "socket.getaddrinfo"):
        raise RuntimeError("Offline test attempted external action: " + event)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        name = sys.argv[2]
        if name not in POSIX_TESTS + ("test_windows.py",):
            raise SystemExit("Test is not in the offline allowlist")
        sys.addaudithook(deny_external)
        sys.dont_write_bytecode = True
        if name == "test_windows.py":
            sys.argv = [str(ROOT / "tests" / name)]
            runpy.run_path(sys.argv[0], run_name="__main__")
        else:
            sys.argv = [str(ROOT / "tests" / "run_dev.py"), str(ROOT / "tests" / name)]
            runpy.run_path(sys.argv[0], run_name="__main__")
        return
    names = ("test_windows.py",) if sys.platform == "win32" else POSIX_TESTS + ("test_windows.py",)
    for name in names:
        with tempfile.TemporaryDirectory(prefix="cockpit offline ") as home:
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")}
            env.update(HOME=home, USERPROFILE=home, TMPDIR=home, TMP=home, TEMP=home,
                       PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", name],
                                    cwd=str(ROOT), env=env, check=False)
            if result.returncode:
                raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
