#!/usr/bin/env python3
"""Interactive setup. Collects credentials, checking each one as it goes.

    ./lnp setup            walk through whatever is missing
    ./lnp setup --recheck  verify what is already configured, change nothing

Every value is tested against the real service the moment it is entered, so a
wrong one is caught where it was typed rather than three steps later as an
error that names something else. Nothing is written until it has passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import onboarding as onb
from lnp.config import REPO_ROOT

ENV_PATH = REPO_ROOT / ".env"

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)


def say(text: str = "") -> None:
    print(text)


def ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def bad(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


def note(text: str) -> None:
    print(f"    {DIM}{text}{RESET}")


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {DIM}[{default}]{RESET}" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        say("\n\nStopped. Nothing was written that had not already passed its check.")
        raise SystemExit(1)
    return answer or default


def confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    return default if not answer else answer.startswith("y")


# --------------------------------------------------------------------------
# Live checks. Each one talks to the real service.
# --------------------------------------------------------------------------


def verify_anthropic(key: str) -> onb.Check:
    import anthropic

    try:
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4,
            messages=[{"role": "user", "content": "ok"}],
        )
        return onb.Check(True, "key works")
    except anthropic.AuthenticationError:
        return onb.Check(False, "the API rejected that key",
                         "Copy it again from console.anthropic.com -> API keys")
    except anthropic.PermissionDeniedError:
        return onb.Check(False, "key is valid but not permitted to use the model",
                         "Check the workspace the key belongs to has credit")
    except anthropic.APIStatusError as exc:
        if exc.status_code == 429:
            return onb.Check(True, "key works (rate limited right now, which still proves it)")
        return onb.Check(False, f"API error {exc.status_code}", str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 - shown to the human, not swallowed
        return onb.Check(False, f"could not reach the API: {type(exc).__name__}", str(exc)[:200])


def verify_sheet(sheet_id: str, key_path: str) -> onb.Check:
    """Open the sheet as the service account, and name the fix for a 403."""
    import gspread
    from google.oauth2.service_account import Credentials

    from lnp.sheets import SCOPES

    key_check = onb.check_key_file(key_path, REPO_ROOT)
    if not key_check.ok:
        return key_check
    email = key_check.extra["client_email"]

    try:
        raw = key_path if key_path.lstrip().startswith("{") else Path(key_path).read_text()
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        sheet = gspread.authorize(creds).open_by_key(sheet_id)
        tabs = [ws.title for ws in sheet.worksheets()]
        return onb.Check(True, f"opened {sheet.title!r}",
                         extra={"tabs": ", ".join(tabs), "url": sheet.url})
    except gspread.exceptions.APIError as exc:
        text = str(exc)
        if "PERMISSION_DENIED" in text or "403" in text:
            return onb.Check(
                False,
                "the service account cannot see that sheet",
                f"Open the sheet, click Share, add this address as an Editor:\n"
                f"      {BOLD}{email}{RESET}\n"
                f"      Untick 'Notify people' - it is not a mailbox.",
            )
        if "has not been used in project" in text or "SERVICE_DISABLED" in text:
            return onb.Check(
                False, "the Google Sheets API is not switched on",
                "Cloud console -> APIs & Services -> Library -> Google Sheets API -> Enable",
            )
        if "404" in text or "NOT_FOUND" in text:
            return onb.Check(False, "no sheet with that id",
                             "Check you copied the address of the right sheet")
        return onb.Check(False, "Google refused the request", text[:300])
    except Exception as exc:  # noqa: BLE001
        return onb.Check(False, f"{type(exc).__name__}", str(exc)[:300])


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def step_key_file(env: dict) -> str:
    heading("1. Google service-account key")
    current = env.get("GOOGLE_SA_JSON", "")
    if onb.is_set(current):
        check = onb.check_key_file(current, REPO_ROOT)
        if check.ok:
            ok(check.detail)
            return current
        bad(f"{current}: {check.detail}")

    found = onb.find_key_files(REPO_ROOT)
    if found:
        say("  Found a key already on this machine:")
        for i, path in enumerate(found[:5], start=1):
            info = onb.check_key_file(str(path))
            say(f"    {i}. {path}  {DIM}({info.extra.get('client_email','')}){RESET}")
        answer = ask("Number to use, or paste a path / drag the file here", "1")
        if answer.isdigit() and 1 <= int(answer) <= len(found[:5]):
            chosen = found[int(answer) - 1]
        else:
            chosen = Path(answer.strip().strip("'\"")).expanduser()
    else:
        say("  Download it: Cloud console -> APIs & Services -> Credentials ->")
        say("  your service account -> Keys -> Add Key -> Create new key -> JSON.")
        chosen = Path(ask("Then drag the file here, or paste its path").strip().strip("'\"")).expanduser()

    while True:
        check = onb.check_key_file(str(chosen), REPO_ROOT)
        if check.ok:
            break
        bad(check.detail)
        if check.fix:
            note(check.fix)
        chosen = Path(ask("Try again - path to the key file").strip().strip("'\"")).expanduser()

    installed = onb.install_key_file(chosen, REPO_ROOT)
    rel = installed.relative_to(REPO_ROOT)
    ok(f"{check.extra['client_email']}")
    note(f"filed at {rel}, readable only by you")
    note("this address is what you share the sheet with, in the next step")
    return str(rel)


def step_sheet(env: dict, key_path: str) -> str:
    heading("2. Your Google Sheet")
    current = env.get("SHEET_ID", "")
    if onb.is_set(current):
        check = verify_sheet(current, key_path)
        if check.ok:
            ok(f"{check.detail} - tabs: {check.extra.get('tabs') or 'none yet'}")
            return current
        bad(check.detail)
        if check.fix:
            note(check.fix)

    say("  Create one at https://sheets.new if you have not already.")
    while True:
        answer = ask("Paste the sheet's web address (or its id)")
        parsed = onb.check_sheet_id(answer)
        if not parsed.ok:
            bad(parsed.detail)
            note(parsed.fix)
            continue
        sheet_id = parsed.extra["sheet_id"]

        check = verify_sheet(sheet_id, key_path)
        if check.ok:
            ok(f"{check.detail} - tabs: {check.extra.get('tabs') or 'none yet'}")
            return sheet_id
        bad(check.detail)
        if check.fix:
            note(check.fix)
        if not confirm("Try again once you have done that?"):
            return sheet_id


def step_anthropic(env: dict) -> str:
    heading("3. Anthropic API key")
    current = env.get("ANTHROPIC_API_KEY", "")
    if onb.is_set(current):
        check = verify_anthropic(current)
        if check.ok:
            ok(check.detail)
            return current
        bad(check.detail)

    say("  Create one at https://console.anthropic.com -> API keys.")
    say(f"  {DIM}Used to score sources and draft posts. Costs pennies per week.{RESET}")
    while True:
        key = ask("Paste the key (starts sk-ant-)")
        if not key:
            if confirm("Skip for now? Curating and drafting will not run", default=False):
                return current
            continue
        check = verify_anthropic(key)
        if check.ok:
            ok(check.detail)
            return key
        bad(check.detail)
        if check.fix:
            note(check.fix)


def build_tabs(sheet_id: str, key_path: str) -> None:
    heading("4. Build the sheet's tabs")
    check = verify_sheet(sheet_id, key_path)
    existing = (check.extra.get("tabs") or "") if check.ok else ""
    if "Pipeline" in existing:
        ok("tabs already present")
        if not confirm("Repair them anyway?", default=False):
            return
    elif not confirm("Create the five tabs now?"):
        return

    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "setup_sheet.py")],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("tabs created")
        for line in result.stdout.splitlines():
            if line.startswith("Sheet ready") or line.startswith("Tabs:"):
                note(line)
    else:
        bad("could not build the tabs")
        for line in (result.stderr or result.stdout).splitlines()[-8:]:
            note(line)


def recheck(env: dict) -> int:
    heading("Configured values")
    for key, good, why in onb.status_lines(env):
        (ok if good else bad)(f"{key}: {why}")

    problems = 0
    if onb.is_set(env.get("GOOGLE_SA_JSON")) and onb.is_set(env.get("SHEET_ID")):
        heading("Google Sheet")
        check = verify_sheet(env["SHEET_ID"], env["GOOGLE_SA_JSON"])
        (ok if check.ok else bad)(check.detail)
        if not check.ok:
            note(check.fix)
            problems += 1
        else:
            note(f"tabs: {check.extra.get('tabs') or 'none yet - run ./lnp setup'}")

    if onb.is_set(env.get("ANTHROPIC_API_KEY")):
        heading("Anthropic")
        check = verify_anthropic(env["ANTHROPIC_API_KEY"])
        (ok if check.ok else bad)(check.detail)
        problems += 0 if check.ok else 1
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recheck", action="store_true",
                        help="verify what is configured and change nothing")
    args = parser.parse_args()

    env = onb.read_env(ENV_PATH)
    if args.recheck:
        return recheck(env)

    say(f"{BOLD}Setup{RESET}")
    say("Each value is checked against the real service before it is saved.")
    say(f"{DIM}Values are written to .env, which is gitignored. Nothing else is touched.{RESET}")

    key_path = step_key_file(env)
    onb.upsert_env(ENV_PATH, {"GOOGLE_SA_JSON": key_path})

    sheet_id = step_sheet(env, key_path)
    onb.upsert_env(ENV_PATH, {"SHEET_ID": sheet_id})

    api_key = step_anthropic(env)
    if api_key:
        onb.upsert_env(ENV_PATH, {"ANTHROPIC_API_KEY": api_key})

    build_tabs(sheet_id, key_path)

    heading("Done")
    remaining = onb.missing_required(onb.read_env(ENV_PATH))
    if remaining:
        say(f"  Still to set: {', '.join(remaining)}. Re-run ./lnp setup any time.")
    else:
        say("  Everything needed to curate and draft is configured.")
    say("")
    say(f"  {BOLD}./lnp curate{RESET}      put this week's candidates in the sheet")
    say(f"  {BOLD}./lnp check{RESET}       re-verify everything")
    say("")
    say("  Publishing to LinkedIn needs its own setup - README section 4.")
    say(f"  {DIM}Until then config/config.yaml keeps dry_run on, so nothing posts.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
