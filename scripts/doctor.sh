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
VENV_PY="$ROOT/.venv/bin/python"

ok()    { printf "  ok   %s\n" "$1"; }
warn()  { printf "  --   %s\n" "$1"; }
bad()   { printf "  FAIL %s\n" "$1"; }
head_() { printf "\n== %s\n" "$1"; }

PROBLEMS=0
note_problem() { PROBLEMS=$((PROBLEMS + 1)); }

MODULES="yaml gspread anthropic feedparser rapidfuzz bs4 ulid dateutil requests"

# Import name -> distribution name, for the ones that differ.
dist_for() {
  case "$1" in
    yaml)     echo "PyYAML" ;;
    bs4)      echo "beautifulsoup4" ;;
    dateutil) echo "python-dateutil" ;;
    ulid)     echo "ulid-py" ;;
    *)        echo "$1" ;;
  esac
}

# Report which modules fail AND why. "Missing or broken" without the exception
# is the difference between diagnosing this in one run and in five.
FAILED_MODULES=""
check_imports() {
  FAILED_MODULES=""
  for module in $MODULES; do
    if err=$("$VENV_PY" -c "import $module" 2>&1); then
      :
    else
      FAILED_MODULES="$FAILED_MODULES $module"
      if [ "${1:-quiet}" = "verbose" ]; then
        printf "       %-12s %s\n" "$module" "$(echo "$err" | tail -1)"
      fi
    fi
  done
  [ -z "$FAILED_MODULES" ]
}

# Get a current pip wheel, then run it off sys.path to install itself. A pip
# wheel is executable that way, so replacing a bad or ancient pip never asks
# that pip to do the work.
#
# Three download routes, because each fails in a different real situation:
#   curl        - uses the system trust store, so it works on python.org builds
#                 whose stdlib has no CA certificates configured
#   pip download - the old pip may be too old to install modern wheels but can
#                 still fetch one, and it bundles its own certifi
#   urllib      - last resort, needs the stdlib's SSL trust to be set up
PIP_WHEEL=""
SSL_BROKEN=0

fetch_pip_wheel() {
  DL=$(mktemp -d)
  if command -v curl >/dev/null 2>&1; then
    url=$(curl -fsSL --max-time 60 https://pypi.org/pypi/pip/json 2>/dev/null \
      | "$BASE_PY" -c 'import json,sys; d=json.load(sys.stdin); print(next(u["url"] for u in d["urls"] if u["packagetype"]=="bdist_wheel"))' 2>/dev/null)
    if [ -n "$url" ] && curl -fsSL --max-time 120 -o "$DL/${url##*/}" "$url" 2>/dev/null; then
      PIP_WHEEL="$DL/${url##*/}"; printf "    fetched with curl\n"; return 0
    fi
  fi
  if [ -x "$VENV_PY" ] && "$VENV_PY" -m pip download --quiet --no-deps --only-binary :all: \
       --dest "$DL" pip >/tmp/lnp_dl_err 2>&1; then
    PIP_WHEEL=$(ls "$DL"/pip-*.whl 2>/dev/null | head -1)
    if [ -n "$PIP_WHEEL" ]; then printf "    fetched with the existing pip\n"; return 0; fi
  fi
  if "$BASE_PY" - "$DL" <<'FETCH' 2>/tmp/lnp_boot_err
import json, os, sys, urllib.request
meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/pip/json", timeout=60))
url = next(u["url"] for u in meta["urls"] if u["packagetype"] == "bdist_wheel")
urllib.request.urlretrieve(url, os.path.join(sys.argv[1], url.rsplit("/", 1)[-1]))
FETCH
  then
    PIP_WHEEL=$(ls "$DL"/pip-*.whl 2>/dev/null | head -1)
    [ -n "$PIP_WHEEL" ] && { printf "    fetched with urllib\n"; return 0; }
  fi
  grep -q "CERTIFICATE_VERIFY_FAILED" /tmp/lnp_boot_err 2>/dev/null && SSL_BROKEN=1
  return 1
}

