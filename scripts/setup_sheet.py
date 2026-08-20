#!/usr/bin/env python3
"""Create or repair the Google Sheet this pipeline runs on.

Idempotent: run it as often as you like. It creates missing tabs, rewrites
headers, freezes and bolds the header row, applies the Status enum as data
validation, turns Selected into a checkbox, colours rows by status, and wraps
the long text columns.

The data validation matters as much as the code: it stops a fat-fingered cell
from injecting a status the state machine has never heard of.

    python scripts/setup_sheet.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp import log
from lnp.config import load_config
from lnp.models import ALL_STATUSES, COLUMNS, COLUMN_INDEX
from lnp.sheets import (
    AMENDMENT_COLUMNS,
    CONFIG_COLUMNS,
    CONFIG_DEFAULTS,
    FEEDBACK_COLUMNS,
    HISTORY_COLUMNS,
    Sheets,
    a1_column,
)

logger = log.get("setup_sheet")

# Colours are muted on purpose: the human reads this Sheet every morning.
STATUS_COLOURS = {
    "NEW": (0.93, 0.93, 0.93),
    "DRAFTED": (0.85, 0.92, 0.98),
    "REVISE": (1.00, 0.92, 0.80),
    "APPROVED": (0.85, 0.95, 0.85),
    "POSTING": (1.00, 0.98, 0.75),
    "POSTED": (0.75, 0.89, 0.78),
    "FAILED": (0.98, 0.82, 0.82),
    "SKIPPED": (0.90, 0.90, 0.90),
    "EXPIRED": (0.86, 0.84, 0.88),
}

WRAP_COLUMNS = [
    "SourceTitle",
    "WhyItMatters",
    "Angle",
    "DraftText",
    "FinalText",
    "RevisionNote",
    "Error",
]

COLUMN_WIDTHS = {
    "ID": 130,
    "SourceURL": 220,
    "SourceTitle": 260,
    "WhyItMatters": 300,
    "Angle": 260,
    "DraftText": 380,
    "FinalText": 380,
    "RevisionNote": 260,
    "Error": 260,
}

TAB_LAYOUT = [
    ("pipeline_tab", COLUMNS),
    ("history_tab", HISTORY_COLUMNS),
    ("feedback_tab", FEEDBACK_COLUMNS),
    ("amendments_tab", AMENDMENT_COLUMNS),
    ("config_tab", CONFIG_COLUMNS),
]


def ensure_tab(ss, title: str, header: list[str]):
    """Create the tab if missing; always rewrite the header in place."""
    try:
        ws = ss.worksheet(title)
        created = False
    except Exception:
        ws = ss.add_worksheet(title=title, rows=1000, cols=max(len(header), 26))
        created = True
    if ws.col_count < len(header):
        ws.add_cols(len(header) - ws.col_count)
    last = a1_column(len(header) - 1)
    ws.update(values=[header], range_name=f"A1:{last}1", value_input_option="RAW")
    logger.info("tab ready", extra={"tab": title, "created": created})
    return ws


def header_requests(sheet_id: int, width: int) -> list[dict]:
    return [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": width,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.16, "green": 0.20, "blue": 0.24},
                        "horizontalAlignment": "LEFT",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": width,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        }
                    }
                },
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }
        },
    ]


def pipeline_requests(sheet_id: int, existing_rules: int) -> list[dict]:
    """Validation, formatting and colour rules for the Pipeline tab."""
    status_col = COLUMN_INDEX["Status"]
    selected_col = COLUMN_INDEX["Selected"]
    requests: list[dict] = []

    # Status: only the nine states the machine knows about.
    requests.append(
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": status_col,
                    "endColumnIndex": status_col + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": s} for s in ALL_STATUSES],
                    },
                    "showCustomUi": True,
                    "strict": True,
                },
            }
        }
    )
    # Selected: a checkbox, because the human ticks it on a phone.
    requests.append(
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": selected_col,
                    "endColumnIndex": selected_col + 1,
                },
                "rule": {
                    "condition": {"type": "BOOLEAN"},
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }
    )

    # Rebuild conditional formatting from scratch so re-running does not stack
    # duplicate rules on top of each other.
    for index in range(existing_rules - 1, -1, -1):
        requests.append(
            {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
        )

    status_letter = a1_column(status_col)
    for i, (status, (r, g, b)) in enumerate(STATUS_COLOURS.items()):
        requests.append(
            {
                "addConditionalFormatRule": {
                    "index": i,
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(COLUMNS),
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [
                                    {
                                        "userEnteredValue": f'=$%s2="%s"'
                                        % (status_letter, status)
                                    }
                                ],
                            },
                            "format": {
                                "backgroundColor": {"red": r, "green": g, "blue": b}
                            },
                        },
                    },
                }
            }
        )

    # Wrap the long columns; a 1300-character draft in a one-line cell is
    # unreadable, and reading it is the human's whole job.
    for name in WRAP_COLUMNS:
        col = COLUMN_INDEX[name]
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "wrapStrategy": "WRAP",
                            "verticalAlignment": "TOP",
                        }
                    },
                    "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
                }
            }
        )

    for name, width in COLUMN_WIDTHS.items():
        col = COLUMN_INDEX[name]
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col,
                        "endIndex": col + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    return requests


def seed_config(ws) -> None:
    """Write the default Config keys, leaving any existing values alone."""
    existing = {
        str(row[0]).strip().upper(): i
        for i, row in enumerate(ws.get_all_values()[1:], start=2)
        if row and str(row[0]).strip()
    }
    missing = [row for row in CONFIG_DEFAULTS if row[0] not in existing]
    if missing:
        ws.append_rows(missing, value_input_option="RAW")
        logger.info("seeded config keys", extra={"keys": [r[0] for r in missing]})


def check() -> int:
    """Verify access and explain what is wrong, without changing anything.

    Almost every first-run problem is one of three things: the API is not
    enabled, the sheet was never shared with the service account, or the sheet
    id is wrong. Each returns a different error, so name them separately rather
    than making somebody read a stack trace.
    """
    from lnp.config import google_credentials_info, require_env

    try:
        info = google_credentials_info()
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic path
        print(f"GOOGLE_SA_JSON is not usable: {exc}")
        print("It must be either a path to the downloaded key file, or the JSON itself.")
        return 1

    email = info.get("client_email", "?")
    print(f"service account: {email}")
    print(f"project:         {info.get('project_id', '?')}")

    try:
        sheet_id = require_env("SHEET_ID")
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic path
        print(exc)
        return 1
    print(f"sheet id:        {sheet_id}")

    config = load_config()
    try:
        sheets = Sheets.open(config)
        titles = [ws.title for ws in sheets.ss.worksheets()]
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic path
        text = str(exc)
        print(f"\nCannot open the sheet.\n{text[:500]}\n")
        if "has not been used in project" in text or "SERVICE_DISABLED" in text:
            print(
                "The Google Sheets API is not enabled on this project.\n"
                "  Cloud console -> APIs & Services -> Library -> Google Sheets API -> Enable"
            )
        elif "PERMISSION_DENIED" in text or "403" in text:
            print(
                "The service account cannot see this sheet. Open the sheet, click\n"
                f"Share, and add this address as an Editor:\n\n    {email}\n\n"
                "Untick 'Notify people' - it is not a real mailbox."
            )
        elif "404" in text or "not found" in text.lower():
            print(
                "No sheet with that id. SHEET_ID is the part of the URL between\n"
                "  /spreadsheets/d/   and   /edit"
            )
        return 1

    print(f"tabs:            {', '.join(titles) if titles else '(none yet)'}")
    print("\nAccess is working. Run without --check to create or repair the tabs.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="verify access and exit without changing the sheet",
    )
    args = parser.parse_args()
    if args.check:
        return check()

    config = load_config()
    sheets = Sheets.open(config)
    ss = sheets.ss

    tabs = {}
    for key, header in TAB_LAYOUT:
        title = config.get(f"sheet.{key}")
        tabs[key] = ensure_tab(ss, title, header)

    metadata = ss.fetch_sheet_metadata()
    rule_counts = {
        s["properties"]["title"]: len(s.get("conditionalFormats") or [])
        for s in metadata.get("sheets", [])
    }

    requests: list[dict] = []
    for key, header in TAB_LAYOUT:
        ws = tabs[key]
        requests += header_requests(ws.id, len(header))
    pipeline_ws = tabs["pipeline_tab"]
    requests += pipeline_requests(
        pipeline_ws.id, rule_counts.get(pipeline_ws.title, 0)
    )

    ss.batch_update({"requests": requests})
    seed_config(tabs["config_tab"])

    print(f"Sheet ready: {ss.url}")
    print("Tabs:", ", ".join(ws.title for ws in ss.worksheets()))
    print(
        "\nKill switch: Config tab -> PAUSED. Set it to TRUE and the publish job "
        "exits without posting."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
