#!/bin/sh
# uninstall.sh: stops this install's reader server, removes the `reader` link
# from ~/.local/bin and deletes the state folder (the page key and pane files).
# It prints what it will remove first, is safe to run more than once, never
# touches Claude Code or cmux settings, and never deletes a file it does not
# recognise, so a mistyped READER_HOME cannot wipe an unrelated folder.
set -eu

REPO=$(cd "$(dirname "$0")" && pwd -P)
TARGET="$REPO/reader"
LINK="$HOME/.local/bin/reader"
# SYNC WARNING: these defaults repeat _port() and _state_dir() in reader.py.
STATE="${READER_HOME:-$HOME/.claude/reader}"
# A symlinked state folder would send the deletions below into whatever it points at.
if [ -L "$STATE" ]; then
  echo "uninstall: $STATE is a symbolic link; refusing to delete through it. Remove it by hand."
  exit 1
fi
PORT="${READER_PORT:-8791}"

echo "uninstall: this will"
echo "  1. stop the reader server on port $PORT, if this install started it"
echo "  2. remove $LINK, if it is a link to $TARGET"
echo "  3. delete $STATE (the page key, pane files, server.pid, server.log)"
echo ""

# 1. The launcher stops the server only if its pid matches this install's pid file.
if [ -x "$TARGET" ]; then
  "$TARGET" stop || echo "uninstall: the server was left running (see the message above)."
fi

# 2. Only our own link is removed; a different `reader` is left alone.
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$TARGET" ]; then
  rm "$LINK"
  echo "uninstall: removed $LINK"
elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
  echo "uninstall: $LINK is not this reader's link; left it alone."
else
  echo "uninstall: no link at $LINK."
fi

# 3. The state folder is deleted only if it holds nothing but reader's own files.
if [ ! -d "$STATE" ]; then
  echo "uninstall: no state folder at $STATE."
  echo "uninstall: done. You can now delete this folder: $REPO"
  exit 0
fi

unknown=0
report() {
  if [ "$unknown" -eq 0 ]; then
    echo "uninstall: $STATE holds files reader did not create, so nothing in it was deleted:"
  fi
  unknown=1
  echo "    $1"
}
for f in "$STATE"/* "$STATE"/.[!.]*; do
  [ -e "$f" ] || [ -L "$f" ] || continue
  case "$(basename "$f")" in
    key|server.pid|server.log|tickets) ;;
    panes) [ -L "$f" ] && report "$f (a link, not a folder)" ;;
    *) report "$f" ;;
  esac
done
if [ -d "$STATE/panes" ] && [ ! -L "$STATE/panes" ]; then
  for f in "$STATE/panes"/* "$STATE/panes"/.[!.]*; do
    [ -e "$f" ] || [ -L "$f" ] || continue
    case "$f" in
      *.json|*.json.tmp) ;;
      *) report "$f" ;;
    esac
  done
fi
if [ "$unknown" -ne 0 ]; then
  exit 1
fi

rm -f "$STATE/key" "$STATE/server.pid" "$STATE/server.log"
if [ -d "$STATE/tickets" ] && [ ! -L "$STATE/tickets" ]; then
  rm -f "$STATE/tickets"/*
  rmdir "$STATE/tickets"
fi
if [ -d "$STATE/panes" ]; then
  rm -f "$STATE/panes"/*.json "$STATE/panes"/*.json.tmp
  rmdir "$STATE/panes"
fi
rmdir "$STATE"
echo "uninstall: deleted $STATE"
echo "uninstall: done. You can now delete this folder: $REPO"
