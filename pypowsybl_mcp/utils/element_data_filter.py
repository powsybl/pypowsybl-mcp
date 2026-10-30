#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Evaluate a structured ``conditions`` predicate over a network element table.

A condition is a leaf ``{"field", "op", "value"|"ref"}`` or an
``{"all"|"any": [...]}`` group that nests AND/OR; a bare list is an implicit AND.
Fields resolve through compute_metric (a real column or a derived metric such as
loading_percent), so no user string is ever tokenised, compiled, or eval'd.

Used when get_network_element_data is called with a ``conditions`` argument, for
example to return only lines where loading_percent is above 90.
"""

import json
import operator

import pandas as pd

# A leaf op is a string (">", "gt", "==", ...). We map it to a real comparison
# function from the standard library operator module. Membership ops (in/not_in)
# live in MEMBERSHIP below; they evaluate via Series.isin, not an operator.*.
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

# Membership ops take a list value and evaluate via Series.isin, so they are
# kept apart from the operator.* comparisons above.
MEMBERSHIP = {
    "in": lambda series, values: series.isin(values),
    "not_in": lambda series, values: ~series.isin(values),
}

# Ordered comparisons require a numeric operand; equality/inequality accept
# strings and bools unchanged (categorical support, no float coercion).
_ORDERED_OPS = {">", "gt", ">=", "ge", "<", "lt", "<=", "le"}

# Guard a pathological conditions tree from an LLM/HTTP caller (cheap DoS vector).
_MAX_CONDITION_DEPTH = 25
_MAX_CONDITION_LEAVES = 100


class FilterError(ValueError):
    """A malformed or invalid ``conditions`` predicate.

    Subclasses ValueError so the existing ``except ValueError`` handler in
    get_network_element_data still turns it into a friendly JSON error.
    """


# These metrics are not columns in the pypowsybl table; we compute them here.
# unrated_sides counts the energised terminals a branch cannot be checked on (a
# current but no resolvable permanent rating) — the coverage gap the overload
# check would otherwise skip silently.
DERIVED_METRICS = (
    "loading_percent",
    "loading_percent_temporary",
    "p_abs",
    "q_abs",
    "unrated_sides",
)

# Lines and transformers have two terminals (side 1 and side 2).
TWO_SIDED_TYPES = ("line", "hvdc_line", "two_windings_transformer")

# Transformer windings do NOT share a current: they carry currents in inverse
# ratio of their rated voltages, so a borrowed ampere rating must be rescaled by
# the voltage ratio (see _resolve_effective_limits). Lines/tie-lines share a
# current and are not listed here, keeping the direct shared-Imax borrow.
_TRANSFORMER_TYPES = ("two_windings_transformer", "three_windings_transformer")


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
    permanent_by_id = permanent.drop_duplicates("element_id").set_index("element_id")[
        "value"
    ]
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


# 0 is the index pypowsybl gives the main (largest) connected and synchronous
# component, so "in the main area" means the component label equals 0.
MAIN_COMPONENT_INDEX = 0

# Element tables address their terminals through these bus-id columns: an
# injection has one (bus_id), a branch two or three (bus1_id..bus3_id).
_BUS_ID_COLUMNS = ("bus_id", "bus1_id", "bus2_id", "bus3_id")


# The shared component-scoping vocabulary (surface-conventions §5). Replaces the
# main_connected_component / main_synchronous_component boolean pair with one enum.
_MAIN_COMPONENT_MODES = {
    "both": (True, True),
    "connected": (True, False),
    "synchronous": (False, True),
    "none": (False, False),
}


def parse_main_component(main_component):
    """Unpack main_component into the (connected, synchronous) boolean pair.

    main_component in {both, connected, synchronous, none}; "both" (the default)
    intersects the two filters, "none" applies neither. Raises FilterError on an
    unknown value so the tool surfaces a friendly message.
    """
    value = (main_component or "both").strip().lower()
    if value not in _MAIN_COMPONENT_MODES:
        raise FilterError(
            f"Invalid main_component '{main_component}': expected one of "
            f"{', '.join(_MAIN_COMPONENT_MODES)}"
        )
    return _MAIN_COMPONENT_MODES[value]


def main_area_bus_ids(network, *, main_connected_component, main_synchronous_component):
    """Ids of the buses in the requested "main" area, or None for no filter.

    Returns None when no restriction is asked for (both flags False) so callers
    skip the work. When both flags are True the conditions are intersected: a
    bus must be in the main connected AND the main synchronous component
    (component index 0 in each case). Degrades to None (no filter) when the
    component labels are unavailable, rather than returning an empty set that
    would drop every element.
    """
    if not main_connected_component and not main_synchronous_component:
        return None
    buses = network.get_buses(
        attributes=["connected_component", "synchronous_component"]
    )
    if not isinstance(buses, pd.DataFrame):
        return None
    if main_connected_component and "connected_component" not in buses.columns:
        return None
    if main_synchronous_component and "synchronous_component" not in buses.columns:
        return None
    mask = pd.Series(True, index=buses.index)
    if main_connected_component:
        mask &= buses["connected_component"] == MAIN_COMPONENT_INDEX
    if main_synchronous_component:
        mask &= buses["synchronous_component"] == MAIN_COMPONENT_INDEX
    return set(buses.index[mask])


def _hvdc_bus_columns(network, elements_df):
    """Resolve each HVDC line's two ends to a bus via its converter stations.

    HVDC lines carry no bus id; they reference two converter stations, which
    are the elements actually attached to a bus. Both station kinds (VSC and
    LCC) are collected. Returns a frame with bus1_id/bus2_id aligned to
    elements_df, unresolved ends left as NaN.
    """
    bus_by_station = {}
    for getter in ("get_vsc_converter_stations", "get_lcc_converter_stations"):
        stations = getattr(network, getter)()
        if isinstance(stations, pd.DataFrame) and "bus_id" in stations.columns:
            bus_by_station.update(stations["bus_id"].to_dict())
    out = pd.DataFrame(index=elements_df.index)
    for end, col in (("1", "converter_station1_id"), ("2", "converter_station2_id")):
        if col in elements_df.columns:
            out[f"bus{end}_id"] = elements_df[col].map(bus_by_station)
    return out


def filter_elements_to_main_area(elements_df, network, element_type, main_bus_ids):
    """Keep only elements with at least one terminal in the main area.

    `main_bus_ids` comes from main_area_bus_ids(); None means "no filter". An
    element is kept when ANY of its terminal buses is in that set - one-sided
    connection is enough, since a branch energised from a single end still
    belongs to the area. The bus table filters on its own component columns;
    HVDC lines resolve their buses through converter stations; element types
    with no resolvable bus column are returned unchanged.
    """
    if main_bus_ids is None or elements_df.empty:
        return elements_df

    # The bus table carries its own component labels and is indexed by bus id.
    if {"connected_component", "synchronous_component"} <= set(elements_df.columns):
        return elements_df[elements_df.index.isin(main_bus_ids)]

    if element_type == "hvdc_line":
        bus_cols_df = _hvdc_bus_columns(network, elements_df)
    else:
        present = [c for c in _BUS_ID_COLUMNS if c in elements_df.columns]
        if not present:
            return elements_df  # nothing addressable by bus; leave as-is
        bus_cols_df = elements_df[present]

    keep = pd.Series(False, index=elements_df.index)
    for col in bus_cols_df.columns:
        keep |= bus_cols_df[col].isin(main_bus_ids)
    return elements_df[keep]


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


# pypowsybl names a terminal by an ordinal side; the column suffix is its index.
# THREE covers three-winding transformers (empty, hence skipped, on 2-terminal types).
_SIDE_NAME_TO_INDEX = (("ONE", "1"), ("TWO", "2"), ("THREE", "3"))


def _select_per_side_limits(limits):
    """Per-side permanent and temporary ampacity from get_operational_limits().

    Returns a dict of Series keyed by element id, one per materialised column
    (permanent_limit{1,2,3}, temporary_limit{1,2,3}). Temporary falls back to the
    permanent rating for a side where no temporary row exists; a side with no
    CURRENT rows yields an empty Series (skipped by attach_full_current_limits).
    """
    current = limits[limits["type"] == "CURRENT"]
    out = {}
    for side, suffix in _SIDE_NAME_TO_INDEX:
        side_rows = current[current["side"] == side]
        permanent = (
            side_rows[side_rows["acceptable_duration"] == -1]
            .drop_duplicates("element_id")
            .set_index("element_id")["value"]
        )
        temporary = (
            side_rows[side_rows["acceptable_duration"] > 0]
            .sort_values("acceptable_duration")
            .drop_duplicates("element_id", keep="last")
            .set_index("element_id")["value"]
        )
        out[f"permanent_limit{suffix}"] = permanent
        out[f"temporary_limit{suffix}"] = temporary.combine_first(permanent)
    return out


def attach_full_current_limits(df, limits_df):
    """Materialise the full ampacity rating set as columns (the current_limits
    attach token): permanent_limit{1,2,3} and temporary_limit{1,2,3}, in amperes.

    Generalises attach_current_limits (which adds only current_limit1) so the
    derived metrics loading_percent (permanent) and loading_percent_temporary
    (temporary) both resolve, and so a leaf can compare against a per-row rating
    (e.g. i1 > permanent_limit1). Absent ratings are left as NaN; a side's
    temporary falls back to its permanent rating.
    """
    if limits_df is None or getattr(limits_df, "empty", True):
        return df
    limits = limits_df.reset_index()
    required = ("element_id", "side", "type", "acceptable_duration", "value")
    if any(col not in limits.columns for col in required):
        return df
    per_side = _select_per_side_limits(limits)
    out = df.copy()
    for column, series in per_side.items():
        if not series.empty:
            out[column] = out.index.map(series)
    return out


# Geographical tags attached from the substation. country is the ISO code
# (always present); regionCvg is an IIDM property on RTE snapshots (operator
# region for domestic substations, country ISO for foreign ones) and is simply
# absent on generic networks.
_GEOTAG_COLUMNS = ("country", "regionCvg")


def _substation_geotags(network):
    """Map substation_id -> {country, regionCvg} as a frame.

    Reads get_substations(all_attributes=True), which surfaces the regionCvg
    property as a column on datasets that define it. A missing regionCvg yields
    an empty (all-NA) column rather than an error, so a query that references it
    simply matches nothing on networks without the property.
    """
    subs = network.get_substations(all_attributes=True)
    if not isinstance(subs, pd.DataFrame):
        return pd.DataFrame(columns=_GEOTAG_COLUMNS)
    out = pd.DataFrame(index=subs.index)
    for tag in _GEOTAG_COLUMNS:
        out[tag] = subs[tag] if tag in subs.columns else pd.NA
    return out


def _voltage_level_to_substation(network):
    """Series mapping voltage_level_id -> substation_id (variant-independent)."""
    vls = network.get_voltage_levels()
    if isinstance(vls, pd.DataFrame) and "substation_id" in vls.columns:
        return vls["substation_id"]
    return pd.Series(dtype=object)


def _converter_station_voltage_levels(network):
    """Map each converter station id -> its voltage_level_id (VSC and LCC)."""
    vl_by_station = {}
    for getter in ("get_vsc_converter_stations", "get_lcc_converter_stations"):
        stations = getattr(network, getter)()
        if (
            isinstance(stations, pd.DataFrame)
            and "voltage_level_id" in stations.columns
        ):
            vl_by_station.update(stations["voltage_level_id"].to_dict())
    return vl_by_station


def attach_substation_geotags(elements_df, network, element_type):
    """Attach country/regionCvg to each element via the voltage-level hop.

    Elements carry no geography of their own; it lives on the substation. We
    resolve voltage_level -> substation -> {country, regionCvg} and attach:
      - injections (one terminal): single country / regionCvg columns;
      - two-sided branches (line, two_windings_transformer): country1/country2,
        regionCvg1/regionCvg2 (an interconnector spans two countries);
      - hvdc_line: per side, via its converter stations' voltage levels;
      - substation: skipped (the columns are already on the table).
    """
    if elements_df is None or elements_df.empty or element_type == "substation":
        return elements_df

    geotags = _substation_geotags(network)
    vl_to_sub = _voltage_level_to_substation(network)

    def tags_for(vl_series):
        sub = vl_series.map(vl_to_sub)
        return {tag: sub.map(geotags[tag]) for tag in _GEOTAG_COLUMNS}

    out = elements_df.copy()

    if element_type == "hvdc_line":
        vl_by_station = _converter_station_voltage_levels(network)
        for end, col in (
            ("1", "converter_station1_id"),
            ("2", "converter_station2_id"),
        ):
            if col in out.columns:
                tags = tags_for(out[col].map(vl_by_station))
                for tag in _GEOTAG_COLUMNS:
                    out[f"{tag}{end}"] = tags[tag]
        return out

    if "voltage_level1_id" in out.columns or "voltage_level2_id" in out.columns:
        for end in ("1", "2"):
            col = f"voltage_level{end}_id"
            if col in out.columns:
                tags = tags_for(out[col])
                for tag in _GEOTAG_COLUMNS:
                    out[f"{tag}{end}"] = tags[tag]
        return out

    if "voltage_level_id" in out.columns:
        tags = tags_for(out["voltage_level_id"])
        for tag in _GEOTAG_COLUMNS:
            out[tag] = tags[tag]
    return out


def attach_substation_id(elements_df, network, element_type):
    """Attach substation_id to single-terminal elements via the VL hop.

    Branches span two substations, so substation_id is only meaningful for
    injections; left untouched when already present or unresolvable.
    """
    if (
        elements_df is None
        or elements_df.empty
        or "substation_id" in elements_df.columns
    ):
        return elements_df
    if "voltage_level_id" not in elements_df.columns:
        return elements_df
    vl_to_sub = _voltage_level_to_substation(network)
    out = elements_df.copy()
    out["substation_id"] = out["voltage_level_id"].map(vl_to_sub)
    return out


def _vl_nominal_voltage_map(network):
    """Series mapping voltage_level_id -> nominal_v (kV)."""
    vls = network.get_voltage_levels()
    if isinstance(vls, pd.DataFrame) and "nominal_v" in vls.columns:
        return vls["nominal_v"]
    return pd.Series(dtype=float)


def converter_station_nominal_voltages(network, vl_nominal):
    """Map each converter station id -> its voltage level's nominal_v (NaN if none)."""
    out = {}
    for getter in ("get_vsc_converter_stations", "get_lcc_converter_stations"):
        stations = getattr(network, getter)()
        if (
            isinstance(stations, pd.DataFrame)
            and "voltage_level_id" in stations.columns
        ):
            for station_id, vl_id in stations["voltage_level_id"].items():
                out[station_id] = vl_nominal.get(vl_id, float("nan"))
    return out


