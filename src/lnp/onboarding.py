"""Setup logic: reading and writing .env, and validating what goes in it.

Deliberately free of prompts and printing, so every decision the wizard makes is
testable without a terminal. `scripts/wizard.py` is the interactive shell around
this; everything that can be got wrong lives here.

The design rule throughout: accept what the human has in front of them. They
have a browser tab open at their sheet, so take the whole URL. They have a key
file called whatever Google named it, so take the file. Anything this module can
work out for itself is a step the human cannot get wrong.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The values setup collects, in the order it makes sense to ask for them: the
# sheet before its credentials, and anything not needed to reach a working
# sheet left until last.
REQUIRED_KEYS = ["GOOGLE_SA_JSON", "SHEET_ID", "ANTHROPIC_API_KEY"]
OPTIONAL_KEYS = ["SLACK_WEBHOOK_URL", "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"]

# A Google file id: long, and letters/digits/dash/underscore only.
_SHEET_ID = re.compile(r"^[A-Za-z0-9_-]{30,}$")
_SHEET_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{30,})")

# A value that is still the shipped example rather than something real.
_PLACEHOLDER = re.compile(r"\.\.\.|^<.*>$|^your[-_ ]")


@dataclass
class Check:
    """The outcome of validating one value."""

    ok: bool
    detail: str = ""
    fix: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


def is_placeholder(value: str) -> bool:
    """True for a template value like `sk-ant-...` that reads as filled in."""
    value = (value or "").strip()
    return bool(value) and bool(_PLACEHOLDER.search(value))


def is_set(value: Optional[str]) -> bool:
    return bool((value or "").strip()) and not is_placeholder(value)


# --------------------------------------------------------------------------
# The sheet
# --------------------------------------------------------------------------


def parse_sheet_id(text: str) -> Optional[str]:
    """Accept a full sheet URL or a bare id, return the id.

    People have the URL in front of them, not the id. Asking them to extract a
    substring by eye is a step that can be got wrong, so don't ask.
    """
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return None
    match = _SHEET_URL.search(text)
    if match:
        return match.group(1)
    if _SHEET_ID.match(text):
        return text
    return None


def check_sheet_id(text: str) -> Check:
    sheet_id = parse_sheet_id(text)
    if sheet_id:
        return Check(True, f"sheet id {sheet_id[:8]}...{sheet_id[-4:]}", extra={"sheet_id": sheet_id})
    if "docs.google.com" in (text or ""):
        return Check(
            False,
            "that is a Google URL but not a spreadsheet one",
            "Open the sheet itself; the address contains /spreadsheets/d/",
        )
    return Check(
        False,
        "not a sheet id or sheet URL",
        "Paste the whole address bar from your open sheet, or just the id "
        "between /d/ and /edit",
    )


# --------------------------------------------------------------------------
# The service-account key
# --------------------------------------------------------------------------


def check_key_file(value: str, root: Optional[Path] = None) -> Check:
    """Validate a service-account key given as a path or as raw JSON."""
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        return Check(False, "no value given")

    raw = value
    if not value.lstrip().startswith("{"):
        path = Path(value).expanduser()
        if not path.is_absolute() and root:
            path = root / path
        if not path.is_file():
            return Check(
                False,
                f"no file at {path}",
                "Drag the downloaded .json into this window, or give its full path",
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return Check(False, f"cannot read {path}: {exc}")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return Check(
            False,
            "that file is not JSON",
            "Google's key file is .json - check you picked JSON rather than P12",
        )
    if not isinstance(info, dict):
        return Check(False, "JSON is not an object")

    missing = [k for k in ("client_email", "private_key", "project_id") if k not in info]
    if missing:
        return Check(
            False,
            f"JSON is missing {', '.join(missing)}",
            "That is not a service-account key. In the Cloud console it comes "
            "from the service account's Keys tab, not from OAuth credentials.",
        )
    return Check(
        True,
        f"key for {info['client_email']}",
        extra={"client_email": info["client_email"], "project_id": info["project_id"]},
    )


def find_key_files(root: Path, home: Optional[Path] = None) -> List[Path]:
    """Candidate service-account keys, best guess first.

    Looks where the file actually ends up: already filed under .secrets/, or
    still sitting in Downloads under whatever name Google gave it.
    """
    seen: List[Path] = []
    for candidate in sorted((root / ".secrets").glob("*.json")):
        seen.append(candidate)
    downloads = (home or Path.home()) / "Downloads"
    if downloads.is_dir():
        by_recency = sorted(
            downloads.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        seen.extend(by_recency[:10])
    return [p for p in seen if check_key_file(str(p)).ok]


def install_key_file(source: Path, root: Path) -> Path:
    """Copy a key into .secrets/ under its own name, chmod 600.

    Keeps Google's filename rather than renaming to a fixed one: a rename means
    the name in .env and the name on disk are two facts that can disagree, and
    they did.
    """
    secrets = root / ".secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    target = secrets / source.name
    if source.resolve() != target.resolve():
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    return target


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------


def read_env(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def upsert_env(path: Path, updates: Dict[str, str]) -> None:
    """Set keys in .env, preserving every other line including comments.

    Rewrites in place rather than regenerating, so a human's own additions and
    notes survive being edited by the wizard.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows and some mounts do not support it; the value is still written.


def missing_required(env: Dict[str, str]) -> List[str]:
    return [k for k in REQUIRED_KEYS if not is_set(env.get(k))]


def status_lines(env: Dict[str, str]) -> List[Tuple[str, bool, str]]:
    """(key, ok, note) for every value setup cares about, for display."""
    out: List[Tuple[str, bool, str]] = []
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        value = env.get(key, "")
        if is_set(value):
            out.append((key, True, "set"))
        elif is_placeholder(value):
            out.append((key, False, "still the example placeholder"))
        else:
            out.append((key, False, "not set"))
    return out
