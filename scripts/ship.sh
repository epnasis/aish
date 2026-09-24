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

# ------------------------------------------------- install, with the job out

# The install runs with the job OUT of launchd, not merely stopped. The service
# is KeepAlive, so while it is loaded launchd will respawn it whenever the
# process exits — and during `uv tool install` that means starting it against
# an env that is being replaced. On 2026-09-24 the log for the install window
# held exactly such respawns (an ImportError inside a half-written package,
# then "aish-web: No such file or directory"), and after the install and a
# `kickstart -k` the job sat in `spawn scheduled` past the health window. With
# the job booted out nothing can start it mid-install, and the bootstrap after
# the install loads a fresh job (its `runs` counter starts at 1 again) that
# starts because the plist says RunAtLoad — so no restart has to race anything.
#
# Two launchctl facts this leans on, both checked against a throwaway job:
#   - `bootout` returns 0 while the process is still exiting (`state =
#     SIGTERMed`, `print` still succeeds), and a `bootstrap` in that window
#     fails with "5: Input/output error". So we wait until `print` stops
#     finding the job before going on.
#   - `bootstrap` returns 0 even when the program it names does not exist; it
#     says the job is loaded, not that it runs. Only the health check says that.
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
# × 0.5 s. The plist sets no ExitTimeOut, so launchd's default applies; how long
# aish-web actually takes to exit was not measured — 60 s is a generous bound.
UNLOAD_TIMEOUT_POLLS=120
HEALTH_SECONDS=10

if [ ! -f "$PLIST" ]; then
    echo "✗ ${PLIST} not found — run scripts/install-web-service.sh once" >&2
    exit 1
fi

# loaded | gone | unknown. Only exit 113 ("Could not find service", observed)
# means gone: any other failure of `print` says nothing about the job, and
# reading it as gone would let a bootstrap race a job launchd still holds.
job_state() {
    local rc=0
    launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1 || rc=$?
    case "$rc" in
        0) echo loaded ;;
        113) echo gone ;;
        *) echo unknown ;;
    esac
}

# Poll until launchd no longer has the job; nonzero if it still does (or
# cannot be read) after UNLOAD_TIMEOUT_POLLS.
wait_until_gone() {
    for _ in $(seq 1 "$UNLOAD_TIMEOUT_POLLS"); do
        [ "$(job_state)" = gone ] && return 0
        sleep 0.5
    done
    [ "$(job_state)" = gone ]
}

report_launchd_state() {
    echo "  launchctl print ${DOMAIN}/${LABEL}:" >&2
    launchctl print "${DOMAIN}/${LABEL}" 2>&1 | grep -E "state|last exit" | sed 's/^[[:space:]]*/    /' >&2 || true
}

# The service may bind one interface only (ours binds the LAN address, so
# probing 127.0.0.1 reports a false failure) — ask the kernel what it listens
# on, retrying while it comes back up.
healthy() {
    local addr code
    for _ in $(seq 1 "$HEALTH_SECONDS"); do
        # `awk … {exit}` closed the pipe while netstat was still writing, so netstat
        # died of SIGPIPE and `pipefail` made that 141 the SCRIPT's exit status: a
        # successful ship reported as a failed one, intermittently, depending on how
        # much netstat had buffered. Take the first match without closing the pipe.
        addr="$(netstat -an | awk "/\.${PORT}.*LISTEN/ && !seen++{print \$4}" | sed "s/\.${PORT}\$//")"
        if [ -n "$addr" ]; then
            [ "$addr" = "*" ] && addr=127.0.0.1
            code="$(curl -s -o /dev/null --connect-timeout 5 -w '%{http_code}' "http://${addr}:${PORT}/" || true)"
            echo "  health (${addr}:${PORT}): HTTP ${code}"
            [ "$code" = "200" ] && return 0
            echo "✗ unhealthy" >&2
            report_launchd_state
            return 1
        fi
        sleep 1
    done
    echo "✗ no listener on ${PORT} after ${HEALTH_SECONDS}s — check ~/Library/Logs/aish-web.log" >&2
    report_launchd_state
    return 1
}

