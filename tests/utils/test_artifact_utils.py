#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Tests for the helper that hands a result over as a file."""

import json

import pandas as pd
import pytest

from pypowsybl_mcp.utils.artifact_utils import (
    artifact_filename,
    artifact_response,
    build_artifact,
    columns_of,
    dataframe_to_rows,
    normalize_artifact_format,
    normalize_return_as,
)
from pypowsybl_mcp.utils.download_utils import download_links


def artifact_content(artifact: dict) -> bytes:
    """Read back the file an artifact points at."""
    token = artifact["url"].rsplit("/", 2)[-2]
    with open(download_links[token]["temp_path"], "rb") as handle:
        return handle.read()


ROWS = [
    {"bus_id": "B1", "v_kv": 412.0},
    {"bus_id": "B2", "v_kv": 238.9, "note": "extra"},
]


# --- argument reading -------------------------------------------------------


def test_return_as_defaults_to_inline():
    assert normalize_return_as(None) == "inline"
    assert normalize_return_as("") == "inline"
    assert normalize_return_as(" Artifact ") == "artifact"


def test_an_unknown_return_as_says_what_is_accepted():
    with pytest.raises(ValueError, match="inline, artifact"):
        normalize_return_as("file")


def test_artifact_format_defaults_to_json():
    assert normalize_artifact_format(None) == "json"
    assert normalize_artifact_format(".CSV") == "csv"


def test_an_unknown_artifact_format_says_what_is_accepted():
    with pytest.raises(ValueError, match="json, csv"):
        normalize_artifact_format("xlsx")


# --- rows -------------------------------------------------------------------


def test_columns_are_the_union_in_order_of_appearance():
    assert columns_of(ROWS) == ["bus_id", "v_kv", "note"]
    assert columns_of([]) == []


def test_a_dataframe_keeps_its_index_as_a_column():
    frame = pd.DataFrame({"v_mag": [400.0, 225.0]}, index=["B1", "B2"])
    frame.index.name = "id"

    assert dataframe_to_rows(frame) == [
        {"id": "B1", "v_mag": 400.0},
        {"id": "B2", "v_mag": 225.0},
    ]


def test_an_unnamed_index_still_becomes_the_id_column():
    frame = pd.DataFrame({"v_mag": [400.0]}, index=["B1"])

    assert dataframe_to_rows(frame) == [{"id": "B1", "v_mag": 400.0}]


def test_missing_values_become_null_rather_than_nan():
    frame = pd.DataFrame({"v_mag": [400.0, float("nan")]}, index=["B1", "B2"])

    rows = dataframe_to_rows(frame)

    assert rows[1]["v_mag"] is None
    # NaN would not survive a JSON round trip; null does.
    assert json.loads(json.dumps(rows)) == rows


def test_a_comparison_frame_is_flattened_into_a_plain_table():
    frame = pd.DataFrame(
        {("v_mag", "base"): [400.0], ("v_mag", "variant"): [412.0]},
        index=["B1"],
    )

    rows = dataframe_to_rows(frame)

    assert rows == [{"id": "B1", "v_mag_base": 400.0, "v_mag_variant": 412.0}]


# --- the artifact itself ----------------------------------------------------


def test_an_artifact_describes_what_it_holds():
    artifact = build_artifact(ROWS, "net1_voltage_violations")

    assert artifact["row_count"] == 2
    assert artifact["columns"] == ["bus_id", "v_kv", "note"]
    assert artifact["format"] == "json"
    assert artifact["size_bytes"] > 0
    assert artifact["url"].endswith(".json")


def test_the_json_artifact_holds_every_row_under_a_field_named_rows():
    artifact = build_artifact(ROWS, "net1")
    payload = json.loads(artifact_content(artifact))

    assert payload["row_count"] == 2
    assert payload["columns"] == ["bus_id", "v_kv", "note"]
    # "rows" is the name a consumer looks the table up by, and it matters that
    # it stays put: the column list sits next to it and is a list too, so
    # "the list inside the payload" would be ambiguous.
    assert payload["rows"] == ROWS


def test_the_csv_artifact_opens_in_a_spreadsheet():
    artifact = build_artifact(ROWS, "net1", artifact_format="csv")
    content = artifact_content(artifact)

    assert content.startswith(b"\xef\xbb\xbf")  # Excel needs the BOM
    lines = content.decode("utf-8-sig").splitlines()
    assert lines[0] == "bus_id,v_kv,note"
    assert lines[1] == "B1,412.0,"


def test_a_csv_cell_that_is_not_a_scalar_is_serialized_not_dropped():
    artifact = build_artifact([{"ids": ["a", "b"]}], "net1", artifact_format="csv")

    assert '"[""a"", ""b""]"' in artifact_content(artifact).decode("utf-8-sig")


def test_an_empty_result_still_produces_a_readable_artifact():
    artifact = build_artifact([], "net1")

    assert artifact["row_count"] == 0
    assert artifact["columns"] == []
    assert json.loads(artifact_content(artifact))["rows"] == []


def test_file_names_are_safe_and_timestamped():
    name = artifact_filename("net 1/violations", "json")

    assert name.startswith("net_1_violations_")
    assert name.endswith(".json")
    assert "/" not in name


# --- the answer around it ---------------------------------------------------


def test_the_summary_is_kept_and_the_rows_are_replaced_by_the_link():
    summary = {"success": True, "network_id": "net1", "violation_count": 2}

    result = artifact_response(summary, ROWS, "net1_violations")

    assert result["success"] is True
    assert result["violation_count"] == 2
    assert result["return_as"] == "artifact"
    assert result["artifact"]["row_count"] == 2
    assert result["preview"] == ROWS
    # The caller's dict is never mutated.
    assert "artifact" not in summary


def test_the_preview_is_capped():
    rows = [{"i": i} for i in range(50)]

    result = artifact_response({}, rows, "net1", preview_rows=3)

    assert result["preview"] == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert result["artifact"]["row_count"] == 50
