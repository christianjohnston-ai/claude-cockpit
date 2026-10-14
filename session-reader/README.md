# Session Reader

A live page that shows your Claude Code session the way a chat app would: replies in a serif
reading font, real headings and tables, and tool calls folded into one tidy line per burst.
It runs in a browser pane inside cmux, right beside the session, and its decision cards can
type your answer back into the terminal.

<!-- TODO: demo GIF goes here. Record it from a throwaway session that shows no private work. -->
> **TODO:** demo GIF.

Not affiliated with or endorsed by Anthropic or Manaflow. Claude and Claude Code are products of
Anthropic; cmux is a product of Manaflow. This is an independent, free tool.

## What you get

- **Readable replies.** Markdown is drawn as a document: headings, lists, tables, code with
  syntax colours and a copy button.
- **Quiet tool calls.** A run of tool calls becomes one line, such as "Ran 3 commands, edited a
  file". Click it to see each step, with diffs, output and screenshots.
- **Decision cards.** When Claude asks you to choose in a set format, the question becomes a card
  with one button per option. Click once to arm, click again to send. See
  [docs/decision-cards.md](docs/decision-cards.md) for the format and a snippet for your CLAUDE.md.
- **It follows along.** The page updates as the session saves each step, and follows the session
  through `/clear`.

## Requirements

