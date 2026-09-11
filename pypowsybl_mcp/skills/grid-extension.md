---
name: grid-extension
description: How to add equipment to a network with create_network_element / create_network_elements (substations, voltage levels, loads, generators, batteries, shunts, SVCs, lines, transformers, HVDC links, operational limits, reactive limits, tap changers), how to remove it again with remove_network_elements, and how to run a connection study - for instance connecting a new datacenter to the grid. Use when asked to connect, add, attach or build a new load, new generation, storage, compensation, a new site, a line or a transformer, to rate a branch or add a tap changer, to remove, delete or retire an element, or when a creation call answered that a bus or busbar section could not be used.
---

# Skill: extending a grid and running a connection study

Three tools cover every element type:

| Tool | What it does |
| ---- | ------------ |
| `describe_element_creation(element_type)` | What a type needs: required attributes, units, accepted values, rules, guidance. With no argument, the list of creatable types |
| `create_network_element(element_type, attributes)` | Creates one element |
| `create_network_elements(items)` | Creates several in one call, ordered so containers come before what they contain |
| `remove_network_elements(element_ids)` | The inverse: removes elements of any type by id |

The attributes of each type are read from the installed pypowsybl, so
`describe_element_creation` is the truth of the running version. **Errors carry
that description**: a rejected call comes back with the required attributes,
their units and the accepted values, so read the error rather than guessing
twice.

## Never build or delete equipment on your own initiative

**These tools change the structure of the grid model, so they are only ever
called when the user explicitly asked for that change.** Adding a load, a
generator, a line, a transformer or a substation -- or removing any of them --
must correspond to a request the user actually made, in their own words:
"connect this datacenter", "add a second circuit", "what if we build a plant
here", "remove that line from the model".

They are **long-term study tools**: connection studies, network development and
planning scenarios, "what would we have to build" questions. They are **not** a
way to make a case behave better:

* Never create generation, a line or a transformer to fix a constraint, an
  overload, a voltage violation or a non-convergent load flow. A constraint on
  a real-time or operational case is a **result**, and it is reported as such --
  the remedial actions available there are the operational ones
  (`modify_network` to redispatch, `set_line_status` / `set_switch_status` for
  topology, `set_tap_position` for taps), never building new equipment.
* Never delete an element to make a violation, an overload or a divergence go
  away, and never "clean up" a network by removing what looks unused.
* Never invent equipment to complete a network that looks incomplete: report
  what is missing instead.

If extending or reducing the grid looks like the answer to the question and the
user did not ask for it, say so and ask -- describing what you would build or
remove -- rather than doing it.

## The one rule about connection points

Equipment is never attached to a voltage level, it is attached to a **bus or
busbar section**, named in `bus_or_busbar_section_id` (or `..._id_1` / `..._id_2`
for a line or a transformer). Only one family of ids works:

* **`NODE_BREAKER` voltage level** -> a **busbar section** id
  (`get_network_element_data(element_type='busbar_section')`).
* **`BUS_BREAKER` voltage level** -> a **configured bus** id, i.e. a bus of the
  bus/breaker view
  (`get_network_element_data(element_type='bus_from_bus_breaker_view')`).

The bus ids that appear in load-flow results, in `check_voltage_violations` or
in `get_network_element_data(element_type='bus')` belong to the **bus view**.
They are computed from the topology (`VL1_0`, `S1VL2_0`, ...) and **cannot host
an element**. On an IEEE test case, `VL1_0` is a bus-view bus while `B1` is the
configured bus to connect to. When a creation is rejected for this reason the
message lists the ids that would have worked in that voltage level -- use them.

You never pass a `node` number, a `voltage_level_id` or a feeder position for an
injection: the bay that attaches it is built for the topology of the hosting
voltage level, and the engine fills those in. Passing them is an error.

Attachments -- `operational_limits`, `minmax_reactive_limits`,
`reactive_capability_curve_point`, `ratio_tap_changer`, `phase_tap_changer` --
are addressed by the id of the element they attach to, as `element_id`.

## Workflow: connecting a new datacenter

A 300 MW datacenter connected to a 135 kV grid, in one call:

```json
create_network_elements(items=[
  {"element_type": "substation", "id": "SUB_DC", "country": "FR"},
  {"element_type": "voltage_level", "id": "VL_DC", "substation_id": "SUB_DC",
   "nominal_v": 135.0, "low_voltage_limit": 128.0, "high_voltage_limit": 145.0},
  {"element_type": "load", "id": "DATACENTER",
   "bus_or_busbar_section_id": "VL_DC_1_1", "p0": 300.0, "q0": 60.0},
  {"element_type": "line", "id": "LINE_B4_DC", "bus_or_busbar_section_id_1": "B4",
   "bus_or_busbar_section_id_2": "VL_DC_1_1", "r": 0.5, "x": 5.0},
  {"element_type": "operational_limits", "element_id": "LINE_B4_DC",
   "permanent_limit": 800.0}
])
```

Around it:

1. `get_network_info()` and `run_loadflow()` **before** the extension, so the
   impact can be attributed to it.
2. The call above. Items may be listed in any order. A new voltage level
   generates its connection points and the report gives their ids
   (`VL_DC_1_1` here) -- the pattern is `<voltage level>_<busbar>_<section>`, and
   later items in the same call can reference them.
3. `run_loadflow()` -- does it still converge?
4. `get_overloaded_elements()` and `check_voltage_violations()` -- acceptable in N?
5. `run_security_analysis()` -- still acceptable after an N-1?
   `set_line_status('LINE_B4_DC', False)` checks the site is not left alone on a
   single circuit.