def attach_nominal_voltage(elements_df, network, element_type):
    """Attach a nominal_v column (kV) resolved from static voltage-level ids.

    The highest of a branch's sides (two for a line/2WT, three for a 3WT, so a
    400/90 kV asset reads 400), the single voltage level for an injection, the
    converter-station AC side for an HVDC line. NaN when unresolvable, so a band
    comparison excludes the element symmetrically rather than bucketing it at 0 kV.
    """
    if elements_df is None or elements_df.empty or "nominal_v" in elements_df.columns:
        return elements_df
    vl_nominal = _vl_nominal_voltage_map(network)
    out = elements_df.copy()

    if element_type == "hvdc_line":
        conv = converter_station_nominal_voltages(network, vl_nominal)
        sides = [
            out[col].map(conv)
            for col in ("converter_station1_id", "converter_station2_id")
            if col in out.columns
        ]
    elif any(f"voltage_level{n}_id" in out.columns for n in (1, 2, 3)):
        # Every side present (line/2WT: 1,2; 3WT: 1,2,3); the skipna max below
        # then yields the higher/highest resolved side.
        sides = [
            out[col].map(vl_nominal)
            for col in ("voltage_level1_id", "voltage_level2_id", "voltage_level3_id")
            if col in out.columns
        ]
    elif "voltage_level_id" in out.columns:
        sides = [out["voltage_level_id"].map(vl_nominal)]
    else:
        sides = []

    if sides:
        # skipna max => the higher resolved side, or NaN when neither resolves.
        out["nominal_v"] = pd.concat(sides, axis=1).max(axis=1)
    else:
        out["nominal_v"] = float("nan")
    return out


