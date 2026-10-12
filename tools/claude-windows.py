#!/usr/bin/env python3
"""Native Windows profile commands. Only Claude Code writes authentication data.

No shell, token copies, process discovery, global environment changes or symlinks.
The launcher also remains importable on non-Windows hosts for offline tests.
"""
import importlib.util
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "cswitch", os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude-switch.py"))
switch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(switch)


def profile_dir(name):
    if not switch.valid_name(name):
        raise ValueError("Use an ASCII profile name containing letters, digits, - or _; no Windows device names.")
    path = os.path.join(switch.BASE, name)
    if not os.path.isfile(os.path.join(path, ".claude.json")):
        raise ValueError("Unknown profile. First run: claude-new-profile " + name)
    return path


def new_profile(name):
    if not switch.valid_name(name):
        raise ValueError("Invalid profile name (Windows device names are not allowed).")
    os.makedirs(switch.BASE, exist_ok=True)
    path = os.path.join(switch.BASE, name)
    os.mkdir(path)  # exclusive; never overwrite a previous profile or follow its links
    # Deliberately isolated. A fresh config avoids copying API keys, MCP secrets,
    # account caches or a token file from another profile. Claude owns onboarding.
    config = os.path.join(path, ".claude.json")
    created_file = False
    try:
        with open(config, "x", encoding="utf-8") as f:
            created_file = True
            json.dump({}, f)
    except (OSError, ValueError):
        # Roll back only this new profile. Never remove a pre-existing directory,
        # linked path or another file that appeared while we were creating it.
        if not os.path.islink(path):
            if created_file and not os.path.islink(config):
                try:
                    os.unlink(config)
                except OSError:
                    pass
            try:
                os.rmdir(path)  # succeeds only if empty, never recursively deletes
            except OSError:
                pass
        raise
    print(f"Created isolated profile {name}. Next: claude-login {name}")
    return 0


def profile_env(name):
    return dict(os.environ, CLAUDE_CONFIG_DIR=profile_dir(name))


def login(name):
    switch.require_claude()
    if name == "--fleet":
        env = switch.fleet_env(dict(os.environ))
        path = switch.FLEET_DIR
        print("Signing into the default login. Running sessions are not switched or stopped.")
    else:
        env = profile_env(name)
        path = env["CLAUDE_CONFIG_DIR"]
    # Do not set a macOS BROWSER helper. The official CLI handles browser/callback.
    rc = subprocess.run([switch.CLAUDE_BIN, "auth", "login"], env=env).returncode
    if rc != 0:
        print("Login was interrupted or failed; identity was not verified. Run claude-profiles to inspect it.")
        return rc
    email = switch.authed_email(path)
    if not email:
        print("Login returned, but account identity could not be verified. Run claude-profiles.")
        return 4
    print(f"Verified {name}: {email}")
    return 0


def launch(name, args):
    switch.require_claude()
    return subprocess.run([switch.CLAUDE_BIN] + list(args), env=profile_env(name)).returncode


def verify():
    """Local checks only; no CLI launch, network or token output."""
    ok = True
    executable = switch.CLAUDE_BIN
    exists = os.path.isabs(executable) and os.path.isfile(executable) and executable.lower().endswith(".exe")
    print(("PASS" if exists else "FAIL") + ": native Claude executable")
    ok &= exists
    try:
        import curses  # noqa: F401
        print("PASS: curses import")
    except ImportError:
        print("FAIL: install windows-curses in the cockpit Python environment")
        ok = False
    for name in switch.profiles():
        try:
            data = switch.read_windows_credential(switch.slot_for(profile_dir(name)))
            print(("PASS" if (data or {}).get("claudeAiOauth") else "WARN") + ": " + name + " login store")
        except (switch.CredentialReadError, ValueError):
            print("FAIL: " + name + " has an unreadable login store")
            ok = False
    print("INFO: hot-swap and cockpit login keys disabled; native interactive validation pending")
    return 0 if ok else 1


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    if not switch.IS_WINDOWS:
        print("This launcher is for native Windows. Use the shell tools on macOS.", file=sys.stderr)
        return 1
    try:
        if args == ["profiles"]:
            switch.require_claude()
            for name in switch.profiles():
                email = switch.authed_email(profile_dir(name))
                print(f"{name}: {email or 'signed out or unverified'}")
            print("default: " + (switch.fleet_email() or "signed out or unverified"))
            return 0
        if args == ["verify"]:
            return verify()
        if len(args) == 2 and args[0] == "new-profile":
            return new_profile(args[1])
        if len(args) == 2 and args[0] == "login":
            return login(args[1])
        if len(args) >= 2 and args[0] == "as":
            return launch(args[1], args[2:])
        print("Usage: claude-windows.py new-profile NAME | login NAME/--fleet | as NAME [args...] | profiles | verify")
        return 1
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, switch.CredentialReadError) as error:
        # Do not print raw credential JSON or subprocess stdout on failures.
        print("Profile command failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
