#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The overlay: what pypowsybl's creation metadata does not say.

Introspection gives the field list of every creation call, and the calls
themselves (``schema.CREATE_CALLS``, ``schema.BAY_CALLS``), but not which fields
are required, what they are measured in, which enumerated values are legal, or
what depends on what. This module supplies exactly that, one profile per element
type, and nothing else: no pipeline, no validation code, no pypowsybl calls --
the call a profile uses follows from how its element connects (see
:attr:`CreationProfile.executor`).

A profile is around twenty lines. Adding an element type the server does not
support yet is a new entry here, not a new tool.

Two kinds of entry deserve a word:

``managed``
    Attributes the caller must not pass: either the engine computes them (a
    feeder position), or the bay API fills them in (the voltage level and bus
    of an injection, which follow from the connection point), or they are
    artifacts of the metadata that mean nothing here (``feeder_type``, the
    duplicate ``TSO``).

``virtual``
    Convenience attributes that are *not* pypowsybl fields. A derivation turns
    them into real ones -- the ladder of a tap changer, the rows of a limits
    group -- because expressing those by hand is where a caller goes wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from functools import cached_property
from typing import Any

from pypowsybl.network import Network

from pypowsybl_mcp.plugins.grid_extension.creation import derivations as derive
from pypowsybl_mcp.plugins.grid_extension.creation import rules as rule
from pypowsybl_mcp.plugins.grid_extension.creation.executors import (
    DerivedCall,
    Executor,
    Unavailable,
    VoltageLevel,
)
from pypowsybl_mcp.plugins.grid_extension.creation.schema import (
    BAY_CALLS,
    CREATE_CALLS,
    ElementSchema,
    creatable_element_types,
    creation_schema,
)

# Attributes every bay-created injection takes from its connection point.
BAY_MANAGED = (
    "voltage_level_id",
    "bus_id",
    "connectable_bus_id",
    "node",
    "feeder_type",
)
DIRECTIONS = ("TOP", "BOTTOM")


@dataclass(frozen=True)
class VirtualTable:
    """The rows of a virtual attribute: the type of each column, and the
    columns every row must give."""

    columns: dict[str, str]
    required: tuple[str, ...] = ()


class Connection(str, Enum):
    """How an element attaches to the rest of the network."""

    #: nothing to connect (a substation) or connected by naming a container
    NONE = "none"
    #: one bus or busbar section, given as bus_or_busbar_section_id
    SINGLE = "single"
    #: two of them, given as bus_or_busbar_section_id_1 and _2
    DOUBLE = "double"
    #: a configured bus, for element types pypowsybl cannot create with a bay
    BUS = "bus"
    #: an element that already exists, which the creation attaches to
    TARGET = "target"


@dataclass(frozen=True)
class CreationProfile:
    """Everything about one element type that pypowsybl does not say itself."""

    element_type: str
    summary: str
    connection: Connection = Connection.NONE
    required: tuple[str, ...] = ()
    managed: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)
    rules: tuple[rule.Rule, ...] = ()
    derivations: tuple[derive.Derivation, ...] = ()
    virtual: dict[str, str] = field(default_factory=dict)
    #: type of each virtual attribute ("number", "integer", "boolean" or
    #: "string"), or the columns of a virtual table: what pypowsybl's metadata
    #: says of a real attribute, so a virtual one is checked the same way
    virtual_kinds: dict[str, str | VirtualTable] = field(default_factory=dict)
    #: attribute holding a table of rows, when the call takes one
    rows_attribute: str | None = None
    #: schema group of those rows: the second dataframe of the call by default
    #: (tap changer steps), the third for the sections of a non-linear shunt,
    #: 0 when the rows are the call's main dataframe (limits, curve points)
    rows_group: int = 1
    #: values a row of the table attribute gets for the columns it leaves out
    row_defaults: dict[str, Any] = field(default_factory=dict)
    #: element types this creation may attach to, for Connection.TARGET
    target_types: frozenset[str] = frozenset()
    #: batch ordering: containers, then equipment, then what attaches to it
    order: int = 50
    guidance: str = ""
    #: pypowsybl calls whose docstrings document these attributes, named like
    #: executor calls ("Network.create_loads") and resolved only when used
    docs: tuple[str, ...] = ()
    #: no overlay: built from the introspected schema alone (see UNPROFILED)
    schema_only: bool = False
    #: a call shape no derived executor covers (the voltage level); None for
    #: every other type, whose call is observed rather than written down
    special_executor: Executor | None = None

    @cached_property
    def executor(self) -> Executor:
        """How this type is created, derived from how it connects.

        An element connected to a bus or busbar section (``SINGLE``, ``DOUBLE``)
        goes through its bay helper, which builds the switchgear; everything
        else -- containers, attachments, schema-only types -- through its
        ``Network.create_*`` method. Both come from the registries observed on
        the installed pypowsybl, so no profile names a call.
        """
        if self.special_executor is not None:
            return self.special_executor
        if self.connection in (Connection.SINGLE, Connection.DOUBLE):
            registry, family = BAY_CALLS, "bay helper"
        else:
            registry, family = CREATE_CALLS, "Network.create_* method"
        call = registry.get(self.element_type)
        if call is None:
            return Unavailable(self.element_type, family)
        return DerivedCall(
            call,
            rows_attribute=self.rows_attribute,
            rows_group=self.rows_group,
            row_defaults=self.row_defaults,
            attaches=self.connection is Connection.TARGET,
            main_frame_only=self.schema_only,
        )

    @property
    def schema(self) -> ElementSchema:
        return self.executor.schema(self.element_type)

    @property
    def id_field(self) -> str:
        return self.schema.index_name


