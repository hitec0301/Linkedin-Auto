#!/usr/bin/env python3
"""Print the two secrets a hosted deployment needs.

Run once, put the values in the host's variables, and keep them. Rotating
LNP_ENCRYPTION_KEY makes every stored LinkedIn credential unreadable and every
customer has to reconnect, so it is a decision, not a maintenance task.

    python scripts/gen_keys.py
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp.db.crypto import generate_key


def main() -> int:
    print("# Put these in your host's variables. They are shown once.")
    print(f"LNP_SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"LNP_ENCRYPTION_KEY={generate_key()}")
    print()
    print("# LNP_SECRET_KEY signs session cookies. Changing it signs everyone out.")
    print("# LNP_ENCRYPTION_KEY encrypts stored LinkedIn credentials. Changing it")
    print("# makes them unreadable and every account has to reconnect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
