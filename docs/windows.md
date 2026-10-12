# Native Windows (experimental)

This is a **limited, experimental native Windows port**, not macOS feature parity.
No live Windows installation, browser login, account switch, or terminal session has
been validated by the maintainers of this change. The Linux fixture tests are not
proof of Windows behavior. Keep existing Claude sessions open and unchanged while
trying the separate profile workflow below.

## Requirements

- Windows 10/11 and Windows PowerShell 5.1 or PowerShell 7.
- Native Windows Python 3.9+ with `venv` and `pip`, from
  [python.org](https://www.python.org/downloads/windows/). A current supported Python
  version is preferable; Python must have a matching `windows-curses` binary wheel.
- Claude Code's native Windows `claude.exe`, installed separately according to
  [Anthropic's setup instructions](https://code.claude.com/docs/en/setup#install-on-native-windows).
  This installer does not download Claude Code. Install any dependencies required
  by your Claude Code release, including Git for Windows where applicable.
- An ordinary interactive terminal. Windows Terminal is suggested for the TUI;
  redirected output, IDE terminal embedding, resizing, and Unicode glyph rendering
  still need live validation.

This is not the WSL route. Do not point native Windows tools at a WSL/macOS profile
folder or share credential files across operating systems.

## Install without elevation

Review the checkout and `install.ps1`, then run from its folder in PowerShell:

```powershell
.\install.ps1 -CheckOnly
.\install.ps1
```

If PowerShell blocks the script under your organization's policy, stop and use your
approved script-review/signing process. The installer does not change or bypass
execution policy, request Administrator rights, or install a startup service.

It discovers `py.exe -3`, then `python.exe`. To select an existing interpreter:

```powershell
.\install.ps1 -Python 'C:\Program Files\Python312\python.exe'
```

The install directory is Python's current-user home plus `.claude-profiles`
(normally `%USERPROFILE%\.claude-profiles`). Files installed there are:

- The explicit inventory of cockpit Python tools and `requirements-windows.txt`.
- `.venv`, an isolated virtual environment. System Python packages are untouched.
- `bin\claude-*.cmd`, quoted, relative-path launchers for PowerShell or CMD.
- `autoswitch.json`, created only if absent, with watching and swapping off.

The sole additional package is
[`windows-curses==2.4.2`](https://pypi.org/project/windows-curses/2.4.2/), installed
from the official PyPI index with binary wheels only and no dependency resolution.
No source builds are attempted. An unsupported Python version/architecture or a
blocked package download causes installation to fail before tool replacement. A
partially created `.venv` may remain; inspect it before removing/recreating it.
A successful repeat install preserves identical tool files and existing settings.
Differing tools require `-Update`; replaced files are backed up outside profiles
under `.claude-profiles-backups\<timestamp>`.

No persistent PATH or PowerShell profile change is made. Enable the commands for
this terminal only (or use the full path to a `.cmd` launcher):

```powershell
$env:PATH = "$HOME\.claude-profiles\bin;$env:PATH"
claude-verify
```

The installer prints the actual bin path if Python's home differs from `$HOME`.
To make the commands available in future terminals, add that exact bin directory
to your **user** PATH yourself using Windows Settings. No virtual-environment
activation or PowerShell activation script is needed.

Claude Code is resolved to `$HOME\.local\bin\claude.exe`. For a different native
installation, set the full path for the current shell:

```powershell
$env:COCKPIT_CLAUDE_BIN = 'C:\My Tools\Claude\claude.exe'
```

Use a native `.exe`, not an npm `.cmd` shim or a shell command string. Launcher
paths are quoted to allow spaces in usernames and installation directories.
Normal CMD/PowerShell argument escaping still applies to user-supplied arguments;
for complicated literal shell characters, call the venv Python entrypoint directly.

## Separate accounts, explicit browser login

```powershell
claude-new-profile personal
claude-login personal
claude-profiles
claude-as personal
```

Choose the correct account in Claude Code's browser consent screen. Login then
checks Claude Code's authentication status. No password, token, or existing
credential file is copied by the profile creator or installer. Claude Code itself
owns authentication and token renewal. Treat each profile directory as sensitive:
keep it out of repositories, shared folders, and unprotected backups. This port
relies on your Windows user-folder permissions; it does not change ACLs or promise
macOS Keychain-equivalent storage.

Unlike the macOS setup, a new Windows profile is **isolated**, with a fresh
`.claude.json`. It does not clone the default settings, MCP credentials, project
trust decisions, plugins, or history. It creates no symlinks, junctions, or hard
links. Configure trusted settings separately. Your old configuration and session
files remain where they are, and new sessions/history belong to the selected
profile. Review settings for secrets before manually transferring anything.

`claude-as` sets `CLAUDE_CONFIG_DIR` only for its child Claude process; it does not
pin the parent shell or retarget other sessions. Repeat the profile/login steps
for each account you own. Profile names accept ASCII letters, digits, `_`, and
`-`; Windows reserved device names are rejected. Existing profiles are never
recreated over their contents.

## Commands and limits

- `claude-cockpit`, `claude-usage`, `claude-usage --fast`: usage UI, refresh, and
  cached output. Live refresh depends on the installed Claude Code version's
  usage output; errors and stale data must remain visible.
- `claude-profiles`, `claude-new-profile`, `claude-login`, `claude-as`,
  `claude-verify`: explicit profile setup, authentication, launch, and diagnostics.
- `claude-switch <name>`: experimental explicit CLI browser sign-in with identity
  verification. There is no promise that already running Windows sessions adopt
  a changed login. Prefer a new `claude-as <name>` session. Check the verified
  identity after any sign-in.
- Cockpit number-key switching and undo are disabled on Windows until terminal
  hand-off/resume has been validated. Use the CLI separately.
- `claude-ping` sends a real prompt and consumes tokens. It is not part of install
  or verification; only run it deliberately.
- `claude-meters`: history/forecast processing is available, but Windows profile
  histories are separate. Do not assume default-profile token totals include all
  pinned Windows sessions.
- `claude-swap --check` performs a local credential-duplication check. Credential-
  moving swaps, swap dry-run plans, and automatic swaps are unavailable on Windows
  even if `swap_enabled` is edited. There is no Windows credential-migration
  implementation.
- macOS Keychain checks, Chrome-profile aiming, `open`, `osascript`, `launchd`, and
  the existing `.sh` install/uninstall/verify scripts are not Windows features.

## Optional foreground watcher

Nothing runs in the background after installation. The Windows watcher supports
foreground monitoring/log output only, with no automatic credential swap and no
macOS desktop notifications. To opt in, review `autoswitch.json`, set `enabled`
to JSON `true`, and run in a separate terminal:

```powershell
claude-autoswitch
```

Keep `swap_enabled` and `auto_swap` false. Stop with Ctrl+C; use
`claude-autoswitch --off` to turn watching off in settings. The watcher can read
usage over the network and write its usage history/logs. Check the runtime's
warnings before relying on it. No Scheduled Task, Windows service, startup folder
entry, or registry autorun is installed. `--once` is a single evaluation, not a
replacement for the persistent history/heartbeat loop. Do not build a scheduled
watcher around it assuming equivalent behavior.

## Update and remove

After reviewing a new checkout:

```powershell
.\install.ps1 -Update
```

Backups contain replaced tool/shim files, not profiles or credentials. Keep your
own secure data backup if needed. Updates preserve `autoswitch.json` and account
folders; they do not sign in, sign out, close sessions, or migrate account data.

There is no Windows automatic uninstaller in this first port. To stop using it,
close only the cockpit/watcher terminals and remove its bin directory from any
PATH setting you added. Leave profile folders in place to preserve sessions and
logins. To recover space, inspect the explicit inventory in `install.ps1` before
manually removing just tools, `bin`, and `.venv`. Do **not** delete the entire
`.claude-profiles` directory as an uninstall shortcut.

## Validation still required on Windows

Use a disposable Windows user/VM and fake credentials first. No paid CI is
required or configured by this change. Run `tests/test_windows_installer.ps1`
for parser checks and an offline install in a temporary fake user home. It uses
`-SkipDependencies`, an explicit offline/test option that skips PyPI and the
`curses` import check; normal installs must omit it. The test compiles a fake
Claude executable only if the local Windows C# compiler is already present.
It never signs in or invokes real Claude. Before calling this port stable:

1. Install under a username/path containing spaces; verify no elevation, shell
   profile/PATH edits, scheduled tasks, or credential modifications occurred.
2. Repeat installation; verify unchanged files/settings. Alter a harmless tool
   fixture, verify refusal without `-Update`, then verify its backup with `-Update`.
3. Check interrupted dependency download and unsupported wheel handling. Verify
   old tools/profiles still work and existing settings were not overwritten.
4. Create profiles with mock Claude, verify reserved/traversal names are rejected,
   pre-existing directories are preserved, and child argv/exit codes survive spaces.
5. With deliberate user consent, test a real browser login, auth status, cancelled
   login, wrong account, expired login, and usage errors. Never record secrets.
6. Test TUI resize, narrow width, Unicode, Ctrl+C/quit, and disabled switch keys.
7. Test foreground watcher stop/restart and confirm automatic swap stays blocked.
8. Confirm existing Claude sessions and macOS tests remain unaffected. Validate
   behavior again after Claude Code upgrades; its internals are not a stable API.
