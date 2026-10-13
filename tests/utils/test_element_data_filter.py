#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock

import pandas as pd
import pytest

from pypowsybl_mcp.utils.element_data_filter import (
    FilterError,
    apply_element_filter,
    attach_current_limits,
    attach_substation_geotags,
    attach_tap_changer_data,
    attach_voltage_limits,
    compute_metric,
    dimensioning_terminal,
)


def _geo_network(*, with_region=True, converters=False):
    """A MagicMock network wired with substation/voltage-level geography.

    S1 -> FR (region PARIS), S2 -> ES (region ES). Voltage levels VL1/VL2 sit on
    S1/S2. When `converters` is set, converter stations CS1/CS2 live on VL1/VL2.
    """
    net = MagicMock()
    sub_cols = {"country": ["FR", "ES"]}
    if with_region:
        sub_cols["regionCvg"] = ["PARIS", "ES"]
    net.get_substations.return_value = pd.DataFrame(sub_cols, index=["S1", "S2"])
    net.get_voltage_levels.return_value = pd.DataFrame(
        {
            "substation_id": ["S1", "S2"],
            "low_voltage_limit": [380.0, 210.0],
            "high_voltage_limit": [420.0, 240.0],
        },
        index=["VL1", "VL2"],
    )
    if converters:
        net.get_vsc_converter_stations.return_value = pd.DataFrame(
            {"voltage_level_id": ["VL1"]}, index=["CS1"]
        )
        net.get_lcc_converter_stations.return_value = pd.DataFrame(
            {"voltage_level_id": ["VL2"]}, index=["CS2"]
        )
    return net


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
        element_type="generator",
        conditions=[{"field": "p", "op": ">=", "value": 5}],
        order_by="-p",
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
        element_type="generator",
        conditions=[{"field": "loading_percent", "op": ">", "value": 90}],
        order_by="-loading_percent",
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
        element_type="line",
        conditions=[{"field": "p_abs", "op": "gt", "value": 100}],
        order_by="-p_abs",
    )

    assert list(out.index) == ["line_2"]
    assert out.loc["line_2", "p_abs"] == pytest.approx(120.0)


def test_unknown_operator_is_rejected():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    with pytest.raises(FilterError, match="op"):
        apply_element_filter(
            df,
            element_type="generator",
            conditions=[{"field": "p", "op": "??", "value": 1}],
        )


def test_non_numeric_threshold_is_rejected():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    with pytest.raises(FilterError, match="value must be a number"):
        apply_element_filter(
            df,
            element_type="generator",
            conditions=[{"field": "p", "op": ">", "value": "not-a-number"}],
        )


def test_filter_column_vs_column_with_ref():
    # i1 above its own per-row rating: a leaf comparing two columns via ref.
    df = pd.DataFrame(
        {"i1": [80.0, 250.0], "i2": [0.0, 0.0], "permanent_limit1": [100.0, 200.0]},
        index=["line_1", "line_2"],
    )

    out = apply_element_filter(
        df,
        element_type="line",
        conditions=[{"field": "i1", "op": ">", "ref": "permanent_limit1"}],
    )

    assert list(out.index) == ["line_2"]


def test_filter_categorical_equality_passes_strings_through():
    df = pd.DataFrame(
        {"country": ["FR", "BE", "FR"]}, index=["l1", "l2", "l3"]
    )

    out = apply_element_filter(
        df,
        element_type="line",
        conditions=[{"field": "country", "op": "==", "value": "FR"}],
    )

    assert sorted(out.index) == ["l1", "l3"]


def test_filter_membership_in_and_not_in():
    df = pd.DataFrame(
        {"country": ["FR", "BE", "DE", "ES"]}, index=["l1", "l2", "l3", "l4"]
    )

    inside = apply_element_filter(
        df,
        element_type="line",
        conditions=[{"field": "country", "op": "in", "value": ["FR", "BE"]}],
    )
    outside = apply_element_filter(
        df,
        element_type="line",
        conditions=[{"field": "country", "op": "not_in", "value": ["FR", "BE"]}],
    )

    assert sorted(inside.index) == ["l1", "l2"]
    assert sorted(outside.index) == ["l3", "l4"]


def test_membership_op_requires_a_list_value():
    df = pd.DataFrame({"country": ["FR"]}, index=["l1"])

    with pytest.raises(FilterError, match="list"):
        apply_element_filter(
            df,
            element_type="line",
            conditions=[{"field": "country", "op": "in", "value": "FR"}],
        )


