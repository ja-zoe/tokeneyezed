#!/usr/bin/env bash
# Session-start sync check, run by the Claude Code and Codex SessionStart hooks.
# Fetches origin/main and reports whether this branch is behind it and whether the shared
# contracts changed there. Read-only: it never merges, rebases, or touches the working tree,
# and it always exits 0 so a network or auth problem never blocks a session.
set -u

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

if ! timeout 10 git fetch --quiet origin main 2>/dev/null; then
  echo "sync-check: could not fetch origin/main, so upstream changes were not checked."
  exit 0
fi

behind=$(git rev-list --count HEAD..origin/main 2>/dev/null) || exit 0
[ "$behind" -eq 0 ] && exit 0

echo "sync-check: this branch is $behind commit(s) behind origin/main."
echo "Rebase onto origin/main at the next task boundary (clean tree), then re-run the tests."
git log --oneline HEAD..origin/main | head -20

shared=(docs/contracts.md tests/INVARIANTS.md tests/contracts src/tokeneyezed/controller/ports.py)
changed=$(git diff --name-only HEAD...origin/main -- "${shared[@]}")
if [ -n "$changed" ]; then
  echo
  echo "sync-check: shared contracts changed on main. Check your work against them:"
  git diff HEAD...origin/main -- "${shared[@]}" | head -200
fi
exit 0