bootstrap_pip() {
  printf "  fetching a current pip from PyPI...\n"
  fetch_pip_wheel || return 1
  # Download first, recreate second: the old pip may be one of the download
  # routes, so deleting it before fetching would remove a working option.
  rm -rf "$ROOT/.venv"
  "$BASE_PY" -m venv --without-pip "$ROOT/.venv" || return 1
  "$VENV_PY" "$PIP_WHEEL/pip" install --no-cache-dir --quiet "$PIP_WHEEL" 2>/tmp/lnp_boot_err
}

# --------------------------------------------------------------------------
head_ "Where we are"
# --------------------------------------------------------------------------
printf "  repo    %s\n" "$ROOT"
printf "  os      %s %s\n" "$(uname -s)" "$(uname -m)"

if [ -f requirements.txt ] && [ -d src/lnp ]; then
  ok "this is the project root"
else
  bad "requirements.txt or src/lnp is missing - are you in the right folder?"
  exit 1
fi
[ -n "${VIRTUAL_ENV:-}" ] && printf "  venv    active: %s\n" "$VIRTUAL_ENV"

# --------------------------------------------------------------------------
head_ "Python interpreters available"
# --------------------------------------------------------------------------
# Modern `packaging` - which pip, and therefore every install, depends on -
# matches version strings with a regex using possessive quantifiers and scoped
# inline flags. Some Python builds mis-compile that pattern and then reject
# perfectly valid versions, which makes pip unusable no matter how it is
# installed. Test each interpreter before trusting it.
interpreter_is_sane() {
  "$1" - <<'SANITY' >/dev/null 2>&1
import re, sys
# packaging's VERSION_PATTERN, verbatim. Using the real thing rather than an
# abbreviation means a Python that passes this test can definitely compile what
# pip compiles.
pattern = r"""
    v?+                                                   # optional leading v
    (?a:
        (?:(?P<epoch>[0-9]+)!)?+                          # epoch
        (?P<release>[0-9]+(?:\.[0-9]+)*+)                 # release segment
        (?P<pre>                                          # pre-release
            [._-]?+
            (?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)
            [._-]?+
            (?P<pre_n>[0-9]+)?
        )?+
        (?P<post>                                         # post release
            (?:-(?P<post_n1>[0-9]+))
            |
            (?:
                [._-]?
                (?P<post_l>post|rev|r)
                [._-]?
                (?P<post_n2>[0-9]+)?
            )
        )?+
        (?P<dev>                                          # dev release
            [._-]?+
            (?P<dev_l>dev)
            [._-]?+
            (?P<dev_n>[0-9]+)?
        )?+
    )
    (?a:\+
        (?P<local>                                        # local version
            [a-z0-9]+
            (?:[._-][a-z0-9]+)*+
        )
    )?+
"""
try:
    rx = re.compile(r"^\s*" + pattern + r"\s*$", re.VERBOSE | re.IGNORECASE)
except Exception:
    sys.exit(1)
# "0.dev0" is the literal value modern packaging evaluates at import time.
sys.exit(0 if all(rx.search(v) for v in ("0.dev0", "1.2.3", "2.34.2", "6.0.3")) else 1)
SANITY
}

BASE_PY=""
BROKEN_PYS=""
for candidate in python3.12 python3.13 python3.11 python3; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  version=$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
  [ -z "$version" ] && continue
  if interpreter_is_sane "$candidate"; then
    note="usable"
    [ -z "$BASE_PY" ] && BASE_PY="$candidate"
  else
    note="UNUSABLE - its re module rejects valid version strings"
    BROKEN_PYS="$BROKEN_PYS $candidate($version)"
  fi
  printf "  %-12s %-10s %-28s %s\n" "$candidate" "$version" "$(command -v "$candidate")" "$note"
done

if [ -n "$BROKEN_PYS" ]; then
  echo
  warn "these interpreters cannot run modern pip:$BROKEN_PYS"
  warn "packaging matches versions with possessive quantifiers; a Python whose"
  warn "re module mishandles them rejects strings like '0.dev0', so pip fails"
  warn "on import. No pip version fixes this - the interpreter has to change."
fi

if [ -z "$BASE_PY" ]; then
  bad "no usable Python found - every interpreter on PATH has the defect above"
  echo
  echo "      brew install python@3.12"
  echo "      bash scripts/doctor.sh --fix"
  echo
  echo "  (or install a current 3.12.x / 3.13.x from python.org)"
  exit 1
