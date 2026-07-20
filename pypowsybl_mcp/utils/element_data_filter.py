#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

"""Filter a network element table on one metric and one threshold.

Used when get_network_element_data is called with mode="filter", for example
to return only lines where loading_percent is above 90.
"""

import operator

import pandas as pd

# The MCP client sends filter_op as a string (">", "gt", ...). We map it to
# a real comparison function from the standard library operator module.
COMPARISONS = {
    ">": operator.gt,
    "gt": operator.gt,
    ">=": operator.ge,
    "ge": operator.ge,
    "<": operator.lt,
    "lt": operator.lt,
    "<=": operator.le,
    "le": operator.le,
    "==": operator.eq,
    "eq": operator.eq,
    "!=": operator.ne,
    "ne": operator.ne,
}

# These metrics are not columns in the pypowsybl table; we compute them here.
DERIVED_METRICS = ("loading_percent", "p_abs", "q_abs")

# Lines and transformers have two terminals (side 1 and side 2).
TWO_SIDED_TYPES = ("lines", "hvdc_lines", "transformers", "2_windings_transformers")


def _max_abs(df, columns):
    """Return the highest absolute value among the given columns, for each row.

    On a line, i1 and i2 are almost the same current seen from each end.
    For loading we care about the worst side, so we take the max of |i1| and |i2|.
    """
    present = [c for c in columns if c in df.columns]
    if not present:
        return pd.Series(float("nan"), index=df.index)
    return df[present].abs().max(axis=1)


def _select_current_limit(limits, limit_kind):
    """Pick one ampacity per element from get_operational_limits().

    Both permanent and temporary limits live in the same table. We read
    type CURRENT, side ONE, and choose by acceptable_duration:
      - permanent: duration -1 (normal N rating)
      - temporary: duration > 0; if several exist, keep the longest duration;
        if none exist for an element, use its permanent rating instead.
    """
    current = limits[(limits["type"] == "CURRENT") & (limits["side"] == "ONE")]
    permanent = current[current["acceptable_duration"] == -1]
    permanent_by_id = (
        permanent.drop_duplicates("element_id").set_index("element_id")["value"]
    )
    if limit_kind != "temporary":
        return permanent_by_id
    temporary = current[current["acceptable_duration"] > 0]
    # Keep the rating with the longest allowed duration for each element.
    temporary_by_id = (
        temporary.sort_values("acceptable_duration")
        .drop_duplicates("element_id", keep="last")
        .set_index("element_id")["value"]
    )
    # Use the temporary rating where it exists, otherwise the permanent one.
    return temporary_by_id.combine_first(permanent_by_id)


def attach_current_limits(df, limits_df, limit_kind="permanent"):
    """Copy the chosen ampacity from operational limits onto each line row.

    get_lines() has i1/i2 but not the thermal limit. We add current_limit1 so
    loading_percent can divide current by the right rating (permanent or
    temporary, see limit_kind on get_network_element_data).
    """
    if "current_limit1" in df.columns:
        return df
    if limits_df is None or limits_df.empty:
        return df

    limits = limits_df.reset_index()
    required = ("element_id", "side", "type", "acceptable_duration", "value")
    if any(col not in limits.columns for col in required):
        return df

    limit_by_id = _select_current_limit(limits, limit_kind)
    if limit_by_id.empty:
        return df

    out = df.copy()
    out["current_limit1"] = out.index.map(limit_by_id)
    return out


# pypowsybl side names to the leg number used in 3-winding transformer columns
# (rated_u1, ratio_tap_position1, ...). ONE -> leg 1, TWO -> leg 2, THREE -> leg 3.
_SIDE_TO_LEG = {"ONE": 1, "TWO": 2, "THREE": 3}