BRANCH_TYPES = frozenset(
    {
        "line",
        "two_windings_transformer",
        "three_windings_transformer",
        "tie_line",
        "boundary_line",
        "dangling_line",
    }
)
REACTIVE_TYPES = frozenset(
    {"generator", "battery", "vsc_converter_station", "hvdc_converter_station"}
)

PROFILES: dict[str, CreationProfile] = {}


#: what every attachment's guidance ends with: pypowsybl has no removal for it
ATTACHMENT_GUIDANCE = (
    " It cannot be removed on its own: create it again to replace it. "
    "remove_network_elements() on the id of the element it is attached to would "
    "remove that element itself."
)


def _register(profile: CreationProfile) -> None:
    if profile.connection is Connection.TARGET:
        profile = replace(profile, guidance=profile.guidance + ATTACHMENT_GUIDANCE)
    PROFILES[profile.element_type] = profile


# --------------------------------------------------------------------------
# Containers
# --------------------------------------------------------------------------

_register(
    CreationProfile(
        element_type="substation",
        summary="A geographical site holding voltage levels; no equipment of its own.",
        order=10,
        # 'TSO' duplicates 'tso' in the metadata; exposing both invites confusion.
        managed=("TSO",),
        enums={},
        guidance=(
            "The first step when extending a grid with a new site. A substation on "
            "its own changes no analysis: add a voltage level to it next. 'country' "
            "is an ISO 3166-1 alpha-2 code such as FR or BE."
        ),
        docs=("Network.create_substations",),
    )
)

_register(
    CreationProfile(
        element_type="voltage_level",
        summary=(
            "The busbar system of a site at one nominal voltage, created with the "
            "connection points equipment attaches to."
        ),
        special_executor=VoltageLevel(),
        order=20,
        required=("substation_id", "nominal_v"),
        defaults={
            "topology_kind": "BUS_BREAKER",
            "aligned_buses_or_busbar_count": 1,
            "section_count": 1,
        },
        units={
            "nominal_v": "kV",
            "low_voltage_limit": "kV",
            "high_voltage_limit": "kV",
        },
        enums={"topology_kind": ("BUS_BREAKER", "NODE_BREAKER")},
        rules=(
            rule.AtLeast("aligned_buses_or_busbar_count", 1),
            rule.AtLeast("section_count", 1),
            rule.Positive("nominal_v"),
        ),
        guidance=(
            "Equipment never attaches to a voltage level directly but to one of its "
            "connection points, so this creates them too and reports their ids -- "
            "pass those to the elements that follow. Match 'topology_kind' to the "
            "network it connects to: BUS_BREAKER for IEEE cases and most study "
            "files, NODE_BREAKER for detailed grid files. Without voltage limits, "
            "check_voltage_violations() can never report the new busbar."
        ),
        docs=(
            "Network.create_voltage_levels",
            "pp.network.create_voltage_level_topology",
        ),
    )
)

