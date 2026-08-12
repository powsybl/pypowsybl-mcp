#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import pandas as pd
import pytest

from pypowsybl_mcp.utils.element_data_filter import (
    apply_element_filter,
    attach_current_limits,
    attach_tap_changer_data,
    compute_metric,
)


def _operational_limits():
    # Same shape as Network.get_operational_limits(): a permanent CURRENT limit
    # (acceptable_duration = -1) and two temporary limits (60 s, 600 s) on line_1.
    return pd.DataFrame(
        {
            "element_id": ["line_1", "line_1", "line_1", "line_2"],
            "side": ["ONE", "ONE", "ONE", "ONE"],
            "type": ["CURRENT", "CURRENT", "CURRENT", "CURRENT"],
            "acceptable_duration": [-1, 60, 600, -1],
            "value": [100.0, 110.0, 120.0, 200.0],
        }
    )


def test_filter_on_existing_column():
    df = pd.DataFrame({"p": [1.0, 5.0, 10.0]}, index=["a", "b", "c"])

    out = apply_element_filter(
        df,
        element_type="generators",
        metric="p",
        filter_op=">=",
        filter_value=5,
    )

    # Sorted by descending p, so the biggest one comes first.
    assert list(out.index) == ["c", "b"]


def test_filter_loading_percent_for_generators():
    # max_p is 100 everywhere, so loading_percent equals p here.
    df = pd.DataFrame(
        {"p": [40.0, 88.0, 95.0, 120.0], "max_p": [100.0, 100.0, 100.0, 100.0]},
        index=["g1", "g2", "g3", "g4"],
    )

    out = apply_element_filter(
        df,
        element_type="generators",
        metric="loading_percent",
        filter_op=">",
        filter_value=90,
    )

    assert list(out.index) == ["g4", "g3"]
    assert out.loc["g4", "loading_percent"] == pytest.approx(120.0)


def test_filter_lines_use_worst_side():
    # p1 and p2 are the two ends of a line; we keep the worst (max abs) side.
    df = pd.DataFrame(
        {"p1": [50.0, -120.0], "p2": [-50.0, 80.0]},
        index=["line_1", "line_2"],
    )

    out = apply_element_filter(
        df,
        element_type="lines",
        metric="p_abs",
        filter_op="gt",
        filter_value=100,
    )

    assert list(out.index) == ["line_2"]
    assert out.loc["line_2", "p_abs"] == pytest.approx(120.0)


def test_unknown_operator_is_rejected():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    with pytest.raises(ValueError, match="filter_op"):
        apply_element_filter(
            df,
            element_type="generators",
            metric="p",
            filter_op="??",
            filter_value=1,
        )


def test_non_numeric_threshold_is_rejected():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    with pytest.raises(ValueError, match="filter_value must be a number"):
        apply_element_filter(
            df,
            element_type="generators",
            metric="p",
            filter_op=">",
            filter_value="not-a-number",
        )


def test_unknown_metric_lists_available_columns():
    df = pd.DataFrame({"p": [1.0], "q": [2.0]}, index=["g1"])

    with pytest.raises(ValueError, match="Unknown metric"):
        compute_metric(df, "does_not_exist", "generators")


def test_attach_current_limits_permanent_ignores_temporary():
    df = pd.DataFrame({"i1": [80.0, 150.0]}, index=["line_1", "line_2"])

    out = attach_current_limits(df, _operational_limits())

    # The temporary limits (110/120) are ignored, only the permanent one (100) stays.
    assert out.loc["line_1", "current_limit1"] == pytest.approx(100.0)
    assert out.loc["line_2", "current_limit1"] == pytest.approx(200.0)


def test_attach_current_limits_without_limits_returns_input():
    df = pd.DataFrame({"i1": [80.0]}, index=["line_1"])

    assert attach_current_limits(df, None).equals(df)
    assert attach_current_limits(df, pd.DataFrame()).equals(df)


def test_attach_current_limits_temporary_prefers_longest_duration():
    df = pd.DataFrame({"i1": [80.0, 150.0]}, index=["line_1", "line_2"])

    out = attach_current_limits(df, _operational_limits(), limit_kind="temporary")

    # line_1 keeps the longest-duration temporary limit (600 s, 120 A) over the
    # shorter 60 s one (110 A); line_2 has no temporary limit so it falls back
    # to the permanent 200 A.
    assert out.loc["line_1", "current_limit1"] == pytest.approx(120.0)
    assert out.loc["line_2", "current_limit1"] == pytest.approx(200.0)


def test_loading_percent_uses_current_limit1_from_operational_limits():
    df = attach_current_limits(
        pd.DataFrame(
            {"i1": [100.0, 150.0], "i2": [-100.0, -150.0]}, index=["line_1", "line_2"]
        ),
        _operational_limits(),
    )

    out = apply_element_filter(
        df,
        element_type="lines",
        metric="loading_percent",
        filter_op=">",
        filter_value=90,
    )

    assert list(out.index) == ["line_1"]
    assert out.loc["line_1", "loading_percent"] == pytest.approx(100.0)