- macOS.
- [cmux](https://github.com/manaflow-ai/cmux), the macOS terminal built on Ghostty. The sidebar
  mode also needs cmux's Dock: **Settings, Beta Features, Dock** (it is off by default).
- Python 3.9 or newer at `/usr/bin/python3`. Macs with Apple's Command Line Tools have it; if
  not, run `xcode-select --install`. No packages to install.
- Claude Code, running inside cmux.

Tested with Claude Code 2.1.296, cmux 0.64.22, Python 3.9.6 and macOS 15.7.3.

## Install

1. Get the folder: clone the claude-cockpit repository you are reading (or download and unzip it)
   somewhere you will keep it, then `cd claude-cockpit/session-reader`. The reader lives in the same
   repository as the cockpit tools, but the two are independent and share no files.
2. Run `./install.sh` inside that folder. It links the `reader` command into `~/.local/bin` and changes
   nothing else. If that folder is not on your PATH, it prints the one line to add.
3. In a Claude Code session inside cmux, type at the prompt: `! reader here`

The `!` at the start runs a shell command from the Claude Code prompt. The first run starts a small
local server; later runs reuse it.

## The two commands

```
! reader          the sidebar: opens the reader in this cmux window's right-hand Dock
! reader here     a split beside this one session
```

**`! reader here`** opens a browser split to the right of the session you typed it in. That split
always shows that pane's session, including after `/clear`. When the session closes, the page says
"Session ended". Running it again refreshes the same split instead of opening a second one.

**`! reader`** opens the reader in cmux's Dock (the panel on the right of the window), stacked under
anything already there. One sidebar serves the whole window, and it decides what to show like this:

- It shows the Claude Code session in the tab that has focus in the focused cmux window.
- When focus moves to something that is not a Claude Code session (a shell, a browser, or the
  sidebar itself), it keeps showing the last session, so answering a card never switches it away.
- When the selected workspace has several Claude Code tabs, a row of small session tabs appears
  at the top, with a pulsing dot on any session that is working. Click one to read it.
- It finds sessions by asking cmux which terminal tabs the window has, then checking which of them
  runs a Claude Code process. A plain shell never appears and can never receive an answer.

Drag the Dock wider to read comfortably.

## Settings

All optional. Set them in your shell profile, so they are present when Claude Code runs `reader`.

| Variable | Default | What it does |
|---|---|---|
| `READER_PORT` | `8791` | The local port the server listens on. |
| `READER_HOME` | `~/.claude/reader` | Where the server keeps its key, pane files, pid file and log. Must be an absolute path. |
| `READER_CONFIG_DIRS` | (none) | Extra Claude Code config folders for the sidebar to search, separated by colons. `~/.claude` and `$CLAUDE_CONFIG_DIR` are always searched. |

The server keeps the settings it started with. After changing one, run `reader stop`, then
`! reader` again.

## Security and privacy

The reader can type into your terminal, so it is built to be boring about it.

- **Local only.** The server listens on `127.0.0.1` and refuses any request addressed to another
  host name, so a web page cannot reach it by pointing its own domain at your machine.
- **No telemetry.** It sends nothing anywhere. The page loads its libraries and fonts from the
  local server (with integrity hashes), never from a CDN. Its Content-Security-Policy stops the page
  from loading images or making background requests to any other site, even if a reply contains an
  image from the web. A link in a reply is plain text until you click it, and then it opens in a
  new tab like any link.
- **A key on every call, new on every start.** A random key, stored in `READER_HOME/key` (readable
  only by you, in a folder only you can open), is needed for every API call, and it changes each
  time the server starts. A key that something captured while the server was down is worthless once
  the server is back. A request without the key is refused.
- **The key is not put on a command line.** The address `reader` hands to cmux carries a one-time
  ticket that expires after 20 seconds, which the page swaps for the key once. Other accounts on
  the same Mac can read command lines with `ps`, so the key itself is never in one. The ticket is
  a private file in `READER_HOME/tickets`, not a request to the server, so a program that merely
  sits on the port never receives the key, and only a process running as you can redeem a ticket.
- **Answers need a custom header.** An answer must carry the key in an `X-Reader-Key` header.
  Another website cannot send that header without first asking permission (a CORS preflight), and
  the server never grants it. A request from another site's Origin is refused.
- **One short line at a time.** An answer is one line of 1 to 300 characters, and each pane
  accepts at most one answer every 2 seconds. Only visible characters are accepted: control
  characters, invisible and right-to-left marks, and a backslash are refused (cmux reads a
  backslash followed by n as the Enter key). A decision card types only its number and letter,
  never its label. Free text may not start with `!`, `/`, `&`, `#`, `@`, `$`, `~` or `-`, and may
  not be only a number, so a reader answer can never be a shell escape or a slash command.
- **Only to a Claude Code pane.** An answer goes only to a pane that ran `reader here`, or to a
  terminal tab cmux reported in the sidebar's window that runs Claude Code. Nothing is typed once
  that Claude process has exited, and nothing is typed unless Claude Code reports that it is idle at
  its prompt: while it is working, or showing a permission or question dialog (where a typed digit
  would pick a menu option), the cards are disabled and the server refuses. Before typing,
  the server asks cmux to confirm the pane is exactly the one registered, and it presses Enter
  only if cmux confirms the typing landed in that pane.
- **Replies cannot draw controls.** Text in a reply is untrusted, because a model may have read a hostile
  web page. The page removes buttons, forms, inputs, data attributes and styling classes from reply
  HTML, and only cards the page itself builds can be clicked into an answer.
- **Screenshots cannot run as pages.** Only PNG, JPEG, GIF and WebP images are served, each in a
  sandboxed response that allows no script.
- **Folded by default.** Tool output, diffs and screenshots stay folded until you click them, so
  nothing private appears on a livestream or screen recording by accident. Your home folder is
  shown as `~`.
- **Read-only on your sessions.** It reads Claude Code's session files and never writes them. It
  changes no Claude Code or cmux setting.

To check a running server against these rules (it sends only requests that must be refused, so
it never types anything):

```
/usr/bin/python3 -I tests/security_check.py
```

## Limits

- The transcript format is Claude Code's internal one, not a published contract. Every field is
  read defensively and anything unknown is skipped, but a Claude Code update can change what shows
  until the reader is updated.
- Replies appear as each step is saved, not word by word.
- The server must be started from inside cmux, because cmux only lets processes it started use
  its socket. After cmux restarts, answers stop working until you run `! reader` again, which
  replaces the server.
- macOS only.
- The page's script is inline, so the page's Content-Security-Policy is a second layer against
  injected script, not the first: the sanitizer is the first.
- A Claude process is identified by its process number and session file. If a session file were left
  behind and its number reused, the reader could treat another program in that pane as Claude. The
  pane check and the idle check make this unlikely, not impossible.
- A ticket sits in a command line for a moment. Other accounts cannot redeem it, because the server
  checks that the connecting process runs as you. Another program of yours that reads the process
  list in that moment could redeem it first, and those programs can read the key file anyway.
- On a Mac shared with other accounts, the state folder is private to you and the key is never on a
  command line, but anything running as you can read the key file; it is a local convenience tool,
  not a security boundary against your own programs.
- `READER_HOME/panes/` keeps a small file for each pane that ever ran `reader here`. They are
  harmless and safe to delete.

## Uninstall

```
./uninstall.sh
```

It prints what it will remove, stops this install's server, removes the `reader` link from
`~/.local/bin` (only if it points to this folder) and deletes `READER_HOME`. Then delete this
folder.

## Credits

Bundled, unmodified: [marked](https://github.com/markedjs/marked) (MIT),
[DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 or MPL-2.0),
[highlight.js](https://github.com/highlightjs/highlight.js) (BSD-3-Clause), and the fonts
[Lora](https://github.com/cyrealtype/Lora-Cyrillic), [Inter](https://github.com/rsms/inter) and
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) (SIL Open Font License 1.1). Full
licence texts are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This project is under the MIT licence; see [LICENSE](../LICENSE) at the top of the repository.