# --------------------------------------------------------------------------
# Injections
# --------------------------------------------------------------------------

_register(
    CreationProfile(
        element_type="load",
        summary="A consumption point: a datacenter, a factory, an extra demand.",
        connection=Connection.SINGLE,
        order=40,
        required=("p0",),
        managed=BAY_MANAGED,
        defaults={"q0": 0.0},
        units={"p0": "MW", "q0": "MVAr"},
        enums={
            "type": ("UNDEFINED", "AUXILIARY", "FICTITIOUS"),
            "direction": DIRECTIONS,
        },
        derivations=(
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "p0 is positive for consumption: a 300 MW datacenter is p0=300. A q0 of "
            "about 0.2*p0 corresponds to a 0.98 power factor."
        ),
        docs=("pp.network.create_load_bay", "Network.create_loads"),
    )
)

_register(
    CreationProfile(
        element_type="generator",
        summary="A production unit: a plant, a wind or solar farm.",
        connection=Connection.SINGLE,
        order=40,
        required=("target_p", "max_p"),
        managed=BAY_MANAGED,
        defaults={"min_p": 0.0, "voltage_regulator_on": False},
        units={
            "target_p": "MW",
            "max_p": "MW",
            "min_p": "MW",
            "target_q": "MVAr",
            "target_v": "kV",
            "rated_s": "MVA",
        },
        enums={
            "energy_source": (
                "HYDRO",
                "NUCLEAR",
                "WIND",
                "THERMAL",
                "SOLAR",
                "OTHER",
            ),
            "direction": DIRECTIONS,
        },
        rules=(
            rule.RequiredIf(
                "target_v",
                "voltage_regulator_on",
                "a regulating unit needs a voltage setpoint",
            ),
            rule.DefaultUnless("target_q", 0.0, "voltage_regulator_on"),
            rule.Ordered(("min_p", "target_p", "max_p")),
        ),
        derivations=(
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "target_v is in kV, not per-unit, and should be close to the nominal "
            "voltage of the hosting voltage level. A voltage-regulating unit is "
            "usually what turns a diverging connection study into a converging one; "
            "give it reactive limits (element_type='minmax_reactive_limits') or the "
            "load flow will hold the setpoint with unlimited reactive power."
        ),
        docs=("pp.network.create_generator_bay", "Network.create_generators"),
    )
)

_register(
    CreationProfile(
        element_type="battery",
        summary="A storage unit: produces when target_p is positive, absorbs when negative.",
        connection=Connection.SINGLE,
        order=40,
        required=("target_p", "max_p"),
        managed=BAY_MANAGED,
        defaults={"target_q": 0.0},
        units={"target_p": "MW", "max_p": "MW", "min_p": "MW", "target_q": "MVAr"},
        enums={"direction": DIRECTIONS},
        rules=(rule.Ordered(("min_p", "target_p", "max_p")),),
        derivations=(
            derive.Negated("min_p", "max_p"),
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "min_p defaults to -max_p, a symmetric charge/discharge range. Energy "
            "content and state of charge are not modelled: a load flow sees a fixed "
            "injection, not a storage schedule."
        ),
        docs=("pp.network.create_battery_bay", "Network.create_batteries"),
    )
)

