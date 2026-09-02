#!/usr/bin/env bash
#
# Runs every check in CLAUDE.md's "Verifying a change" table in one pass:
# ruff lint, ruff format check, mypy, pytest with coverage, and the frontend
# test + build. Not wired into CI yet (planning/TODO.md M-12) — this is the
# manual all-in-one a pre-push hook or a future workflow would call.
#
#     ./scripts/check.sh
#
# Every step runs even if an earlier one fails, so one pass reports
# everything that's broken instead of stopping at the first. Exits 1 if any
# step failed, 0 if all passed. Uses the repo's own .venv directly rather
# than relying on it being activated, so this also works from a shell that
# hasn't sourced it.

set -u

app="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$app/.venv/bin/python"
if [ ! -x "$python" ]; then
    python="$app/.venv/Scripts/python.exe"  # WSL against a Windows-created venv
fi
if [ ! -x "$python" ]; then
    echo "ERROR: no virtual environment at \"$app/.venv\". See README.md." >&2
    exit 1
fi

names=()
oks=()

run_step() {
    local name="$1"; shift
    local dir="$1"; shift
    echo ''
    echo "-- $name --"
    if (cd "$dir" && "$@"); then
        names+=("$name"); oks+=(1)
    else
        names+=("$name"); oks+=(0)
    fi
}

run_step "ruff check"        "$app"             "$python" -m ruff check pka tests scripts
run_step "ruff format check" "$app"             "$python" -m ruff format --check pka tests scripts
run_step "mypy"               "$app"             "$python" -m mypy pka
run_step "pytest --cov"       "$app"             "$python" -m pytest --cov=pka --cov-report=term-missing
run_step "npm run test"       "$app/frontend"    npm run test
run_step "npm run build"      "$app/frontend"    npm run build

echo ''
echo "-- Summary --"
failed=0
for i in "${!names[@]}"; do
    if [ "${oks[$i]}" -eq 1 ]; then
        echo "  PASS  ${names[$i]}"
    else
        echo "  FAIL  ${names[$i]}"
        failed=$((failed + 1))
    fi
done
echo ''

if [ "$failed" -gt 0 ]; then
    echo "$failed of ${#names[@]} checks failed."
    exit 1
fi
echo "All ${#names[@]} checks passed."
exit 0
