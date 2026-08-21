#!/usr/bin/env bash
#
#   ./setup.sh
#
# Installs everything and walks you through configuration. Safe to re-run.
#
# It does not use the Python on your machine. It installs uv, which fetches a
# known-good CPython of its own. Every setup failure this project has actually
# seen came from the system Python - a broken pip, an interpreter whose regex
# engine could not run modern packaging, missing CA certificates, C extensions
# built from source against the wrong toolchain. None of those can happen to an
# interpreter we bring ourselves.

set -u
cd "$(dirname "$0")" || exit 1
ROOT=$(pwd)

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
bad()  { printf "  ${RED}✗${RESET} %s\n" "$1"; }
note() { printf "    ${DIM}%s${RESET}\n" "$1"; }
step() { printf "\n${BOLD}%s${RESET}\n" "$1"; }

PYTHON_VERSION=3.12

step "Toolchain"

UV=""
for candidate in "$ROOT/.uv/uv" "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] && { UV="$candidate"; break; }
done

if [ -z "$UV" ]; then
  note "installing uv (a package manager that brings its own Python)"
  if command -v brew >/dev/null 2>&1 && brew install uv >/tmp/lnp_uv.log 2>&1; then
    UV=$(command -v uv)
  else
    # Download to a file and run it, rather than piping the network into a
    # shell: you can read what you are about to execute.
    mkdir -p "$ROOT/.uv"
    if curl -fsSL --max-time 120 https://astral.sh/uv/install.sh -o "$ROOT/.uv/install.sh" 2>/dev/null; then
      note "downloaded uv installer to .uv/install.sh"
      UV_INSTALL_DIR="$ROOT/.uv" UV_UNMANAGED_INSTALL="$ROOT/.uv" \
        sh "$ROOT/.uv/install.sh" >/tmp/lnp_uv.log 2>&1
      [ -x "$ROOT/.uv/uv" ] && UV="$ROOT/.uv/uv"
    fi
    # Last resort: any Python at all is enough to fetch uv, which then takes over.
    if [ -z "$UV" ]; then
      for py in python3 python3.13 python3.12 python3.11; do
        command -v "$py" >/dev/null 2>&1 || continue
        "$py" -m pip install --quiet --upgrade --target "$ROOT/.uv/pkg" uv >/tmp/lnp_uv.log 2>&1 && {
          [ -x "$ROOT/.uv/pkg/bin/uv" ] && UV="$ROOT/.uv/pkg/bin/uv"; }
        [ -n "$UV" ] && break
      done
    fi
  fi
fi

if [ -z "$UV" ]; then
  bad "could not install uv"
  note "last output:"; tail -5 /tmp/lnp_uv.log 2>/dev/null | sed 's/^/      /'
  note "install it by hand from https://docs.astral.sh/uv/ then re-run ./setup.sh"
  exit 1
fi
ok "uv $("$UV" --version 2>/dev/null | awk '{print $2}')"

step "Python and dependencies"
if ! "$UV" venv --python "$PYTHON_VERSION" "$ROOT/.venv" >/tmp/lnp_venv.log 2>&1; then
  bad "could not create the environment"
  tail -10 /tmp/lnp_venv.log | sed 's/^/      /'
  exit 1
fi
ok "python $("$ROOT/.venv/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
note "supplied by uv, independent of any Python on this machine"

if ! "$UV" pip install --python "$ROOT/.venv/bin/python" -q -r requirements.txt >/tmp/lnp_deps.log 2>&1; then
  bad "could not install dependencies"
  tail -15 /tmp/lnp_deps.log | sed 's/^/      /'
  exit 1
fi
ok "dependencies installed"

MISSING=""
for module in yaml gspread anthropic feedparser rapidfuzz bs4 ulid dateutil requests; do
  "$ROOT/.venv/bin/python" -c "import $module" >/dev/null 2>&1 || MISSING="$MISSING $module"
done
if [ -n "$MISSING" ]; then
  bad "installed but not importable:$MISSING"
  note "re-run ./setup.sh; if it persists this is a bug, please report it"
  exit 1
fi
ok "all packages import"

if ! "$ROOT/.venv/bin/python" -m pytest >/tmp/lnp_test.log 2>&1; then
  bad "the test suite did not pass - the install is not trustworthy"
  tail -15 /tmp/lnp_test.log | sed 's/^/      /'
  exit 1
fi
ok "$(grep -E 'passed|failed' /tmp/lnp_test.log | tail -1)"

[ -f .env ] || { cp .env.example .env 2>/dev/null || touch .env; chmod 600 .env; }

step "Configuration"
exec "$ROOT/.venv/bin/python" scripts/wizard.py