def _higher_side(a, b):
    """The higher of two nominal voltages, NaN-aware (NaN if both NaN)."""
    if pd.isna(a):
        return b
    if pd.isna(b):
        return a
    return max(a, b)


def element_nominal_voltage(
    element_type, element_row, voltage_by_level, voltage_by_converter_station=None
):
    """Nominal voltage used to classify one element row (kV), NaN if unresolvable.

    Branch elements take the HIGHER of every side they expose so a mixed-voltage
    asset stays visible in a high-voltage band — the higher of two sides for a
    line / two_windings_transformer, of three legs for a three_windings_transformer
    (read from voltage_level{1,2,3}_id). HVDC lines resolve via their converter
    stations' voltage levels (the AC side); injections take their single voltage
    level. ``element_row`` may be a Series or a plain dict. Returns NaN (not 0)
    when the voltage cannot be resolved, so a band filter excludes the element
    symmetrically instead of bucketing it at 0 kV.
    """
    nan = float("nan")
    if element_row is None:
        return nan

    def nominal(mapping, key):
        return mapping.get(key, nan) if key is not None else nan

    if element_type == "hvdc_line":
        stations = voltage_by_converter_station or {}
        return _higher_side(
            nominal(stations, element_row.get("converter_station1_id")),
            nominal(stations, element_row.get("converter_station2_id")),
        )

    # Fold the higher side over whatever voltage-level ids this row carries: a
    # branch exposes voltage_level{1,2,3}_id (3WT uses all three), an injection a
    # single voltage_level_id. NaN-aware, so missing sides drop out and the side
    # count is never hardcoded.
    higher = nan
    for key in (
        "voltage_level_id",
        "voltage_level1_id",
        "voltage_level2_id",
        "voltage_level3_id",
    ):
        higher = _higher_side(higher, nominal(voltage_by_level, element_row.get(key)))
    return higher


