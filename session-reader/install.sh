#!/bin/sh
# install.sh: puts the `reader` command on your PATH by linking it into
# ~/.local/bin. It changes nothing else: no shell profile edits, no Claude Code
# settings, no cmux settings. Safe to run more than once.
set -eu

REPO=$(cd "$(dirname "$0")" && pwd -P)
BIN="$HOME/.local/bin"
LINK="$BIN/reader"
TARGET="$REPO/reader"

# The launcher's first line runs macOS's own /usr/bin/python3, so check that one.
# On a Mac without Apple's Command Line Tools, this may open Apple's installer.
if ! /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "install: reader needs Python 3.9 or newer at /usr/bin/python3."
  echo "         Install Apple's Command Line Tools with:  xcode-select --install"
  exit 1
fi

# A download that lost the executable bit would fail with "permission denied".
if [ ! -x "$TARGET" ]; then
  chmod u+x "$TARGET"
  echo "install: made $TARGET executable."
fi

if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$TARGET" ]; then
  echo "install: already installed: $LINK -> $TARGET"
elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
  # Never replace someone else's `reader`.
  echo "install: $LINK already exists and is not this reader; nothing was changed."
  echo "         Remove or rename it, then run ./install.sh again."
  exit 1
else
  mkdir -p "$BIN"
  ln -s "$TARGET" "$LINK"
  echo "install: linked $LINK -> $TARGET"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo ""
    echo "Note: $BIN is not on your PATH. Add this line to your shell profile (for zsh, ~/.zshrc),"
    echo "then open a new terminal and restart Claude Code:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

if ! command -v cmux >/dev/null 2>&1 && [ ! -x /Applications/cmux.app/Contents/Resources/bin/cmux ]; then
  echo ""
  echo "Note: cmux was not found. reader runs inside cmux (https://github.com/manaflow-ai/cmux)."
fi

echo ""
echo "Next: in a Claude Code session running inside cmux, type at the prompt:"
echo ""
echo "    ! reader here"