def test_flat_list_is_implicit_and():
    df = pd.DataFrame(
        {"p": [10.0, 60.0, 80.0], "q": [5.0, 5.0, 50.0]},
        index=["a", "b", "c"],
    )

    out = apply_element_filter(
        df,
        element_type="generator",
        conditions=[
            {"field": "p", "op": ">", "value": 50},
            {"field": "q", "op": ">", "value": 10},
        ],
    )

    assert list(out.index) == ["c"]


def test_any_group_is_or():
    df = pd.DataFrame(
        {"p": [10.0, 60.0, 5.0], "q": [5.0, 5.0, 50.0]},
        index=["a", "b", "c"],
    )

    out = apply_element_filter(
        df,
        element_type="generator",
        conditions={
            "any": [
                {"field": "p", "op": ">", "value": 50},
                {"field": "q", "op": ">", "value": 10},
            ]
        },
        order_by="p",
    )

    assert sorted(out.index) == ["b", "c"]


def test_conditions_accepts_a_json_string():
    df = pd.DataFrame({"p": [1.0, 10.0]}, index=["a", "b"])

    out = apply_element_filter(
        df,
        element_type="generator",
        conditions='[{"field": "p", "op": ">", "value": 5}]',
    )

    assert list(out.index) == ["b"]


def test_bad_json_conditions_is_rejected():
    df = pd.DataFrame({"p": [1.0]}, index=["a"])

    with pytest.raises(FilterError, match="valid JSON"):
        apply_element_filter(
            df, element_type="generator", conditions="[{not json}]"
        )


def test_empty_group_is_rejected():
    df = pd.DataFrame({"p": [1.0]}, index=["a"])

    with pytest.raises(FilterError, match="non-empty"):
        apply_element_filter(
            df, element_type="generator", conditions={"all": []}
        )


def test_conditions_leaf_count_cap():
    df = pd.DataFrame({"p": [1.0]}, index=["a"])
    too_many = [{"field": "p", "op": ">", "value": 0} for _ in range(101)]

    with pytest.raises(FilterError, match="too many conditions"):
        apply_element_filter(df, element_type="generator", conditions=too_many)


def test_unknown_metric_lists_available_columns():
    df = pd.DataFrame({"p": [1.0], "q": [2.0]}, index=["g1"])

    with pytest.raises(ValueError, match="Unknown metric"):
        compute_metric(df, "does_not_exist", "generator")


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
        element_type="line",
        conditions=[{"field": "loading_percent", "op": ">", "value": 90}],
        order_by="-loading_percent",
    )

    assert list(out.index) == ["line_1"]
    assert out.loc["line_1", "loading_percent"] == pytest.approx(100.0)


def test_max_abs_returns_nan_when_no_matching_columns():
    # Lines with neither p1/p2 nor q1/q2: computing p_abs/q_abs should yield NaN
    # for every row rather than raising.
    df = pd.DataFrame({"other": [1.0, 2.0]}, index=["line_1", "line_2"])

    values = compute_metric(df, "p_abs", "line")

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

    out = attach_tap_changer_data(df, None, None, "two_windings_transformer")

    assert out is not df
    assert out.equals(df)


def test_attach_tap_changer_data_missing_required_tap_columns_is_skipped():
    df = pd.DataFrame({"rated_u1": [110.0]}, index=["t1"])
    # ratio_df lacks tap/low_tap/high_tap so it should be skipped entirely.
    ratio_df = pd.DataFrame({"side": ["ONE"]}, index=["t1"])

    out = attach_tap_changer_data(df, ratio_df, None, "two_windings_transformer")

    assert "ratio_tap_position" not in out.columns


def test_attach_tap_changer_data_three_windings_missing_side_is_skipped():
    df = pd.DataFrame({"rated_u1": [110.0]}, index=["t1"])
    # Has the required tap columns but no "side" column, required for 3-winding.
    ratio_df = pd.DataFrame({"tap": [1], "low_tap": [0], "high_tap": [2]}, index=["t1"])

    out = attach_tap_changer_data(df, ratio_df, None, "three_windings_transformer")

    assert "ratio_tap_position1" not in out.columns


def test_loading_percent_two_sided_without_limit_column_is_nan():
    df = pd.DataFrame(
        {"i1": [100.0], "i2": [-100.0]},
        index=["line_1"],
    )

    values = compute_metric(df, "loading_percent", "line")

    assert values.isna().all()


