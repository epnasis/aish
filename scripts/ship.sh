#!/usr/bin/env bash
# Ship THIS checkout to the locally installed aish and restart the web service.
#
# Why this exists as a script rather than a command in a doc: `uv tool install`
# builds the wheel from the WORKING TREE, not from HEAD, so it silently
# packages whatever is uncommitted at that moment. On 2026-08-11 that nearly
# shipped another session's half-finished frontend — 129 uncommitted lines that
# were also failing two doc-gate tests — because the ship step was a bare
# command nobody could hang a check on. A guard needs somewhere to live.
#
#   scripts/ship.sh              # test, install, restart, health-check
#   scripts/ship.sh --no-test    # skip the suite (you just ran it)
#   scripts/ship.sh --dirty      # ship uncommitted work ON PURPOSE
#   scripts/ship.sh --check      # run the preflight only, change nothing
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.aish.web"
PORT="${AISH_PORT:-8787}"
MAIN_BRANCH="main"

run_tests=1
allow_dirty=0
check_only=0
for arg in "$@"; do
    case "$arg" in
        --no-test) run_tests=0 ;;
        --dirty) allow_dirty=1 ;;
        --check) check_only=1 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

cd "$PROJECT"

# ---------------------------------------------------------------- preflight

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "✗ not a git checkout: $PROJECT" >&2
    exit 1
fi

dirty="$(git status --porcelain)"
if [ -n "$dirty" ] && [ "$allow_dirty" -eq 0 ]; then
    # Name the files that would actually land in the wheel separately from the
    # rest: a modified README is untidy, a modified aish/ file is the bug this
    # guard exists for. A refusal that does not say which is which just trains
    # people to reach for --dirty.
    in_wheel="$(printf '%s\n' "$dirty" | awk '{print $NF}' | grep -E '^(aish/|pyproject\.toml$)' || true)"
    echo "✗ refusing to ship: the working tree is not clean." >&2
    echo >&2
    echo "  The wheel is built from the WORKING TREE, not from HEAD — these" >&2
    echo "  changes would ship even though they are not committed:" >&2
    echo >&2
    printf '%s\n' "$dirty" | sed 's/^/    /' >&2
    echo >&2
    if [ -n "$in_wheel" ]; then
        echo "  These would end up INSIDE the installed build:" >&2
        printf '%s\n' "$in_wheel" | sed 's/^/    /' >&2
    else
        echo "  (none of them land in the wheel, but the tree state is still" >&2
        echo "   unknown — commit or stash so what ships is what was tested.)" >&2
    fi
    echo >&2
    echo "  Commit them, stash them, or pass --dirty if you mean it." >&2
    exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "$MAIN_BRANCH" ]; then
    # A warning, not a refusal: shipping a branch to your own machine to try it
    # is legitimate. Shipping one by accident, having forgotten to merge, is not.
    echo "⚠ on branch '$branch', not '$MAIN_BRANCH' — shipping branch code."
fi

head_sha="$(git rev-parse --short HEAD)"
head_subject="$(git log -1 --pretty=%s)"
echo "→ shipping ${head_sha} ${head_subject}"
[ -n "$dirty" ] && echo "  ⚠ plus uncommitted changes (--dirty)"

# A word list aish LEANED ON and that never once matched — reported here and
# nowhere else that gets walked. `browse._FORWARD` sat at 7 asked / 0 matched
# for a month against `on_miss=breaks`, and the statistical detector stayed
# silent about it correctly: at that window's rarest working rate, 7 asks expect
# 0.09 matches and the bar is 1. It would have needed ~78 asks to trip, and a
# date picker is consulted seven times a month. `vocab.failing` needs no
# threshold because every consultation of a demanded list is one aish had
# already committed to needing an answer from.
#
# A WARNING, never a refusal. It is evidence about the last 30 days of browsing,
# not about the commit being shipped, so blocking on it would stop unrelated
# work and teach everyone to reach for an override.
broken="$(uv run --quiet python -c '
from aish import vocab
import aish.browse, aish.browser, aish.web, aish.approval, aish.signin  # noqa: F401
import aish.provenance, aish.agent  # noqa: F401
for one in vocab.failing(vocab.scan(days=30)):
    print(f"    {one.vocabulary} — {one.asked} asked, none matched")
' 2>/dev/null || true)"
if [ -n "$broken" ]; then
    echo "⚠ word lists aish leaned on and that never matched (\`aish vocab\`):"
    printf '%s\n' "$broken"
fi

if [ "$check_only" -eq 1 ]; then
    echo "✓ preflight passed (--check: nothing installed)"
    exit 0
fi

# ------------------------------------------------------------------- verify

if [ "$run_tests" -eq 1 ]; then
    echo "→ lint"
    uv run ruff check . >/dev/null
    uv run mypy >/dev/null
    echo "→ tests"
    uv run pytest -q >/dev/null
    echo "  passed"
fi

# ------------------------------------------------------------------ install

echo "→ installing"
uv tool install --force --reinstall --no-cache "$PROJECT" >/dev/null 2>&1
for exe in aish aish-web; do
    [ -x "$HOME/.local/bin/$exe" ] || { echo "✗ $exe missing after install" >&2; exit 1; }
done
echo "  installed"

# ------------------------------------------------------------------ restart

if ! launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null; then
    echo "  ${LABEL} not loaded — run scripts/install-web-service.sh once" >&2
    exit 1
fi
echo "→ restarted ${LABEL}"

# The service may bind one interface only (ours binds the LAN address, so
# probing 127.0.0.1 reports a false failure) — ask the kernel what it listens
# on, retrying while it comes back up.
for _ in $(seq 1 10); do
    # `awk … {exit}` closed the pipe while netstat was still writing, so netstat
    # died of SIGPIPE and `pipefail` made that 141 the SCRIPT's exit status: a
    # successful ship reported as a failed one, intermittently, depending on how
    # much netstat had buffered. Take the first match without closing the pipe.
    addr="$(netstat -an | awk "/\.${PORT}.*LISTEN/ && !seen++{print \$4}" | sed "s/\.${PORT}\$//")"
    if [ -n "$addr" ]; then
        [ "$addr" = "*" ] && addr=127.0.0.1
        code="$(curl -s -o /dev/null --connect-timeout 5 -w '%{http_code}' "http://${addr}:${PORT}/")"
        echo "  health (${addr}:${PORT}): HTTP ${code}"
        [ "$code" = "200" ] || { echo "✗ unhealthy" >&2; exit 1; }
        echo "✓ shipped ${head_sha}"
        exit 0
    fi
    sleep 1
done
echo "✗ no listener on ${PORT} after 10s — check ~/Library/Logs/aish-web.log" >&2
exit 1