# Once the job is out, EVERY way this script ends must put it back — an
# install that failed must not become an outage. The EXIT trap is the one place
# that guarantees it, including for an interrupt, a hangup or a `set -e` stop.
job_out=0
install_log=""
restore_old_install() {
    # The bootstrap must run whatever else fails. Under `set -e` a failing
    # command in the trap ends the trap, and when the terminal has gone (the
    # hangup case) even an `echo` to it fails — so errors no longer stop
    # anything here, a write to a closed pipe returns instead of killing us, and
    # a second signal cannot cut the restore short (the wait in it is bounded).
    set +e
    trap '' PIPE INT TERM HUP
    [ -n "$install_log" ] && rm -f "$install_log"
    [ "$job_out" -eq 1 ] || return 0
    job_out=0
    echo "→ bringing ${LABEL} back on the install that is on disk now" >&2
    # An interrupt can land while the booted-out job is still exiting, and a
    # bootstrap in that window fails — so the same wait as before the install.
    if ! wait_until_gone; then
        echo "✗ ${LABEL} did not leave launchd — the service is DOWN. Once it has, recover with:" >&2
        echo "    launchctl bootstrap ${DOMAIN} ${PLIST}" >&2
        return 0
    fi
    # If uv had already replaced part of the old env before failing, what is on
    # disk is neither build, and this bootstrap loads a job that may not start.
    # The health check below is what says whether it did.
    if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
        echo "✗ could not load ${LABEL} again — the service is DOWN. Recover with:" >&2
        echo "    launchctl bootstrap ${DOMAIN} ${PLIST}" >&2
        return 0
    fi
    healthy || echo "✗ ${LABEL} is loaded but not serving — see above" >&2
}
trap restore_old_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

was_loaded=0
[ "$(job_state)" = gone ] || was_loaded=1
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
job_out=1
if [ "$was_loaded" -eq 1 ]; then
    if ! wait_until_gone; then
        # Nothing has been installed yet, so there is nothing to put back that
        # launchd has not still got; bootstrapping now would fail anyway.
        job_out=0
        echo "✗ ${LABEL} is still loaded (or unreadable) $((UNLOAD_TIMEOUT_POLLS / 2))s after bootout — nothing installed." >&2
        report_launchd_state
        echo "  Once \`launchctl print ${DOMAIN}/${LABEL}\` no longer finds it, recover with:" >&2
        echo "    launchctl bootstrap ${DOMAIN} ${PLIST}" >&2
        exit 1
    fi
    echo "→ stopped ${LABEL} (booted out of launchd for the install)"
else
    echo "→ ${LABEL} was not loaded — it will be loaded after the install"
fi

echo "→ installing"
install_log="$(mktemp -t aish-ship-install)"
if ! uv tool install --force --reinstall --no-cache "$PROJECT" >"$install_log" 2>&1; then
    echo "✗ uv tool install failed:" >&2
    tail -n 20 "$install_log" | sed 's/^/    /' >&2
    exit 1
fi
rm -f "$install_log"
install_log=""
for exe in aish aish-web; do
    [ -x "$HOME/.local/bin/$exe" ] || { echo "✗ $exe missing after install" >&2; exit 1; }
done
echo "  installed"

# ------------------------------------------------------------------ restart

job_out=0
if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
    echo "✗ launchctl bootstrap failed after a completed install — ${LABEL} is DOWN." >&2
    report_launchd_state
    echo "  Recover with:" >&2
    echo "    launchctl bootstrap ${DOMAIN} ${PLIST}" >&2
    exit 1
fi
echo "→ loaded ${LABEL}"

healthy || exit 1
echo "✓ shipped ${head_sha}"