def test_loading_percent_single_sided_without_p_or_max_p_is_nan():
    df = pd.DataFrame({"other": [1.0]}, index=["load_1"])

    values = compute_metric(df, "loading_percent", "load")

    assert values.isna().all()


def test_compute_metric_p_abs_single_sided_with_p_column():
    df = pd.DataFrame({"p": [-42.0]}, index=["load_1"])

    values = compute_metric(df, "p_abs", "load")

    assert values.loc["load_1"] == pytest.approx(42.0)


def test_compute_metric_p_abs_single_sided_without_p_column_is_nan():
    df = pd.DataFrame({"other": [1.0]}, index=["load_1"])

    values = compute_metric(df, "p_abs", "load")

    assert values.isna().all()


def test_compute_metric_q_abs_two_sided_uses_worst_side():
    df = pd.DataFrame(
        {"q1": [10.0, -60.0], "q2": [-20.0, 30.0]}, index=["line_1", "line_2"]
    )

    values = compute_metric(df, "q_abs", "line")

    assert values.loc["line_1"] == pytest.approx(20.0)
    assert values.loc["line_2"] == pytest.approx(60.0)


def test_compute_metric_q_abs_single_sided_with_q_column():
    df = pd.DataFrame({"q": [-15.0]}, index=["load_1"])

    values = compute_metric(df, "q_abs", "load")

    assert values.loc["load_1"] == pytest.approx(15.0)


def test_compute_metric_q_abs_single_sided_without_q_column_is_nan():
    df = pd.DataFrame({"other": [1.0]}, index=["load_1"])

    values = compute_metric(df, "q_abs", "load")

    assert values.isna().all()


def test_apply_element_filter_requires_conditions():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    with pytest.raises(FilterError, match="conditions is required"):
        apply_element_filter(df, element_type="generator", conditions=None)


def test_leaf_needs_exactly_one_of_value_or_ref():
    df = pd.DataFrame({"p": [1.0], "max_p": [2.0]}, index=["g1"])

    with pytest.raises(FilterError, match="exactly one of 'value' or 'ref'"):
        apply_element_filter(
            df,
            element_type="generator",
            conditions=[{"field": "p", "op": ">", "value": 1, "ref": "max_p"}],
        )


def test_loading_percent_two_sided_rates_each_side_against_its_own_limit():
    # A transformer: side 1 at 100 A / 200 A = 50%, side 2 at 300 A / 400 A = 75%.
    # The worst side is 75%, NOT max(100, 300) / 200 = 150% (the old single-limit bug).
    df = pd.DataFrame(
        {
            "i1": [100.0],
            "i2": [300.0],
            "permanent_limit1": [200.0],
            "permanent_limit2": [400.0],
        },
        index=["t1"],
    )

    out = apply_element_filter(
        df,
        element_type="two_windings_transformer",
        conditions=[{"field": "loading_percent", "op": ">", "value": 0}],
    )

    assert out.loc["t1", "loading_percent"] == pytest.approx(75.0)


def test_loading_percent_three_terminals_uses_worst_winding():
    # A 3-winding transformer: terminal count is read from i1/i2/i3, and the worst
    # winding (i3 300 A / 300 A = 100%) wins over the other two.
    df = pd.DataFrame(
        {
            "i1": [100.0],
            "i2": [150.0],
            "i3": [300.0],
            "permanent_limit1": [400.0],
            "permanent_limit2": [400.0],
            "permanent_limit3": [300.0],
        },
        index=["t3"],
    )

    out = apply_element_filter(
        df,
        element_type="three_windings_transformer",
        conditions=[{"field": "loading_percent", "op": ">", "value": 0}],
    )

    assert out.loc["t3", "loading_percent"] == pytest.approx(100.0)


def test_loading_percent_single_limit_falls_back_to_worst_current():
    # Only a side-1 rating (a line's single conductor Imax): the worst-end current
    # is divided by that shared limit -> 180/200 = 90%, side 2 borrowing limit1.
    df = pd.DataFrame(
        {"i1": [100.0], "i2": [180.0], "permanent_limit1": [200.0]},
        index=["l1"],
    )

    out = apply_element_filter(
        df,
        element_type="line",
        conditions=[{"field": "loading_percent", "op": ">", "value": 0}],
    )

    assert out.loc["l1", "loading_percent"] == pytest.approx(90.0)


