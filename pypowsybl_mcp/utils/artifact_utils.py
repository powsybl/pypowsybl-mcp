#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Hand a large result over as a file instead of inlining it in the answer.

A read tool that returns thousands of rows has two bad options: send them all,
which floods the caller's context, or paginate, which means dozens of round
trips to get the whole table. `return_as="artifact"` is the third one: the rows
are written to a temporary file, and the answer carries the summary, the column
list and a link. Whoever needs the data fetches it directly - another MCP
server, a notebook, the user's browser - and the numbers never pass through a
language model on the way.
"""

import csv
import io
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from loguru import logger

from pypowsybl_mcp.utils import DOWNLOAD_BASE_URL, DOWNLOAD_LINK_EXPIRY_SECONDS
from pypowsybl_mcp.utils.download_utils import generate_download_link

# What `return_as` accepts.
RETURN_MODES = ("inline", "artifact")

# What an artifact can be written as. JSON keeps the types, CSV opens in a
# spreadsheet; both are read back by any HTTP client.
ARTIFACT_FORMATS = ("json", "csv")

# How many rows the answer still shows when the table went to a file: enough to
# see the shape of a row and check the columns are the expected ones, few enough
# to stay cheap.
PREVIEW_ROWS = 5


def normalize_return_as(return_as: str | None) -> str:
    """Read the `return_as` argument, or say what it should have been."""
    mode = (return_as or "inline").strip().lower()
    if mode not in RETURN_MODES:
        raise ValueError(
            f"Invalid return_as '{return_as}': expected one of {', '.join(RETURN_MODES)}"
        )
    return mode


def normalize_artifact_format(artifact_format: str | None) -> str:
    """Read the `artifact_format` argument, or say what it should have been."""
    fmt = (artifact_format or "json").strip().lower().lstrip(".")
    if fmt not in ARTIFACT_FORMATS:
        raise ValueError(
            f"Invalid artifact_format '{artifact_format}': "
            f"expected one of {', '.join(ARTIFACT_FORMATS)}"
        )
    return fmt


def columns_of(rows: list[dict[str, Any]]) -> list[str]:
    """The union of the row keys, in the order they first appear."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def dataframe_to_rows(frame: pd.DataFrame, id_column: str = "id") -> list[dict]:
    """Turn a DataFrame into JSON-safe rows, keeping its index as a column.

    The index holds the element id in every pypowsybl table, so dropping it
    would make the rows unusable. NaN becomes null on the way, and the columns
    of a comparison frame - which come as tuples - are flattened to
    `<column>_<variant>` so the result is a flat table like any other.
    """
    flat = frame.copy()
    if isinstance(flat.columns, pd.MultiIndex):
        flat.columns = [
            "_".join(str(part) for part in column if part != "")
            for column in flat.columns
        ]
    flat = flat.reset_index()
    first = str(flat.columns[0])
    if first in ("index", "level_0"):
        flat = flat.rename(columns={flat.columns[0]: id_column})
    # `to_json` is what turns NaN and numpy scalars into valid JSON; going
    # through `to_dict` would leave both in place.
    return json.loads(flat.to_json(orient="records"))


def _json_bytes(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    """Serialize rows as a table document.

    One list field, named `rows`, so a consumer that looks for "the list inside
    the payload" finds it without being told where it is.
    """
    payload = {
        "columns": columns,
        "row_count": len(rows),
        "generated_at": datetime.now(UTC).isoformat(),
        "rows": rows,
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def _csv_bytes(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    """Serialize rows as CSV, with a UTF-8 BOM so Excel reads it correctly."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=columns,
        restval="",
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row.get(column)) for column in columns})
    return buffer.getvalue().encode("utf-8-sig")


def _csv_value(value: Any) -> Any:
    """What a CSV cell can hold: a scalar, or the JSON of anything else."""
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return ""
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def artifact_filename(stem: str, artifact_format: str) -> str:
    """A safe, timestamped file name built from what the artifact holds."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "result"
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{cleaned}_{stamp}.{artifact_format}"


def build_artifact(
    rows: list[dict[str, Any]],
    filename_stem: str,
    artifact_format: str = "json",
) -> dict[str, Any]:
    """Write `rows` to a temporary file and describe how to fetch it."""
    artifact_format = normalize_artifact_format(artifact_format)
    columns = columns_of(rows)
    data = (
        _csv_bytes(rows, columns)
        if artifact_format == "csv"
        else _json_bytes(rows, columns)
    )
    link = generate_download_link(
        filename=artifact_filename(filename_stem, artifact_format),
        file_data=data,
        download_base_url=DOWNLOAD_BASE_URL,
        expiry_seconds=DOWNLOAD_LINK_EXPIRY_SECONDS,
    )
    logger.info(
        f"Stored {len(rows)} row(s) as a {artifact_format} artifact: {link['download_url']}"
    )
    return {
        "url": link["download_url"],
        "format": artifact_format,
        "row_count": len(rows),
        "columns": columns,
        "size_bytes": len(data),
        "expires_at": link["expires_at"],
        # Said in the answer rather than left to the caller to know: the whole
        # point of an artifact is that it is fetched, not read out loud.
        "hint": (
            "The full table is in this file. Fetch the URL to use it "
            "(it is a plain HTTP GET); do not transcribe it row by row."
        ),
    }


def artifact_response(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    filename_stem: str,
    artifact_format: str = "json",
    preview_rows: int = PREVIEW_ROWS,
) -> dict[str, Any]:
    """Assemble the answer of a tool that put its rows in a file.

    The summary keeps whatever the tool counted - it is small and it is what a
    reader acts on - and the rows themselves are replaced by the artifact plus
    a short preview.
    """
    result = dict(summary)
    result["return_as"] = "artifact"
    result["artifact"] = build_artifact(rows, filename_stem, artifact_format)
    if preview_rows > 0:
        result["preview"] = rows[:preview_rows]
    return result