def test_max_abs_returns_nan_when_no_matching_columns():
    # Lines with neither p1/p2 nor q1/q2: computing p_abs/q_abs should yield NaN
    # for every row rather than raising.
    df = pd.DataFrame({"other": [1.0, 2.0]}, index=["line_1", "line_2"])

    values = compute_metric(df, "p_abs", "lines")

    assert values.isna().all()


def test_attach_current_limits_noop_when_current_limit1_already_present():
    df = pd.DataFrame({"i1": [80.0], "current_limit1": [999.0]}, index=["line_1"])

    out = attach_current_limits(df, _operational_limits())

    # Existing current_limit1 column short-circuits the merge entirely.
    assert out is df
    assert out.loc["line_1", "current_limit1"] == pytest.approx(999.0)


def test_attach_current_limits_missing_required_columns_returns_input():
    df = pd.DataFrame({"i1": [80.0]}, index=["line_1"])
    # Missing "value" column among the required ones.
    incomplete_limits = pd.DataFrame(
        {
            "element_id": ["line_1"],
            "side": ["ONE"],
            "type": ["CURRENT"],
            "acceptable_duration": [-1],
        }
    )

    out = attach_current_limits(df, incomplete_limits)

    assert out is df


def test_attach_current_limits_no_matching_rows_returns_input():
    df = pd.DataFrame({"i1": [80.0]}, index=["line_1"])
    # Limits present but none of type CURRENT/side ONE match.
    non_matching_limits = pd.DataFrame(
        {
            "element_id": ["line_1"],
            "side": ["TWO"],
            "type": ["VOLTAGE"],
            "acceptable_duration": [-1],
            "value": [100.0],
        }
    )

    out = attach_current_limits(df, non_matching_limits)

    assert out is df


def test_attach_tap_changer_data_no_tap_changers_returns_copy_unchanged():
    df = pd.DataFrame({"rated_u1": [110.0]}, index=["t1"])

    out = attach_tap_changer_data(df, None, None, "2_windings_transformers")

    assert out is not df
    assert out.equals(df)


def test_attach_tap_changer_data_missing_required_tap_columns_is_skipped():
    df = pd.DataFrame({"rated_u1": [110.0]}, index=["t1"])
    # ratio_df lacks tap/low_tap/high_tap so it should be skipped entirely.
    ratio_df = pd.DataFrame({"side": ["ONE"]}, index=["t1"])

    out = attach_tap_changer_data(df, ratio_df, None, "2_windings_transformers")

    assert "ratio_tap_position" not in out.columns


def test_attach_tap_changer_data_three_windings_missing_side_is_skipped():
    df = pd.DataFrame({"rated_u1": [110.0]}, index=["t1"])
    # Has the required tap columns but no "side" column, required for 3-winding.
    ratio_df = pd.DataFrame({"tap": [1], "low_tap": [0], "high_tap": [2]}, index=["t1"])

    out = attach_tap_changer_data(df, ratio_df, None, "3_windings_transformers")

    assert "ratio_tap_position1" not in out.columns


def test_loading_percent_two_sided_without_limit_column_is_nan():
    df = pd.DataFrame(
        {"i1": [100.0], "i2": [-100.0]},
        index=["line_1"],
    )

    values = compute_metric(df, "loading_percent", "lines")

    assert values.isna().all()


def test_loading_percent_single_sided_without_p_or_max_p_is_nan():
    df = pd.DataFrame({"other": [1.0]}, index=["load_1"])

    values = compute_metric(df, "loading_percent", "loads")

    assert values.isna().all()


def test_compute_metric_p_abs_single_sided_with_p_column():
    df = pd.DataFrame({"p": [-42.0]}, index=["load_1"])

    values = compute_metric(df, "p_abs", "loads")

    assert values.loc["load_1"] == pytest.approx(42.0)


def test_compute_metric_p_abs_single_sided_without_p_column_is_nan():
    df = pd.DataFrame({"other": [1.0]}, index=["load_1"])

    values = compute_metric(df, "p_abs", "loads")

    assert values.isna().all()


def test_compute_metric_q_abs_two_sided_uses_worst_side():
    df = pd.DataFrame(
        {"q1": [10.0, -60.0], "q2": [-20.0, 30.0]}, index=["line_1", "line_2"]
    )

    values = compute_metric(df, "q_abs", "lines")

    assert values.loc["line_1"] == pytest.approx(20.0)
    assert values.loc["line_2"] == pytest.approx(60.0)


def test_compute_metric_q_abs_single_sided_with_q_column():
    df = pd.DataFrame({"q": [-15.0]}, index=["load_1"])

    values = compute_metric(df, "q_abs", "loads")

    assert values.loc["load_1"] == pytest.approx(15.0)


def test_compute_metric_q_abs_single_sided_without_q_column_is_nan():
    df = pd.DataFrame({"other": [1.0]}, index=["load_1"])

    values = compute_metric(df, "q_abs", "loads")

    assert values.isna().all()


def test_apply_element_filter_requires_metric():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    with pytest.raises(ValueError, match="metric is required"):
        apply_element_filter(
            df,
            element_type="generators",
            metric="",
            filter_op=">",
            filter_value=1,
        )