def attach_voltage_limits(df, network, element_type):
    """Attach the voltage level's low/high_voltage_limit (kV) to bus rows.

    The same voltage_levels join check_voltage_violations uses, exposed as
    columns so a conditions query can compare a bus v_mag against its declared
    limits. Only the network-declared limits (no pu fallback) — not a substitute
    for check_voltage_violations.
    """
    if element_type != "bus" or df is None or df.empty:
        return df
    if "voltage_level_id" not in df.columns:
        return df
    vls = network.get_voltage_levels()
    if not isinstance(vls, pd.DataFrame):
        return df
    out = df.copy()
    for col in ("low_voltage_limit", "high_voltage_limit"):
        if col in vls.columns:
            out[col] = out["voltage_level_id"].map(vls[col])
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
    is_three_windings = element_type == "three_windings_transformer"

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


def _side_limit_columns(side, kind):
    """Ordered limit-column preferences for one terminal (1-based `side`).

    Side 1 keeps the legacy fallbacks (current_limit1 / rated1 / permanent_limit)
    for compatibility; every side also accepts permanent_limit{n} / rated{n}. The
    temporary set prepends temporary_limit{n} and otherwise falls back to the
    permanent columns, so loading_percent_temporary is never silently NaN.
    """
    if side == 1:
        permanent = ("current_limit1", "permanent_limit1", "rated1", "permanent_limit")
    else:
        permanent = (f"permanent_limit{side}", f"rated{side}")
    if kind == "temporary":
        return (f"temporary_limit{side}", *permanent)
    return permanent


def _first_present_limit(df, columns):
    """The first present limit column as a float Series (0 -> NaN), else None."""
    for col in columns:
        if col in df.columns:
            return df[col].astype(float).replace(0, float("nan"))
    return None