6. `visualize_network()` or `plot_substation_single_line_diagram('SUB_DC')` to
   show it, `export_network()` to keep it.

Choosing values: `q0` around `0.2 * p0` is a 0.98 power factor; copy `r`/`x`
from a comparable existing line (`get_network_element_data(element_type='line')`)
rather than inventing them -- at 400 kV, r ~ 0.03 Ohm/km and x ~ 0.3 Ohm/km.

**Do not skip the limits.** A branch with no operational limits can never be
reported as overloaded: it is invisible to `get_overloaded_elements()`, to
`loading_percent` and to the current-limit violations of a security analysis, so
without them steps 4 and 5 conclude nothing about the new line.

## Workflow: connecting a site at another voltage

A transformer links two voltages, and powsybl only accepts one **inside a single
substation**, so the site has two voltage levels in the same substation:

```json
create_network_elements(items=[
  {"element_type": "substation", "id": "SUB_DC", "country": "FR"},
  {"element_type": "voltage_level", "id": "VL_135", "substation_id": "SUB_DC",
   "nominal_v": 135.0},
  {"element_type": "voltage_level", "id": "VL_63", "substation_id": "SUB_DC",
   "nominal_v": 63.0},
  {"element_type": "two_windings_transformer", "id": "TR_DC",
   "bus_or_busbar_section_id_1": "VL_135_1_1",
   "bus_or_busbar_section_id_2": "VL_63_1_1", "r": 0.2, "x": 10.0,
   "rated_s": 400.0},
  {"element_type": "load", "id": "DATACENTER",
   "bus_or_busbar_section_id": "VL_63_1_1", "p0": 100.0, "q0": 20.0},
  {"element_type": "line", "id": "LINE_B4_DC", "bus_or_busbar_section_id_1": "B4",
   "bus_or_busbar_section_id_2": "VL_135_1_1", "r": 0.5, "x": 5.0},
  {"element_type": "operational_limits", "element_id": "TR_DC",
   "permanent_limit": 1800.0},
  {"element_type": "ratio_tap_changer", "element_id": "TR_DC",
   "regulating": true, "target_v": 63.0}
])
```

`rated_u1` / `rated_u2` default to the nominal voltages of the two ends, which is
what sets the ratio; `x` is roughly `usc% / 100 * rated_u1^2 / rated_s`. A
created transformer has **no tap changer** until a `ratio_tap_changer` (voltage)
or `phase_tap_changer` (active power) is added, and until then
`set_tap_position()` has nothing to move. Their steps are generated as a ladder
(+/-10% over 17 positions, or +/-10 degrees), starting on the neutral middle
step; pass `steps` explicitly to describe them yourself.

## Other equipment worth knowing

* `generator` -- `target_v` is in **kV**, not per-unit. A regulating unit with
  no reactive limits holds its setpoint with unlimited MVAr, which flatters a
  study: add `minmax_reactive_limits` or a
  `reactive_capability_curve_point` list.
* `battery` -- `min_p` defaults to `-max_p`, a symmetric charge/discharge range.
* `shunt_compensator` -- linear model only; for a bank rated Q MVAr at U kV,
  `b_per_section = Q / U^2`. Positive is a capacitor, negative a reactor.
* `static_var_compensator` -- continuous reactive control; there is no `OFF`
  mode in this pypowsybl version, use `regulating: false`.
* `hvdc_line` -- create the two `vsc_converter_station` (or
  `lcc_converter_station`) first, then the link naming them; one
  `create_network_elements` call does all three in the right order.

## Undoing or retiring equipment

`remove_network_elements(element_ids=[...])` deletes elements of any type, and
picks the right removal from the type it finds in the network:

* A **feeder** (load, generator, line, transformer, shunt, SVC, converter
  station) goes **with its bay**, so no orphan breakers or disconnectors are
  left behind.
* An **HVDC line** goes with its converter stations.
* A **voltage level** or a **substation** takes down everything it contains, and
  for a voltage level the far end of its lines and transformers as well. Both
  are refused unless `cascade=True`; the refusal says how many connectables and
  which branches would go, so ask first, then confirm.
* A **switch, bus or busbar section** is removed as is -- prefer removing the
  equipment itself, since deleting topology can disconnect things silently.

Ids of different types can be mixed in one call, in any order. This is not the
same as `set_line_status(line_id, False)`, which only *opens* a branch: an open
branch still exists and is still seen by a security analysis. Use
`set_line_status` for an N-1 study, removal for a structural change. Removal is
**not undoable** in the session; reload the file (or `duplicate_session()`
beforehand) to get the original back -- `clone_variant()` is not enough, it
isolates the state of the network, not its structure.

## Practical notes

* Every creation and removal clears the cached load-flow results: re-run
  `run_loadflow()` before reading any flow or voltage.
* Ids must be free across the whole network, whatever the element type.
* `create_network_elements` checks every item against its schema before creating
  anything, so a typo leaves the network untouched. A failure that only the
  network can detect (an id already taken, a connection point that does not
  exist) stops the batch there and the report says how far it got.
* Everything happens in memory. `export_network()` saves the extended grid.
* `describe_element_creation()` with no argument lists every type that can be
  created. If a type is not there, say so instead of inventing an
  `element_type`; `generate_python_script()` can produce pypowsybl code for what
  the server does not expose. `docs/tools_reference.md` carries the full
  supported / not-supported table.
* Whether a regulating tap changer or SVC is actually simulated depends on the
  load-flow parameters (transformer voltage control, phase shifter regulation,
  see `get_loadflow_params()`).
