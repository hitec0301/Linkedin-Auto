#!/usr/bin/env bash
#
# Diagnose (and optionally repair) the local Python environment.
#
#   bash scripts/doctor.sh          # report only, changes nothing
#   bash scripts/doctor.sh --fix    # rebuild the venv and reinstall
#
# Written in bash rather than Python on purpose: it has to run when the Python
# environment is too broken to import anything, which is exactly when you need
# it. It uses only what macOS ships with.

set -u

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)

ok()   { printf "  ok   %s\n" "$1"; }
warn() { printf "  --   %s\n" "$1"; }
bad()  { printf "  FAIL %s\n" "$1"; }
head_() { printf "\n== %s\n" "$1"; }

PROBLEMS=0
note_problem() { PROBLEMS=$((PROBLEMS + 1)); }

# --------------------------------------------------------------------------
head_ "Where we are"
# --------------------------------------------------------------------------
printf "  repo    %s\n" "$ROOT"
printf "  os      %s %s\n" "$(uname -s)" "$(uname -r)"

if [ -f requirements.txt ] && [ -d src/lnp ]; then
  ok "this is the project root"
else
  bad "requirements.txt or src/lnp is missing - are you in the right folder?"
  exit 1
fi

if [ -n "${VIRTUAL_ENV:-}" ]; then
  printf "  venv    active: %s\n" "$VIRTUAL_ENV"
else
  printf "  venv    not active in this shell\n"
fi

# --------------------------------------------------------------------------
head_ "Python interpreters available"
# --------------------------------------------------------------------------
BASE_PY=""
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
    printf "  %-12s %-10s %s\n" "$candidate" "${version:-unknown}" "$(command -v "$candidate")"
    [ -z "$BASE_PY" ] && [ -n "$version" ] && BASE_PY="$candidate"
  fi
done

if [ -z "$BASE_PY" ]; then
  bad "no usable python3 found on PATH"
  echo
  echo "  Install Python 3.12:  brew install python@3.12"
  echo "  (or from python.org). Then re-run this script."
  exit 1
fi
ok "using $BASE_PY to build the venv"

# --------------------------------------------------------------------------
head_ "Current venv health"
# --------------------------------------------------------------------------
VENV_PY="$ROOT/.venv/bin/python"
VENV_BROKEN=0

if [ ! -x "$VENV_PY" ]; then
  warn "no .venv yet (or it has no python)"
  VENV_BROKEN=1
else
  ok ".venv exists"
  if PIP_OUT=$("$VENV_PY" -m pip --version 2>&1); then
    ok "pip works: $PIP_OUT"
  else
    bad "pip is broken - it cannot even report its own version"
    echo "$PIP_OUT" | tail -3 | sed 's/^/       /'
    echo "       A broken pip cannot install anything, including a new pip."
    echo "       Rebuilding the venv is the only fix."
    VENV_BROKEN=1
    note_problem
  fi

  MISSING=""
  for module in yaml gspread anthropic feedparser rapidfuzz bs4 ulid dateutil requests; do
    "$VENV_PY" -c "import $module" >/dev/null 2>&1 || MISSING="$MISSING $module"
  done
  if [ -z "$MISSING" ]; then
    ok "all required packages import"
  else
    bad "packages missing or broken:$MISSING"
    VENV_BROKEN=1
    note_problem
  fi
fi

# --------------------------------------------------------------------------
if [ "$VENV_BROKEN" -eq 1 ] && [ "$FIX" -eq 0 ]; then
  head_ "What to do"
  echo "  The environment needs rebuilding. Re-run with --fix:"
  echo
  echo "      bash scripts/doctor.sh --fix"
  echo
  echo "  That deletes .venv and builds a fresh one. Nothing else is touched -"
  echo "  a venv holds only downloaded packages, no configuration of yours."
  exit 1
fi

if [ "$FIX" -eq 1 ]; then
  head_ "Rebuilding the venv"
  rm -rf "$ROOT/.venv"
  ok "removed the old .venv"

  if ! "$BASE_PY" -m venv --upgrade-deps "$ROOT/.venv" 2>/tmp/lnp_venv_err; then
    bad "could not create the venv"
    sed 's/^/       /' /tmp/lnp_venv_err
    exit 1
  fi
  ok "created a new venv with a current pip"

  printf "  installing dependencies (this takes a minute)...\n"
  if ! "$VENV_PY" -m pip install --no-cache-dir -q -r requirements.txt 2>/tmp/lnp_pip_err; then
    bad "dependency install failed"
    tail -20 /tmp/lnp_pip_err | sed 's/^/       /'
    exit 1
  fi
  ok "dependencies installed"

  MISSING=""
  for module in yaml gspread anthropic feedparser rapidfuzz bs4 ulid dateutil requests; do
    "$VENV_PY" -c "import $module" >/dev/null 2>&1 || MISSING="$MISSING $module"
  done
  if [ -z "$MISSING" ]; then
    ok "all packages import cleanly"
  else
    bad "still missing after install:$MISSING"
    exit 1
  fi
  # Everything diagnosed above has now been rebuilt; only failures found after
  # this point are still outstanding.
  PROBLEMS=0
fi

# --------------------------------------------------------------------------
head_ "Test suite"
# --------------------------------------------------------------------------
if TEST_OUT=$("$VENV_PY" -m pytest 2>&1); then
  ok "$(echo "$TEST_OUT" | grep -E "passed|failed" | tail -1)"
else
  bad "tests failed"
  echo "$TEST_OUT" | tail -15 | sed 's/^/       /'
  note_problem
fi

# --------------------------------------------------------------------------
head_ "Configuration"
# --------------------------------------------------------------------------
if [ -f .env ]; then
  ok ".env exists"
  for key in ANTHROPIC_API_KEY GOOGLE_SA_JSON SHEET_ID LINKEDIN_CLIENT_ID; do
    value=$(grep "^$key=" .env 2>/dev/null | head -1 | cut -d= -f2-)
    case "$value" in
      "")
        warn "$key is empty" ;;
      *...*)
        # .env.example ships placeholders like sk-ant-... - a placeholder that
        # reads as "set" is worse than an empty value, because it looks done.
        warn "$key still holds the example placeholder" ;;
      *)
        ok "$key is set" ;;
    esac
  done

  # A path that points at nothing is the most common GOOGLE_SA_JSON mistake.
  sa=$(grep "^GOOGLE_SA_JSON=" .env 2>/dev/null | head -1 | cut -d= -f2-)
  case "$sa" in
    "" | *...*) ;;
    "{"*) ok "GOOGLE_SA_JSON holds inline JSON" ;;
    *)
      if [ -f "$sa" ]; then
        ok "the service account key file exists at $sa"
      else
        bad "GOOGLE_SA_JSON points at $sa, which does not exist"
        note_problem
      fi ;;
  esac
else
  warn ".env not found - run: cp .env.example .env"
fi

# --------------------------------------------------------------------------
head_ "Google Sheets access"
# --------------------------------------------------------------------------
"$VENV_PY" scripts/setup_sheet.py --check 2>&1 | sed 's/^/  /'

# --------------------------------------------------------------------------
head_ "Summary"
# --------------------------------------------------------------------------
if [ "$PROBLEMS" -eq 0 ]; then
  echo "  The Python environment is healthy."
  echo
  echo "  Activate it in your shell with:"
  echo "      source .venv/bin/activate"
  echo
  echo "  Anything reported under 'Google Sheets access' above is about"
  echo "  credentials, not the install - see README section 2."
else
  echo "  $PROBLEMS problem(s) above still need attention."
fi