def test_dimensioning_terminal_picks_the_worst_side():
    # side 1: 100/200 = 50%, side 2: 300/400 = 75% -> side 2 dimensions the row.
    row = {
        "i1": 100.0,
        "i2": 300.0,
        "permanent_limit1": 200.0,
        "permanent_limit2": 400.0,
    }

    i_a, limit_a = dimensioning_terminal(row, "permanent")

    assert i_a == pytest.approx(300.0)
    assert limit_a == pytest.approx(400.0)


def test_dimensioning_terminal_single_limit_is_shared():
    # Only a side-1 rating (a line's Imax): worst current over the shared limit.
    row = {"i1": 100.0, "i2": 180.0, "permanent_limit1": 200.0}

    i_a, limit_a = dimensioning_terminal(row, "permanent")

    assert i_a == pytest.approx(180.0)
    assert limit_a == pytest.approx(200.0)


def test_dimensioning_terminal_none_without_a_resolvable_limit():
    assert dimensioning_terminal({"i1": 100.0, "i2": 100.0}, "permanent") == (None, None)


def test_loading_percent_temporary_is_referenceable():
    # temporary_limit1 is the temporary denominator; falls back to permanent
    # only when absent. Here line_1 loads 100/120 ~ 83%, line_2 100/50 = 200%.
    df = pd.DataFrame(
        {
            "i1": [100.0, 100.0],
            "i2": [0.0, 0.0],
            "temporary_limit1": [120.0, 50.0],
        },
        index=["line_1", "line_2"],
    )

    out = apply_element_filter(
        df,
        element_type="line",
        conditions=[{"field": "loading_percent_temporary", "op": ">", "value": 100}],
    )

    assert list(out.index) == ["line_2"]
    assert out.loc["line_2", "loading_percent_temporary"] == pytest.approx(200.0)


def test_attach_geotags_injection_single_columns():
    df = pd.DataFrame({"voltage_level_id": ["VL1", "VL2"]}, index=["LOAD1", "LOAD2"])

    out = attach_substation_geotags(df, _geo_network(), "load")

    assert list(out["country"]) == ["FR", "ES"]
    assert list(out["regionCvg"]) == ["PARIS", "ES"]


def test_attach_geotags_branch_two_sided_columns():
    df = pd.DataFrame(
        {"voltage_level1_id": ["VL1"], "voltage_level2_id": ["VL2"]},
        index=["L_FR_ES"],
    )

    out = attach_substation_geotags(df, _geo_network(), "line")

    assert out.loc["L_FR_ES", "country1"] == "FR"
    assert out.loc["L_FR_ES", "country2"] == "ES"


def test_attach_geotags_hvdc_resolves_via_converter_stations():
    df = pd.DataFrame(
        {"converter_station1_id": ["CS1"], "converter_station2_id": ["CS2"]},
        index=["HVDC1"],
    )

    out = attach_substation_geotags(df, _geo_network(converters=True), "hvdc_line")

    assert out.loc["HVDC1", "country1"] == "FR"
    assert out.loc["HVDC1", "country2"] == "ES"


def test_attach_geotags_missing_region_property_yields_empty_column():
    df = pd.DataFrame({"voltage_level_id": ["VL1"]}, index=["LOAD1"])

    out = attach_substation_geotags(df, _geo_network(with_region=False), "load")

    # country still resolves; regionCvg is present but empty (NA), never an error.
    assert out.loc["LOAD1", "country"] == "FR"
    assert pd.isna(out.loc["LOAD1", "regionCvg"])


def test_attach_geotags_substation_type_is_unchanged():
    df = pd.DataFrame({"country": ["FR"]}, index=["S1"])

    out = attach_substation_geotags(df, _geo_network(), "substation")

    assert out is df


def test_attach_voltage_limits_on_bus_rows():
    df = pd.DataFrame(
        {"v_mag": [410.0, 215.0], "voltage_level_id": ["VL1", "VL2"]},
        index=["B1", "B2"],
    )

    out = attach_voltage_limits(df, _geo_network(), "bus")

    assert out.loc["B1", "high_voltage_limit"] == pytest.approx(420.0)
    assert out.loc["B2", "low_voltage_limit"] == pytest.approx(210.0)


def test_attach_voltage_limits_non_bus_is_unchanged():
    df = pd.DataFrame({"p": [1.0]}, index=["g1"])

    out = attach_voltage_limits(df, _geo_network(), "generator")

    assert out is df