_register(
    CreationProfile(
        element_type="shunt_compensator",
        summary="A capacitor bank (positive susceptance) or a reactor (negative).",
        connection=Connection.SINGLE,
        order=40,
        rows_attribute="sections",
        rows_group=2,
        row_defaults={"g": 0.0},
        # 'model_type' follows from how the sections are given; 'g' and 'b' are
        # the columns of the non-linear sections, given under 'sections'.
        managed=(*BAY_MANAGED, "model_type", "g", "b"),
        defaults={"section_count": 1},
        units={
            "b_per_section": "S",
            "g_per_section": "S",
            "b": "S",
            "g": "S",
            "target_v": "kV",
            "target_deadband": "kV",
        },
        enums={"direction": DIRECTIONS},
        derivations=(
            derive.SectionModel(),
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        rules=(
            rule.Exclusive(
                ("b_per_section", "g_per_section", "max_section_count"),
                ("sections",),
                "identical sections, or one row per section",
            ),
            rule.RequiredWhenEquals(
                "b_per_section",
                "model_type",
                "LINEAR",
                "the susceptance of each identical section, unless 'sections' are given",
            ),
            rule.DefaultUnless("max_section_count", 1, "sections"),
            rule.DefaultUnless("g_per_section", 0.0, "sections"),
            rule.ColumnRequired("sections", "b"),
            rule.AtLeast("max_section_count", 1),
            rule.AtMost("section_count", "max_section_count"),
            rule.AtMostRows("section_count", "sections"),
        ),
        guidance=(
            "Two models. Identical sections (linear): give b_per_section, and "
            "max_section_count for a bank of several steps. Sections that differ "
            "(non-linear): give 'sections' as a list of {b, g}, one per section in "
            "switching order, the susceptance being cumulative up to that section. "
            "section_count is how many are connected. For a bank rated Q MVAr at "
            "U kV, b = Q / U^2 -- 60 MVAr at 63 kV is 0.0151 S. The reactive power "
            "injected follows the voltage: Q = b*U^2."
        ),
        docs=("pp.network.create_shunt_compensator_bay",),
    )
)

_register(
    CreationProfile(
        element_type="static_var_compensator",
        summary="Continuous reactive control between b_min and b_max.",
        connection=Connection.SINGLE,
        order=40,
        required=("b_min", "b_max"),
        managed=BAY_MANAGED,
        defaults={"regulation_mode": "VOLTAGE", "regulating": True},
        units={"b_min": "S", "b_max": "S", "target_v": "kV", "target_q": "MVAr"},
        # pypowsybl 1.15 has no OFF mode: a device that should not act is
        # created with regulating=False.
        enums={
            "regulation_mode": ("VOLTAGE", "REACTIVE_POWER"),
            "direction": DIRECTIONS,
        },
        rules=(
            rule.Ordered(("b_min", "b_max")),
            rule.RequiredWhenEquals("target_v", "regulation_mode", "VOLTAGE"),
            rule.RequiredWhenEquals("target_q", "regulation_mode", "REACTIVE_POWER"),
        ),
        derivations=(
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "Where a shunt compensator switches fixed sections, an SVC varies its "
            "susceptance continuously, so it can hold a voltage setpoint. For "
            "+/-50 MVAr at 63 kV, b_min=-0.0126 and b_max=0.0126."
        ),
        docs=("pp.network.create_static_var_compensator_bay",),
    )
)

_register(
    CreationProfile(
        element_type="vsc_converter_station",
        summary="The AC end of a voltage-source HVDC link.",
        connection=Connection.SINGLE,
        order=40,
        required=("loss_factor",),
        managed=BAY_MANAGED,
        defaults={"voltage_regulator_on": False},
        units={"target_v": "kV", "target_q": "MVAr", "loss_factor": "percent"},
        enums={"direction": DIRECTIONS},
        rules=(
            rule.RequiredIf("target_v", "voltage_regulator_on"),
            rule.DefaultUnless("target_q", 0.0, "voltage_regulator_on"),
        ),
        derivations=(
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "Create the two stations first, then the hvdc_line that joins them. A VSC "
            "station can regulate voltage on its AC side, like a generator."
        ),
        docs=("pp.network.create_vsc_converter_station_bay",),
    )
)

_register(
    CreationProfile(
        element_type="lcc_converter_station",
        summary="The AC end of a line-commutated HVDC link.",
        connection=Connection.SINGLE,
        order=40,
        required=("power_factor", "loss_factor"),
        managed=BAY_MANAGED,
        units={"loss_factor": "percent"},
        enums={"direction": DIRECTIONS},
        derivations=(
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "Create the two stations first, then the hvdc_line that joins them. An "
            "LCC station absorbs reactive power set by its power factor."
        ),
        docs=("pp.network.create_lcc_converter_station_bay",),
    )
)

_register(
    CreationProfile(
        element_type="ground",
        summary="An earthing connection on a bus.",
        connection=Connection.BUS,
        order=40,
        managed=("connectable_bus_id", "node"),
        rules=(rule.BusBreakerOnly(),),
        derivations=(
            derive.FromConnection("voltage_level_id", "voltage_level_id"),
            derive.FromConnection("bus_id", "id"),
        ),
        guidance=(
            "A ground carries no power in a load flow; it is there for completeness "
            "of the model. pypowsybl has no bay creation for grounds, so only "
            "bus/breaker voltage levels are supported."
        ),
        docs=("Network.create_grounds",),
    )
)

GENERATION_PART = (
    "min_p",
    "max_p",
    "target_p",
    "target_q",
    "target_v",
    "voltage_regulator_on",
)

_register(
    CreationProfile(
        element_type="boundary_line",
        summary=(
            "A line to a point outside the modeled network (a border X-node), "
            "with the exchange at its far end."
        ),
        connection=Connection.SINGLE,
        order=40,
        required=("r", "x"),
        managed=BAY_MANAGED,
        defaults={"p0": 0.0, "q0": 0.0, "g": 0.0, "b": 0.0},
        units={
            "r": "Ohm",
            "x": "Ohm",
            "g": "S",
            "b": "S",
            "p0": "MW",
            "q0": "MVAr",
            "min_p": "MW",
            "max_p": "MW",
            "target_p": "MW",
            "target_q": "MVAr",
            "target_v": "kV",
        },
        enums={"direction": DIRECTIONS},
        rules=(
            rule.NonZero("x", "a zero-impedance line makes the load flow singular"),
            rule.OptionalPart(
                "generation part",
                GENERATION_PART,
                required=("min_p", "max_p", "target_p"),
                defaults=(("voltage_regulator_on", False), ("target_q", 0.0)),
            ),
            rule.RequiredIf(
                "target_v",
                "voltage_regulator_on",
                "a regulating generation part needs a voltage setpoint",
            ),
            rule.Ordered(("min_p", "target_p", "max_p")),
        ),
        derivations=(
            derive.PositionOrder("position_order"),
            derive.DirectionOnlyInNodeBreaker("direction"),
        ),
        guidance=(
            "The equivalent of what lies beyond the border: p0/q0 is the exchange "
            "consumed at the far end (positive means power leaving the network). "
            "Give a generation part (min_p, max_p, target_p, and target_v with "
            "voltage_regulator_on=True to regulate) when the outside can produce. "
            "Set pairing_key to later join it with the boundary line of the "
            "neighboring network into a tie_line."
        ),
        docs=("pp.network.create_boundary_line_bay", "Network.create_boundary_lines"),
    )
)

# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------

_register(
    CreationProfile(
        element_type="line",
        summary="An AC line between two connection points, with a bay at each end.",
        connection=Connection.DOUBLE,
        order=45,
        required=("r", "x"),
        defaults={"g1": 0.0, "b1": 0.0, "g2": 0.0, "b2": 0.0},
        units={"r": "Ohm", "x": "Ohm", "g1": "S", "b1": "S", "g2": "S", "b2": "S"},
        enums={"direction_1": DIRECTIONS, "direction_2": DIRECTIONS},
        rules=(
            rule.DistinctEnds(),
            rule.NonZero("x", "a zero-impedance line makes the load flow singular"),
            rule.WarnIfDifferent(
                "nominal_v",
                "the two ends have different nominal voltages ({side1} kV and "
                "{side2} kV); a transformer, not a line, is normally used there",
            ),
        ),
        derivations=(
            derive.PositionOrder("position_order_1", side=0),
            derive.PositionOrder("position_order_2", side=1),
            derive.DirectionOnlyInNodeBreaker("direction_1", side=0),
            derive.DirectionOnlyInNodeBreaker("direction_2", side=1),
        ),
        guidance=(
            "r and x are totals in ohms, not per km. Copy them from a comparable "
            "existing line rather than inventing values; orders of magnitude at "
            "400 kV are r ~ 0.03 Ohm/km and x ~ 0.3 Ohm/km. A new line has no "
            "current limits until operational_limits are created for it, and "
            "without limits it can never be reported as overloaded."
        ),
        docs=("pp.network.create_line_bays", "Network.create_lines"),
    )
)

_register(
    CreationProfile(
        element_type="two_windings_transformer",
        summary="A transformer between two voltage levels of one substation.",
        connection=Connection.DOUBLE,
        order=45,
        required=("r", "x"),
        managed=("voltage_level1_id", "voltage_level2_id"),
        defaults={"g": 0.0, "b": 0.0},
        units={
            "r": "Ohm",
            "x": "Ohm",
            "g": "S",
            "b": "S",
            "rated_u1": "kV",
            "rated_u2": "kV",
            "rated_s": "MVA",
        },
        enums={"direction_1": DIRECTIONS, "direction_2": DIRECTIONS},
        rules=(
            rule.DistinctEnds(),
            rule.SameSubstation(),
            rule.NonZero(
                "x", "a zero-impedance transformer makes the load flow singular"
            ),
        ),
        derivations=(
            derive.NominalVoltage("rated_u1", side=0),
            derive.NominalVoltage("rated_u2", side=1),
            derive.PositionOrder("position_order_1", side=0),
            derive.PositionOrder("position_order_2", side=1),
            derive.DirectionOnlyInNodeBreaker("direction_1", side=0),
            derive.DirectionOnlyInNodeBreaker("direction_2", side=1),
        ),
        guidance=(
            "rated_u1 and rated_u2 default to the nominal voltages of the two ends, "
            "which is what sets the ratio. x is roughly "
            "(short-circuit voltage %) / 100 * rated_u1^2 / rated_s. The transformer "
            "has no tap changer until a ratio_tap_changer or phase_tap_changer is "
            "created on it."
        ),
        docs=(
            "pp.network.create_2_windings_transformer_bays",
            "Network.create_2_windings_transformers",
        ),
    )
)

_register(
    CreationProfile(
        element_type="hvdc_line",
        summary="A DC link between two converter stations that already exist.",
        order=47,
        required=(
            "converter_station1_id",
            "converter_station2_id",
            "r",
            "nominal_v",
            "max_p",
            "target_p",
        ),
        defaults={"converters_mode": "SIDE_1_RECTIFIER_SIDE_2_INVERTER"},
        units={"r": "Ohm", "nominal_v": "kV", "max_p": "MW", "target_p": "MW"},
        enums={
            "converters_mode": (
                "SIDE_1_RECTIFIER_SIDE_2_INVERTER",
                "SIDE_1_INVERTER_SIDE_2_RECTIFIER",
            )
        },
        rules=(rule.Positive("nominal_v"),),
        guidance=(
            "Create the two converter stations first (vsc_converter_station or "
            "lcc_converter_station), then this link naming them. nominal_v is the DC "
            "voltage, target_p the power ordered through the link."
        ),
        docs=("Network.create_hvdc_lines",),
    )
)

_register(
    CreationProfile(
        element_type="tie_line",
        summary="A line joining two boundary lines, typically across a border.",
        # after the boundary lines it joins, which are created at order 40
        order=47,
        required=("boundary_line1_id", "boundary_line2_id"),
        guidance=(
            "Pairs two existing boundary lines -- usually one from each of two "
            "merged networks, sharing a pairing_key -- into one line. The two "
            "boundary lines keep existing as its halves."
        ),
        docs=("Network.create_tie_lines",),
    )
)

# --------------------------------------------------------------------------
# Attached to an element that already exists
# --------------------------------------------------------------------------

_register(
    CreationProfile(
        element_type="operational_limits",
        summary="The current (or power) ratings of a branch, without which it can never be overloaded.",
        connection=Connection.TARGET,
        target_types=BRANCH_TYPES,
        order=60,
        rows_attribute="limits",
        # the rows are the main dataframe of the call itself
        rows_group=0,
        managed=("name", "fictitious"),
        virtual={
            "permanent_limit": "the permanent rating (PATL), in A for CURRENT limits",
            "temporary_limits": (
                "temporary ratings, a list of {value, acceptable_duration} with the "
                "duration in seconds"
            ),
            "limit_type": "CURRENT (default), ACTIVE_POWER or APPARENT_POWER",
        },
        virtual_kinds={
            "permanent_limit": "number",
            "temporary_limits": VirtualTable(
                columns={
                    "value": "number",
                    "acceptable_duration": "integer",
                    "name": "string",
                },
                required=("value", "acceptable_duration"),
            ),
            "limit_type": "string",
        },
        defaults={"side": "BOTH", "limit_type": "CURRENT"},
        units={"permanent_limit": "A", "value": "A", "acceptable_duration": "seconds"},
        enums={
            "side": ("ONE", "TWO", "BOTH"),
            "limit_type": ("CURRENT", "ACTIVE_POWER", "APPARENT_POWER"),
            "type": ("CURRENT", "ACTIVE_POWER", "APPARENT_POWER"),
        },
        rules=(
            rule.Positive("permanent_limit"),
            rule.RowsRequired("limits", 1, "a permanent rating is required"),
        ),
        derivations=(derive.LimitRows(),),
        guidance=(
            "A branch created by this server starts with no limits, and an element "
            "without limits is invisible to get_overloaded_elements(), to "
            "loading_percent and to the current-limit violations of a security "
            "analysis -- so rating a new branch is what makes a connection study "
            "conclusive. The overload tools read side ONE, so keep side='BOTH'. "
            "Calling again for the same element replaces its limits rather than "
            "adding to them. Leave group_name unset: an explicitly named group is "
            "not the active one and the analyses ignore it."
        ),
        docs=("Network.create_operational_limits",),
    )
)

_register(
    CreationProfile(
        element_type="minmax_reactive_limits",
        summary="A constant reactive capability range for a generator, battery or converter.",
        connection=Connection.TARGET,
        target_types=REACTIVE_TYPES,
        order=60,
        required=("min_q", "max_q"),
        units={"min_q": "MVAr", "max_q": "MVAr"},
        rules=(rule.Ordered(("min_q", "max_q")),),
        guidance=(
            "Without reactive limits a voltage-regulating unit is treated as having "
            "unlimited reactive power, which makes a connection study optimistic. Use "
            "reactive_capability_curve_point instead when the capability depends on "
            "the active power."
        ),
        docs=("Network.create_minmax_reactive_limits",),
    )
)

_register(
    CreationProfile(
        element_type="reactive_capability_curve_point",
        summary="A reactive capability curve Q(P), as a list of points.",
        connection=Connection.TARGET,
        target_types=REACTIVE_TYPES,
        order=60,
        rows_attribute="points",
        # the rows are the main dataframe of the call itself
        rows_group=0,
        units={"p": "MW", "min_q": "MVAr", "max_q": "MVAr"},
        rules=(
            rule.RowsRequired(
                "points", 2, "a curve is made of segments between its points"
            ),
        ),
        guidance=(
            "Pass 'points' as a list of {p, min_q, max_q}, at least two of them, in "
            "increasing active power. This is what a real machine has: less reactive "
            "capability at full output."
        ),
        docs=("Network.create_curve_reactive_limits",),
    )
)

_register(
    CreationProfile(
        element_type="ratio_tap_changer",
        summary="Voltage regulation on a two-windings transformer (on-load tap changer).",
        connection=Connection.TARGET,
        target_types=frozenset({"two_windings_transformer"}),
        order=60,
        rows_attribute="steps",
        row_defaults={"rho": 1.0, "r": 0.0, "x": 0.0, "g": 0.0, "b": 0.0},
        managed=("side",),
        virtual={
            "step_count": "number of tap positions when the steps are generated",
            "range_percent": "half-range of the ratio in percent, e.g. 10 for +/-10%",
        },
        virtual_kinds={"step_count": "integer", "range_percent": "number"},
        defaults={
            "step_count": 17,
            "range_percent": 10.0,
            "low_tap": 0,
            "oltc": True,
            "regulating": False,
            "target_deadband": 1.0,
            "regulated_side": "TWO",
        },
        units={"target_v": "kV", "target_deadband": "kV"},
        enums={"regulated_side": ("ONE", "TWO")},
        rules=(
            rule.RequiredIf(
                "target_v", "regulating", "a regulating tap changer needs a setpoint"
            ),
            rule.RowsRequired("steps", 2, "a tap changer needs at least two positions"),
        ),
        derivations=(derive.TapSteps(), derive.MiddleTap()),
        guidance=(
            "A transformer created here has a fixed ratio until this is added, and "
            "set_tap_position() has nothing to move. The steps are generated as a "
            "ladder around the nominal ratio (+/-10% over 17 positions by default), "
            "starting on the neutral middle step; pass 'steps' as a list of {rho} to "
            "describe them yourself. target_v is in kV. Whether the regulation is "
            "simulated also depends on the load-flow parameters."
        ),
        docs=("Network.create_ratio_tap_changers",),
    )
)

_register(
    CreationProfile(
        element_type="phase_tap_changer",
        summary="Active-power control on a two-windings transformer (phase shifter).",
        connection=Connection.TARGET,
        target_types=frozenset({"two_windings_transformer"}),
        order=60,
        rows_attribute="steps",
        row_defaults={
            "rho": 1.0,
            "alpha": 0.0,
            "r": 0.0,
            "x": 0.0,
            "g": 0.0,
            "b": 0.0,
        },
        managed=("side",),
        virtual={
            "step_count": "number of tap positions when the steps are generated",
            "max_angle_degrees": "largest phase shift, reached at the extreme positions",
        },
        virtual_kinds={"step_count": "integer", "max_angle_degrees": "number"},
        defaults={
            "step_count": 17,
            "max_angle_degrees": 10.0,
            "low_tap": 0,
            "regulating": False,
            "regulation_mode": "CURRENT_LIMITER",
            "target_deadband": 0.0,
            "regulated_side": "ONE",
        },
        # pypowsybl 1.15 has no FIXED_TAP mode: a phase shifter that should stay
        # put is created with regulating=False.
        enums={
            "regulation_mode": ("CURRENT_LIMITER", "ACTIVE_POWER_CONTROL"),
            "regulated_side": ("ONE", "TWO"),
        },
        rules=(
            rule.RequiredIf(
                "target_value",
                "regulating",
                "in A for CURRENT_LIMITER, in MW for ACTIVE_POWER_CONTROL",
            ),
            rule.RowsRequired("steps", 2, "a tap changer needs at least two positions"),
        ),
        derivations=(
            derive.TapSteps(
                span_field="max_angle_degrees",
                stepped="alpha",
                around=0.0,
                scale=1.0,
            ),
            derive.MiddleTap(),
        ),
        guidance=(
            "A phase shifter controls the active power through the transformer, which "
            "is how a flow is pushed away from an overloaded corridor. Steps are "
            "generated from -max_angle_degrees to +max_angle_degrees. Creating it "
            "with regulating=False is the safe default: an active phase shifter "
            "changes flows everywhere."
        ),
        docs=("Network.create_phase_tap_changers",),
    )
)


# --------------------------------------------------------------------------
# Without a profile: created from the introspected schema alone
# --------------------------------------------------------------------------

#: batch position of a schema-only type: right after the voltage levels, as they
#: are mostly topology (buses, busbar sections, switches, DC nodes) that later
#: items connect to. Among themselves they keep the order they were given in.
SCHEMA_ONLY_ORDER = 30

SCHEMA_ONLY_GUIDANCE = (
    "This element type has no profile, so only its attribute names and their "
    "types are checked here: which attributes are required, the accepted values "
    "and the topology rules are left to pypowsybl, whose refusal is returned as "
    "is. Nothing is derived and no switching equipment is built: give the "
    "voltage level and the bus (bus/breaker) or node (node/breaker) exactly as "
    "pypowsybl takes them, and check the ids with get_network_element_data(). "
    "get_online_resource(class_object='network') has the pypowsybl documentation "
    "of the call."
)


def schema_only_profile(element_type: str) -> CreationProfile:
    """The profile of a type that has none, derived from its schema alone."""
    call = CREATE_CALLS[element_type]
    doc = (getattr(Network, call.function).__doc__ or "").strip().splitlines()
    guidance = SCHEMA_ONLY_GUIDANCE
    if creation_schema(element_type).group_count > 1:
        guidance += (
            " Only the attributes of the main table of the call are accepted; its "
            "additional tables are not supported here."
        )
    return CreationProfile(
        element_type=element_type,
        summary=(doc[0].strip() if doc else f"Created with {call.name}()."),
        order=SCHEMA_ONLY_ORDER,
        guidance=guidance,
        docs=(call.name,),
        schema_only=True,
    )


#: element types pypowsybl can create that have no profile, each with a profile
#: derived from its schema; follows the installed version by construction
UNPROFILED: dict[str, CreationProfile] = {
    name: schema_only_profile(name)
    for name in creatable_element_types()
    if name not in PROFILES and name in CREATE_CALLS
}


def profile(element_type: str) -> CreationProfile | None:
    return PROFILES.get(element_type)


def supported_element_types() -> tuple[str, ...]:
    return tuple(sorted(PROFILES))