def _branch_terminals(df):
    """The terminal indices a branch table exposes, by its i1, i2, i3... columns."""
    terminals = []
    side = 1
    while f"i{side}" in df.columns:
        terminals.append(side)
        side += 1
    return terminals


def _transformer_rated_u(df, element_type, terminals):
    """Per-side rated voltage Series for a transformer, else None.

    Returned only for transformer types and only when every ``rated_u{side}``
    column is present. Transformer windings do not share a current, so a borrowed
    ampere rating must be rescaled by the voltage ratio; lines/tie-lines share a
    current and return None, keeping the direct shared-Imax borrow. A 0 rated_u is
    invalid and mapped to NaN so the rescaled borrow is skipped rather than
    dividing by zero.
    """
    if element_type not in _TRANSFORMER_TYPES:
        return None
    cols = {side: f"rated_u{side}" for side in terminals}
    if not all(col in df.columns for col in cols.values()):
        return None
    return {
        side: df[col].astype(float).replace(0, float("nan"))
        for side, col in cols.items()
    }


def _resolve_effective_limits(limit_by_side, rated_u_by_side=None, allow_borrow=True):
    """Map the declared per-side limits onto every terminal, per row.

    The single place the "count the limits, map them onto the sides" rule lives:
    a terminal uses its own rating; a terminal with none borrows a declared one,
    resolved PER ROW, so a branch whose own-side column is NaN on this row (the
    mixed-table case where another branch defines that side's limit) still borrows.

    How the borrow works depends on the element. For a current-sharing element
    (line, tie_line; ``rated_u_by_side`` is None) the borrow is DIRECT — both ends
    carry the same current, so the single conductor Imax applies unchanged. For a
    two-winding transformer (``rated_u_by_side`` given) the windings carry currents
    in inverse ratio of their rated voltages, so the ampere ratings are NOT
    interchangeable: what is shared is the apparent-power limit S = Ilim_k *
    rated_u_k, converted back to a current on each borrowing side as Ilim_j =
    S / rated_u_j (== Ilim_k * rated_u_k / rated_u_j). An HV-side rating thus no
    longer reports a false overload on the LV winding.

    ``allow_borrow=False`` disables borrowing entirely: each terminal keeps only
    its own declared rating and an unrated terminal stays NaN (skipped by the
    worst-terminal max). This is required for a three-winding transformer — a star
    of three windings where S1 ~= S2 + S3 and the legs carry independent
    apparent-power ratings (the tertiary is typically far smaller). A missing
    leg's rating therefore cannot be inferred from a sibling; fabricating one by
    either borrow rule would invent a too-high limit and silently mask a real
    overload. Leaving it NaN fails safe: overloads on the RATED legs are still
    caught, and an unrated leg is never reported as falsely "within limit".

    Returns ``{side: Series}``, or None when no side declares a rating. Both
    _loading_percent and dimensioning_terminal go through here, so they cannot
    disagree.
    """
    declared = [series for series in limit_by_side.values() if series is not None]
    if not declared:
        return None

    if not allow_borrow:
        # No cross-terminal inference (3+ winding transformer): own rating only,
        # NaN where a side declares none, so the worst-terminal max skips it.
        nan_series = pd.Series(float("nan"), index=declared[0].index)
        return {
            side: nan_series if series is None else series
            for side, series in limit_by_side.items()
        }

    if rated_u_by_side is None:
        shared = declared[0]
        for series in declared[1:]:
            shared = shared.fillna(series)
        return {
            side: shared if series is None else series.fillna(shared)
            for side, series in limit_by_side.items()
        }

    # Transformer: share the apparent-power-equivalent (Ilim * rated_u) across
    # sides, then convert it back to a current on each borrowing side.
    shared_s = None
    for side, series in limit_by_side.items():
        if series is None:
            continue
        s_equiv = series * rated_u_by_side[side]
        shared_s = s_equiv if shared_s is None else shared_s.fillna(s_equiv)
    return {
        side: (
            shared_s / rated_u_by_side[side]
            if series is None
            else series.fillna(shared_s / rated_u_by_side[side])
        )
        for side, series in limit_by_side.items()
    }