def attach_tap_changer_data(elements_df, ratio_df, phase_df, element_type):
    """Add tap-changer information (position, range, regulated side) to a
    transformer element table.

    pypowsybl returns transformer tables (get_2_windings_transformers,
    get_3_windings_transformers) without the details of their tap changers.
    Those live in separate tables (get_ratio_tap_changers,
    get_phase_tap_changers) indexed by transformer id, each row exposing:
      - tap: the current tap position
      - low_tap / high_tap: the lowest / highest reachable tap position
      - side: for 3-winding transformers, the leg the tap changer sits on
      - regulating: whether the tap changer currently regulates

    We join the useful columns here so a single get_network_element_data call
    exposes, for each transformer, tap_position, tap_min, tap_max and
    regulated_side.

    Two-winding transformers get flat columns prefixed with ratio_/phase_
    (e.g. ratio_tap_position, ratio_tap_min, ratio_tap_max,
    ratio_regulated_side). Three-winding transformers get the same columns
    suffixed with the leg number (1, 2, 3) to match the existing
    rated_u1/ratio_tap_position1 convention.
    """
    out = elements_df.copy()
    is_three_windings = element_type == "3_windings_transformers"

    for kind, tc_df in (("ratio", ratio_df), ("phase", phase_df)):
        if tc_df is None or tc_df.empty:
            continue
        cols = tc_df.columns
        if not {"tap", "low_tap", "high_tap"}.issubset(cols):
            continue

        if is_three_windings:
            if "side" not in cols:
                continue
            for side_name, leg in _SIDE_TO_LEG.items():
                leg_rows = tc_df[tc_df["side"] == side_name]
                if leg_rows.empty:
                    continue
                leg_rows = leg_rows[~leg_rows.index.duplicated(keep="first")]
                out[f"{kind}_tap_position{leg}"] = out.index.map(leg_rows["tap"])
                out[f"{kind}_tap_min{leg}"] = out.index.map(leg_rows["low_tap"])
                out[f"{kind}_tap_max{leg}"] = out.index.map(leg_rows["high_tap"])
                if "regulating" in cols:
                    out[f"{kind}_regulating{leg}"] = out.index.map(
                        leg_rows["regulating"]
                    )
        else:
            rows = tc_df[~tc_df.index.duplicated(keep="first")]
            out[f"{kind}_tap_position"] = out.index.map(rows["tap"])
            out[f"{kind}_tap_min"] = out.index.map(rows["low_tap"])
            out[f"{kind}_tap_max"] = out.index.map(rows["high_tap"])
            if "side" in cols:
                out[f"{kind}_regulated_side"] = out.index.map(rows["side"])
            if "regulating" in cols:
                out[f"{kind}_regulating"] = out.index.map(rows["regulating"])

    return out


def _loading_percent(df, element_type):
    """Loading in percent: measured value divided by limit, times 100.

    Lines: max(|i1|, |i2|) / Imax * 100  (both in amperes, no unit conversion).
    Generators/loads: |p| / max_p * 100.
    """
    if element_type in TWO_SIDED_TYPES:
        current = _max_abs(df, ["i1", "i2"])
        for col in ("current_limit1", "permanent_limit1", "rated1", "permanent_limit"):
            if col in df.columns:
                limit = df[col].astype(float).replace(0, float("nan"))
                return current / limit * 100
        return pd.Series(float("nan"), index=df.index)

    if "p" in df.columns and "max_p" in df.columns:
        limit = df["max_p"].astype(float).replace(0, float("nan"))
        return df["p"].abs() / limit * 100
    return pd.Series(float("nan"), index=df.index)


def compute_metric(df, metric, element_type):
    """Return one numeric value per row for the metric used in the filter."""
    if metric in df.columns:
        return df[metric]

    if metric == "loading_percent":
        return _loading_percent(df, element_type)

    if metric == "p_abs":
        if element_type in TWO_SIDED_TYPES:
            return _max_abs(df, ["p1", "p2"])
        if "p" in df.columns:
            return df["p"].abs()
        return pd.Series(float("nan"), index=df.index)

    if metric == "q_abs":
        if element_type in TWO_SIDED_TYPES:
            return _max_abs(df, ["q1", "q2"])
        if "q" in df.columns:
            return df["q"].abs()
        return pd.Series(float("nan"), index=df.index)

    cols = ", ".join(sorted(df.columns.astype(str)))
    raise ValueError(
        f"Unknown metric '{metric}' for '{element_type}'. "
        f"Try loading_percent, p_abs, q_abs, or a column name ({cols})"
    )


def apply_element_filter(df, *, element_type, metric, filter_op, filter_value, sort="desc"):
    """Keep rows where metric filter_op filter_value, then sort the result.

    Example: metric="loading_percent", filter_op=">", filter_value=90
    keeps lines loaded above 90% and sorts them by loading (desc by default).
    Rows with a missing metric (NaN) are dropped because we cannot compare them.
    """
    if not metric or str(metric).strip() == "":
        raise ValueError("metric is required when mode='filter'")

    op = str(filter_op).strip().lower()
    compare = COMPARISONS.get(op)
    if compare is None:
        raise ValueError(f"Unknown filter_op '{filter_op}'")

    try:
        threshold = float(filter_value)
    except (TypeError, ValueError):
        raise ValueError(f"filter_value must be a number, got {filter_value!r}")

    values = compute_metric(df, metric, element_type)
    mask = compare(values, threshold) & values.notna()
    result = df[mask].copy()

    # If loading_percent was computed, add it to the output so the client sees it.
    if metric not in result.columns:
        result[metric] = values[mask]

    if result.empty:
        return result

    ascending = str(sort).strip().lower() in ("asc", "ascending")
    return result.sort_values(metric, ascending=ascending)