fi
ok "using $BASE_PY to build the venv"

BASE_VERSION=$("$BASE_PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')

printf "  base pip: %s\n" "$("$BASE_PY" -m pip --version 2>&1 | tail -1)"
printf "  shadowing env: PYTHONPATH=[%s] PIP_TARGET=[%s] PIP_CONFIG_FILE=[%s]\n" \
  "${PYTHONPATH:-}" "${PIP_TARGET:-}" "${PIP_CONFIG_FILE:-}"

# --------------------------------------------------------------------------
head_ "Current venv health"
# --------------------------------------------------------------------------
VENV_BROKEN=0
if [ ! -x "$VENV_PY" ]; then
  warn "no .venv yet"
  VENV_BROKEN=1
else
  ok ".venv exists"
  if PIP_OUT=$("$VENV_PY" -m pip --version 2>&1); then
    ok "pip works: $PIP_OUT"
  else
    bad "pip is broken - it cannot report its own version"
    echo "$PIP_OUT" | tail -2 | sed 's/^/       /'
    VENV_BROKEN=1
    note_problem
  fi
  if check_imports verbose; then
    ok "all required packages import"
  else
    bad "these do not import:$FAILED_MODULES"
    VENV_BROKEN=1
    note_problem
  fi
fi

if [ "$VENV_BROKEN" -eq 1 ] && [ "$FIX" -eq 0 ]; then
  head_ "What to do"
  echo "  Re-run with --fix:"
  echo
  echo "      bash scripts/doctor.sh --fix"
  exit 1
fi

# --------------------------------------------------------------------------
if [ "$FIX" -eq 1 ]; then
  head_ "Rebuilding the venv"

  # Deliberately not --upgrade-deps: that makes pip upgrade itself in place,
  # and an interrupted in-place self-upgrade leaves pip/_vendor/packaging
  # holding files from two versions, which is a pip that cannot run at all.
  rm -rf "$ROOT/.venv"
  "$BASE_PY" -m venv "$ROOT/.venv" 2>/tmp/lnp_venv_err || {
    bad "could not create the venv"; sed 's/^/       /' /tmp/lnp_venv_err; exit 1; }
  ok "created a new venv"

  NEED_PIP=0
  if [ "${LNP_FORCE_PIP_BOOTSTRAP:-0}" = "1" ]; then
    warn "LNP_FORCE_PIP_BOOTSTRAP set"
    NEED_PIP=1
  elif PIP_OUT=$("$VENV_PY" -m pip --version 2>&1); then
    PIP_MAJOR=$(echo "$PIP_OUT" | awk '{print $2}' | cut -d. -f1)
    if [ "${PIP_MAJOR:-0}" -lt 24 ] 2>/dev/null; then
      # Python 3.12.0 seeds pip 23.1.2, which predates current wheel metadata
      # and can resolve to a source build where a wheel exists - producing
      # C extensions built against the wrong toolchain, which install happily
      # and then fail on import.
      warn "seeded pip is old ($PIP_OUT)"
      NEED_PIP=1
    else
      ok "seeded pip is current enough: $PIP_OUT"
    fi
  else
    bad "the freshly seeded pip is broken too - the damage is in your Python install"
    echo "$PIP_OUT" | tail -2 | sed 's/^/       /'
    NEED_PIP=1
  fi

  if [ "$NEED_PIP" -eq 1 ]; then
    if bootstrap_pip; then
      ok "bootstrapped pip: $("$VENV_PY" -m pip --version 2>&1 | tail -1)"
    else
      bad "could not bootstrap pip"
      tail -6 /tmp/lnp_boot_err | sed 's/^/       /'
      if [ "$SSL_BROKEN" -eq 1 ]; then
        echo
        echo "       Your Python has no CA certificates, so it cannot verify HTTPS."
        echo "       python.org builds ship a script to install them. Find it with:"
        echo
        echo "           find /Applications /Library/Frameworks/Python.framework \\"
        echo "                -name 'Install Certificates.command' 2>/dev/null"
        echo
        echo "       If it is not there, do the same thing by hand - it only points"
        echo "       Python at certifi's CA bundle:"
        echo
        echo "           $BASE_PY -m pip install --upgrade certifi"
        echo "           $BASE_PY -c \"import os,ssl,certifi; d=ssl.get_default_verify_paths().openssl_cafile; os.path.lexists(d) and os.remove(d); os.symlink(certifi.where(), d); print('linked', d)\""
        echo
        echo "       (prefix both with sudo if it reports a permissions error)"
        echo
        echo "       This is optional for this project - the fetch above prefers"
        echo "       curl, which uses the system trust store instead."
      else
        echo "       Reinstall Python:  brew install python@3.12"
      fi
      exit 1
    fi
  fi

  printf "  installing dependencies (this takes a minute)...\n"
  if "$VENV_PY" -m pip install --no-cache-dir -r requirements.txt >/tmp/lnp_pip.log 2>&1; then
    ok "pip install reported success"
  else
    bad "pip install failed"
    tail -25 /tmp/lnp_pip.log | sed 's/^/       /'
    exit 1
  fi

  if check_imports verbose; then
    ok "all packages import cleanly"
  else
    # Installed but not importable almost always means a C extension built
    # from source against the wrong toolchain. Force prebuilt wheels only.
    warn "installed, but these do not import:$FAILED_MODULES"
    DISTS=""
    for module in $FAILED_MODULES; do DISTS="$DISTS $(dist_for "$module")"; done
    printf "  retrying those as prebuilt wheels only:%s\n" "$DISTS"
    if "$VENV_PY" -m pip install --no-cache-dir --force-reinstall \
         --only-binary :all: $DISTS >/tmp/lnp_retry.log 2>&1; then
      ok "reinstalled from wheels"
    else
      bad "wheel-only reinstall failed - there may be no wheel for your Python"
      tail -20 /tmp/lnp_retry.log | sed 's/^/       /'
    fi

    if check_imports verbose; then
      ok "all packages import cleanly now"
    else
      bad "still failing:$FAILED_MODULES"
      echo
      echo "       Versions actually installed:"
      for module in $FAILED_MODULES; do
        "$VENV_PY" -m pip show "$(dist_for "$module")" 2>/dev/null \
          | awk '/^(Name|Version|Location)/' | sed 's/^/         /'
      done
      echo
      echo "       This is your Python build, not the project. $BASE_VERSION from"
      echo "       python.org is the likely culprit. Install a current Python and"
      echo "       re-run:"
      echo "           brew install python@3.12"
      echo "           bash scripts/doctor.sh --fix"
      note_problem
    fi
  fi
  [ -z "$FAILED_MODULES" ] && PROBLEMS=0
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
      "")     warn "$key is empty" ;;
      *...*)  warn "$key still holds the example placeholder" ;;
      *)      ok "$key is set" ;;
    esac
  done
  sa=$(grep "^GOOGLE_SA_JSON=" .env 2>/dev/null | head -1 | cut -d= -f2-)
  case "$sa" in
    "" | *...*) ;;
    "{"*) ok "GOOGLE_SA_JSON holds inline JSON" ;;
    *) if [ -f "$sa" ]; then ok "key file exists at $sa"
       else
         bad "GOOGLE_SA_JSON points at $sa, which does not exist"
         found=$(ls -t "$HOME"/Downloads/*.json 2>/dev/null | head -3)
         if [ -n "$found" ]; then
           echo "       JSON files in ~/Downloads that might be the key:"
           echo "$found" | sed 's/^/         /'
           echo "       Move the right one into place:"
           echo "         mkdir -p .secrets && mv <that file> $sa && chmod 600 $sa"
         else
           echo "       Download it from the Cloud console - README section 2d."
         fi
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
  echo "  The Python environment is healthy. Activate it before using python:"
  echo "      source .venv/bin/activate"
  echo
  echo "  Or run commands without activating, via the venv directly:"
  echo "      .venv/bin/python scripts/setup_sheet.py --check"
  echo
  echo "  Anything under 'Google Sheets access' is about credentials, not the"
  echo "  install - see README section 2."
else
  echo "  $PROBLEMS problem(s) above still need attention."
fi