def _scalar_float(value):
    """A finite float, or None for None / NaN / non-numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _loading_percent(df, kind="permanent", element_type=None):
    """Loading in percent: each terminal's current over its effective rating.

    The number of terminals is read from the i1, i2, i3... columns present, so the
    rule is uniform across 2-terminal (line, two_windings_transformer) and
    3-terminal (three_windings_transformer) elements:
    ``max_k(|i_k| / effective_limit_k) * 100``, where the effective per-terminal
    limit comes from _resolve_effective_limits (one shared limit = a line's Imax;
    one per side = a 2-winding transformer, with a borrowed rating rescaled by the
    winding voltage ratio). A 3-winding transformer does NOT borrow across legs
    (star topology, independent ratings): an unrated leg stays NaN and is skipped
    by the max. Injections with no terminal currents use |p| / max_p.
    """
    terminals = _branch_terminals(df)
    if terminals:
        effective = _resolve_effective_limits(
            {
                side: _first_present_limit(df, _side_limit_columns(side, kind))
                for side in terminals
            },
            _transformer_rated_u(df, element_type, terminals),
            # A 3+ winding transformer (star topology) must not borrow a missing
            # leg's rating from a sibling; only 2-terminal branches may borrow.
            allow_borrow=len(terminals) <= 2,
        )
        if effective is None:
            return pd.Series(float("nan"), index=df.index)
        loadings = [df[f"i{side}"].abs() / effective[side] * 100 for side in terminals]
        # Worst terminal wins; a NaN terminal (missing current/limit) is skipped.
        return pd.concat(loadings, axis=1).max(axis=1)

    if "p" in df.columns and "max_p" in df.columns:
        limit = df["max_p"].astype(float).replace(0, float("nan"))
        return df["p"].abs() / limit * 100
    return pd.Series(float("nan"), index=df.index)


def compute_metric(df, metric, element_type):
    """Return one numeric value per row for the metric used in the filter."""
    if metric in df.columns:
        return df[metric]

    if metric == "loading_percent":
        return _loading_percent(df, element_type=element_type)

    if metric == "loading_percent_temporary":
        return _loading_percent(df, kind="temporary", element_type=element_type)

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

    if metric == "unrated_sides":
        mask = _unrated_loaded_mask(df, element_type)
        if mask.shape[1] == 0:
            return pd.Series(0, index=df.index)
        return mask.sum(axis=1)

    cols = ", ".join(sorted(df.columns.astype(str)))
    raise ValueError(
        f"Unknown metric '{metric}' for '{element_type}'. "
        f"Try loading_percent, p_abs, q_abs, unrated_sides, or a column name ({cols})"
    )


def dimensioning_terminal(row, kind="permanent", element_type=None):
    """Current and limit of the worst-loaded terminal of one branch row.

    Returns ``(i_a, limit_a)`` for the terminal maximising ``|i_k| / limit_k`` —
    the one that dimensions the row's loading_percent — or ``(None, None)`` when
    no terminal has both a current and a resolvable limit. ``row`` is a mapping
    (dict or Series) carrying i1/i2/... and the attached permanent_limit{k} /
    temporary_limit{k} columns (plus rated_u{k} for a transformer). The effective
    per-terminal limit is resolved by the SAME _resolve_effective_limits that
    backs loading_percent, so i_a / limit_a are consistent with it by construction
    — on an asymmetric transformer limit_a is the borrowed rating already rescaled
    to the dimensioning winding's voltage.
    """
    frame = pd.DataFrame([dict(row)])
    terminals = _branch_terminals(frame)
    if not terminals:
        return None, None
    effective = _resolve_effective_limits(
        {
            side: _first_present_limit(frame, _side_limit_columns(side, kind))
            for side in terminals
        },
        _transformer_rated_u(frame, element_type, terminals),
        # Mirror _loading_percent: a 3+ winding transformer never borrows a
        # missing leg's rating, so i_a/limit_a report a truly-rated winding.
        allow_borrow=len(terminals) <= 2,
    )
    if effective is None:
        return None, None

    best = None  # (loading, i_a, limit_a)
    for side in terminals:
        current = _scalar_float(row.get(f"i{side}"))
        limit = _scalar_float(effective[side].iloc[0])
        if current is None or limit is None:
            continue
        loading = abs(current) / limit
        if best is None or loading > best[0]:
            best = (loading, abs(current), limit)
    return (best[1], best[2]) if best else (None, None)


def _unrated_loaded_mask(df, element_type):
    """Per row and side, True where the terminal is energised (carries a finite
    current) yet has NO resolvable permanent rating, so its loading cannot be
    computed and the overload check silently skips it.

    Uses the SAME effective-limit resolution as loading_percent (permanent kind),
    so the no-borrow rule holds: a 2-terminal branch's side that borrows a
    sibling's rating is NOT flagged, but an unrated three-winding leg (no
    cross-leg borrow) IS — as is any branch terminal with a current but no rating
    anywhere. Returns a bool DataFrame indexed like ``df`` with one column per
    side (empty columns for a non-branch table, where there is nothing to check).
    """
    terminals = _branch_terminals(df)
    if not terminals:
        return pd.DataFrame(index=df.index)
    effective = _resolve_effective_limits(
        {
            side: _first_present_limit(df, _side_limit_columns(side, "permanent"))
            for side in terminals
        },
        _transformer_rated_u(df, element_type, terminals),
        allow_borrow=len(terminals) <= 2,
    )
    out = pd.DataFrame(index=df.index)
    for side in terminals:
        energised = df[f"i{side}"].abs().notna()
        limit_missing = (
            pd.Series(True, index=df.index)
            if effective is None
            else effective[side].isna()
        )
        out[side] = energised & limit_missing
    return out


def unrated_loaded_sides(row, element_type):
    """The 1-based side numbers of ``row`` that carry a current but have no
    resolvable permanent rating — the terminals the overload check cannot see.

    Empty when every energised terminal is rated. ``row`` is a mapping (dict or
    Series); mirrors dimensioning_terminal's one-row-frame reuse of the shared
    limit resolution so the coverage report and the loading math stay consistent.
    """
    frame = pd.DataFrame([dict(row)])
    mask = _unrated_loaded_mask(frame, element_type)
    if mask.shape[1] == 0:
        return []
    row_mask = mask.iloc[0]
    return [int(side) for side, flag in row_mask.items() if bool(flag)]


def _coerce_operand(value, op):
    """Type the right-hand side of a leaf according to its op.

    Membership ops need a list; ordered ops need a number; equality/inequality
    pass strings and bools through unchanged (no float coercion, so
    ``country == "FR"`` works).
    """
    if op in MEMBERSHIP:
        if not isinstance(value, (list, tuple)):
            raise FilterError(f"value must be a list for op '{op}', got {value!r}")
        return list(value)
    if op in _ORDERED_OPS:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise FilterError(f"value must be a number for op '{op}', got {value!r}")
    return value


def _coerce_to_number(value):
    """A numeric string -> float; anything else is returned unchanged."""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _match_operand_dtype(series, operand):
    """Coerce a stringified-number operand to match a numeric column.

    LLM/JSON callers often send numbers as strings; against a numeric column
    ``nominal_v == "225"`` would otherwise compare float-vs-str and silently never
    match (and ``!=`` match everything). We coerce the operand to a number only
    when the column is numeric, so a categorical column (``country``, ``regionCvg``)
    keeps comparing its values as strings. A genuinely non-numeric operand on a
    numeric column is left as-is (it simply matches nothing). Bool columns are
    excluded (bool is a numeric dtype in pandas) so ``connected == true`` stays a
    boolean comparison rather than coercing to ``1``.
    """
    if not pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return operand
    if isinstance(operand, list):
        return [_coerce_to_number(item) for item in operand]
    return _coerce_to_number(operand)


def _eval_leaf(df, cond, element_type):
    """Evaluate one ``{field, op, value|ref}`` leaf to a boolean mask Series."""
    if not isinstance(cond, dict):
        raise FilterError(
            f"each condition must be an object with 'field' and 'op', got {cond!r}"
        )
    field = cond.get("field")
    op = cond.get("op")
    if not isinstance(field, str) or not field.strip():
        raise FilterError(f"condition is missing a 'field': {cond!r}")
    if not isinstance(op, str) or not op.strip():
        raise FilterError(f"condition on '{field}' is missing an 'op'")

    op = op.strip()
    op_key = op.lower()
    compare = COMPARISONS.get(op) or COMPARISONS.get(op_key)
    membership = MEMBERSHIP.get(op_key)
    if compare is None and membership is None:
        valid = ", ".join(list(COMPARISONS) + list(MEMBERSHIP))
        raise FilterError(f"Unknown op '{op}' on '{field}'. Valid ops: {valid}")

    has_value = "value" in cond
    has_ref = "ref" in cond
    if has_value == has_ref:
        raise FilterError(
            f"condition on '{field}' needs exactly one of 'value' or 'ref'"
        )

    try:
        lhs = compute_metric(df, field, element_type)
    except ValueError as exc:
        raise FilterError(f"condition on '{field}': {exc}")

    if has_ref:
        if membership is not None:
            raise FilterError(f"op '{op}' needs a 'value' list, not a 'ref'")
        ref = cond["ref"]
        if not isinstance(ref, str) or not ref.strip():
            raise FilterError(f"condition on '{field}' has an invalid 'ref': {ref!r}")
        try:
            rhs = compute_metric(df, ref, element_type)
        except ValueError as exc:
            raise FilterError(f"condition ref '{ref}': {exc}")
        try:
            mask = compare(lhs, rhs)
        except TypeError:
            raise FilterError(
                f"cannot compare '{field}' with '{ref}' using '{op}' "
                "(non-numeric column?)"
            )
        return mask & lhs.notna() & rhs.notna()

    operand = _match_operand_dtype(lhs, _coerce_operand(cond["value"], op_key))
    if membership is not None:
        return membership(lhs, operand) & lhs.notna()
    try:
        mask = compare(lhs, operand)
    except TypeError:
        raise FilterError(
            f"cannot order-compare non-numeric column '{field}' with {operand!r}"
        )
    return mask & lhs.notna()


def _group_key(node):
    """Return 'all'/'any' if node is a group dict, else None."""
    if isinstance(node, dict):
        if "all" in node:
            return "all"
        if "any" in node:
            return "any"
    return None


def _validate_conditions_size(node, depth=0):
    """Enforce the depth/non-empty guards; return the total leaf count."""
    if depth > _MAX_CONDITION_DEPTH:
        raise FilterError(f"conditions nested too deep (max {_MAX_CONDITION_DEPTH})")
    if isinstance(node, list):
        if not node:
            raise FilterError("a conditions group is empty")
        return sum(_validate_conditions_size(c, depth + 1) for c in node)
    key = _group_key(node)
    if key is not None:
        children = node[key]
        if not isinstance(children, list) or not children:
            raise FilterError(f"'{key}' must be a non-empty list of conditions")
        return sum(_validate_conditions_size(c, depth + 1) for c in children)
    return 1  # a leaf


def _eval_node(df, node, element_type):
    """Evaluate a leaf, a bare list (AND), or an all/any group to a mask."""
    if isinstance(node, list):
        node = {"all": node}
    key = _group_key(node)
    if key is not None:
        reducer = operator.and_ if key == "all" else operator.or_
        masks = [_eval_node(df, child, element_type) for child in node[key]]
        result = masks[0]
        for mask in masks[1:]:
            result = reducer(result, mask)
        return result
    return _eval_leaf(df, node, element_type)


def _collect_fields(node, acc):
    """Gather every ``field``/``ref`` name referenced anywhere in the tree."""
    if isinstance(node, list):
        for child in node:
            _collect_fields(child, acc)
        return acc
    key = _group_key(node)
    if key is not None:
        for child in node[key]:
            _collect_fields(child, acc)
        return acc
    if isinstance(node, dict):
        for name in (node.get("field"), node.get("ref")):
            if isinstance(name, str):
                acc.add(name)
    return acc


def collect_condition_fields(conditions):
    """Public: the set of every field/ref name a conditions tree references.

    Accepts a JSON string (parsed) like apply_element_filter. Used to validate a
    predicate against a field allowlist before evaluating it (e.g. compare_variants
    rejecting state/derived fields). Enforces the §6 depth/leaf-count caps before
    recursing, so a caller that collects fields before apply_element_filter (which
    caps them too) is still protected from a RecursionError DoS.
    """
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except (ValueError, TypeError) as exc:
            raise FilterError(f"conditions is not valid JSON: {exc}")
    if conditions is None:
        return set()
    total_leaves = _validate_conditions_size(conditions)
    if total_leaves > _MAX_CONDITION_LEAVES:
        raise FilterError(
            f"too many conditions ({total_leaves}; max {_MAX_CONDITION_LEAVES})"
        )
    return _collect_fields(conditions, set())


def _first_leaf_field(node):
    """The ``field`` of the first leaf in document order, for the default sort."""
    if isinstance(node, list):
        for child in node:
            found = _first_leaf_field(child)
            if found:
                return found
        return None
    key = _group_key(node)
    if key is not None:
        for child in node[key]:
            found = _first_leaf_field(child)
            if found:
                return found
        return None
    if isinstance(node, dict):
        return node.get("field")
    return None


def _parse_order_by(order_by, conditions):
    """Resolve order_by to (column, ascending).

    ``"-field"``=desc, ``"field"``=asc, bare ``"asc"``/``"desc"`` sort the default
    (first-leaf) field, and None => first-leaf field descending (today's default).
    Returns (None, _) when there is no field to sort on.
    """
    default_field = _first_leaf_field(conditions)
    if order_by is None:
        return default_field, False
    text = str(order_by).strip()
    lowered = text.lower()
    if lowered in ("desc", "descending"):
        return default_field, False
    if lowered in ("asc", "ascending"):
        return default_field, True
    if text.startswith("-"):
        return text[1:].strip() or default_field, False
    if text.startswith("+"):
        return text[1:].strip() or default_field, True
    return text, True


def apply_element_filter(df, *, element_type, conditions, order_by=None):
    """Keep the rows matching ``conditions``, then sort the result.

    ``conditions`` is a leaf ``{field, op, value|ref}``, a flat list (implicit
    AND), or an ``{"all"|"any": [...]}`` group; a JSON string is accepted and
    parsed (LLM callers often stringify). Rows whose operands are NaN are dropped
    because they cannot be compared. Referenced derived metrics (loading_percent,
    p_abs, ...) are re-attached to the output so the client sees them.
    """
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except (ValueError, TypeError) as exc:
            raise FilterError(f"conditions is not valid JSON: {exc}")

    if conditions is None or (isinstance(conditions, (list, dict)) and not conditions):
        raise FilterError("conditions is required")

    total_leaves = _validate_conditions_size(conditions)
    if total_leaves > _MAX_CONDITION_LEAVES:
        raise FilterError(
            f"too many conditions ({total_leaves}; max {_MAX_CONDITION_LEAVES})"
        )

    mask = _eval_node(df, conditions, element_type)
    result = df[mask].copy()

    # Re-attach referenced derived metrics so the client sees them in the output
    # (generalises the single result[metric] = values[mask] of the old filter).
    for name in _collect_fields(conditions, set()):
        if name not in result.columns:
            result[name] = compute_metric(df, name, element_type)[mask]

    if result.empty:
        return result

    column, ascending = _parse_order_by(order_by, conditions)
    if column and column not in result.columns:
        try:
            result[column] = compute_metric(df, column, element_type)[mask]
        except ValueError as exc:
            raise FilterError(f"cannot order_by '{column}': {exc}")
    if column:
        result = result.sort_values(column, ascending=ascending)
    return result
