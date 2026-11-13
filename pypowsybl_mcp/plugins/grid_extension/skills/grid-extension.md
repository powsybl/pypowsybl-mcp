---
name: grid-extension
description: How to add equipment to a network with create_network_element / create_network_elements (substations, voltage levels, loads, generators, batteries, shunts, SVCs, lines, transformers, HVDC links, operational limits, reactive limits, tap changers), how to remove it again with remove_network_elements, how to insert a substation on an existing line, tap a line or couple busbars with modify_network_topology, and how to run a connection study - for instance connecting a new datacenter to the grid. Use when asked to connect, add, attach or build a new load, new generation, storage, compensation, a new site, a line or a transformer, to rate a branch or add a tap changer, to remove, delete or retire an element, to cut a line into a new substation (loop-in / insertion), tap a line (tee connection), couple two busbars or undo those, or when a creation call answered that a bus or busbar section could not be used.
---

# Skill: extending a grid and running a connection study

Five tools cover every element type and the topology around them:

| Tool | What it does |
| ---- | ------------ |
| `describe_element_creation(element_type)` | What a type needs: required attributes, units, accepted values, rules, guidance. With no argument, the list of creatable types |
| `create_network_element(element_type, attributes)` | Creates one element |
| `create_network_elements(items)` | Creates several in one call, ordered so containers come before what they contain |
| `remove_network_elements(element_ids)` | The inverse: removes elements of any type by id |
| `modify_network_topology(operation, attributes)` | Reconnects existing lines: inserts a substation on a line, taps a line into another, couples busbars, and reverts those |

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

Attachments -- `loading_limits`, `minmax_reactive_limits`,
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
  {"element_type": "loading_limits", "element_id": "LINE_B4_DC",
   "permanent_limit": 800.0}
])
```

Around it:

1. `get_network_info()` and `run_loadflow()` **before** the extension, so the
   impact can be attributed to it.
2. The call above. Items may be listed in any order. A new voltage level
   generates its connection points and the reply gives their ids
   (`VL_DC_1_1` here) -- the pattern is `<voltage level>_<busbar>_<section>`, and
   later items in the same call can reference them. With several busbars
   (`aligned_buses_or_busbar_count` above 1) they come **uncoupled**: each busbar
   is a separate node, and equipment on busbar 2 is cut off from a line arriving
   on busbar 1. The reply says so in `notes`; couple them with
   `create_coupling_device` (below) when they should work as one node.
3. `run_loadflow()` -- does it still converge?
4. `check_current_violations()` and `check_voltage_violations()` -- acceptable in N?
5. `run_security_analysis()` -- still acceptable after an N-1?
   `set_line_status('LINE_B4_DC', False)` checks the site is not left alone on a
   single circuit.
6. `visualize_network()` or `plot_substation_single_line_diagram('SUB_DC')` to
   show it, `export_network()` to keep it.

Choosing values: `q0` around `0.2 * p0` is a 0.98 power factor; copy `r`/`x`
from a comparable existing line (`get_network_element_data(element_type='line')`)
rather than inventing them -- at 400 kV, r ~ 0.03 Ohm/km and x ~ 0.3 Ohm/km.

**Do not skip the limits.** A branch with no operational limits can never be
reported as overloaded: it is invisible to `check_current_violations()`, to
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
   "bus_or_busbar_section_id_2": "VL_63_1_1", "r": 0.05, "x": 1.2,
   "rated_s": 400.0},
  {"element_type": "load", "id": "DATACENTER",
   "bus_or_busbar_section_id": "VL_63_1_1", "p0": 100.0, "q0": 20.0},
  {"element_type": "line", "id": "LINE_B4_DC", "bus_or_busbar_section_id_1": "B4",
   "bus_or_busbar_section_id_2": "VL_135_1_1", "r": 0.5, "x": 5.0},
  {"element_type": "loading_limits", "element_id": "TR_DC",
   "permanent_limit": 1800.0},
  {"element_type": "ratio_tap_changer", "element_id": "TR_DC",
   "regulating": true, "target_v": 63.0}
])
```

`rated_u1` / `rated_u2` default to the nominal voltages of the two ends, which is
what sets the ratio. `r` and `x` are in ohms **referred to side 2**, so `x` is
roughly `usc% / 100 * rated_u2^2 / rated_s`: 1.2 Ohm for the 400 MVA, 12 %
transformer above, whose side 2 is at 63 kV. Its limits rate side ONE in amps
(1800 A at 135 kV, about the 400 MVA rating), which is what `loading_limits`
does when no `side` is given: the overload checks derive side TWO from the ratio.
Side ONE is the `voltage_level1_id` end -- here `bus_or_busbar_section_id_1`,
the 135 kV one -- but on an existing transformer it is not always the
high-voltage end: on many grids side 1 is high voltage and the ratings are
given for the low-voltage winding. Amps belong to the winding they were rated
on, so read `voltage_level1_id` / `voltage_level2_id` with
`get_network_element_data(element_type='two_windings_transformer')` first, and
pass `side='TWO'` for amps of the `voltage_level2_id` end.
`side='BOTH'` is refused on a transformer, as the same amps on the 63 kV winding
would report it overloaded at a fraction of its rating; a three-windings
transformer takes one call per leg (`side` ONE, TWO or THREE). A
created transformer has **no tap changer** until a `ratio_tap_changer` (voltage)
or `phase_tap_changer` (active power) is added, and until then
`set_tap_position()` has nothing to move. Their steps are generated as a ladder
(+/-10% over 17 positions, or +/-10 degrees), starting on the neutral middle
step; pass `steps` explicitly to describe them yourself. A transformer can have
both, but **only one may regulate**: a regulating one is refused while the other
regulates. To hand the regulation over, create the regulating one again with
`regulating: false` first.

## Other equipment worth knowing

* `generator` -- `target_v` is in **kV**, not per-unit. A regulating unit with
  no reactive limits holds its setpoint with unlimited MVAr, which flatters a
  study: add `minmax_reactive_limits` or a
  `reactive_capability_curve_point` list.
* `battery` -- `min_p` defaults to `-max_p`, a symmetric charge/discharge range.
* `shunt_compensator` -- identical sections: `b_per_section` (and
  `max_section_count`); sections that differ: `sections` as a list of `{b, g}`,
  where each `b` is the total with that section and the ones before it in. For
  a bank rated Q MVAr at U kV, `b = Q / U^2`. Positive is a capacitor, negative
  a reactor.
* `static_var_compensator` -- continuous reactive control; there is no `OFF`
  mode in this pypowsybl version, use `regulating: false`.
* `hvdc_line` -- create the two `vsc_converter_station` (or
  `lcc_converter_station`) first, then the link naming them; one
  `create_network_elements` call does all three in the right order. Each
  station ends one HVDC line only: to carry more power between two stations
  already linked, raise the existing line's `max_p` with `modify_network`
  instead of adding a line.
* `boundary_line` -- the equivalent of what lies beyond a border: `p0`/`q0` is
  the exchange at its far end. Add a generation part (`min_p`, `max_p`,
  `target_p`, optionally regulating with `target_v`) when the outside can
  produce. Two boundary lines sharing a `pairing_key` can then be joined into a
  `tie_line`, in the same batch.

## Workflow: inserting a substation on an existing line

When a new site is connected **on** an existing line rather than by a new line
to an existing substation (a "loop-in"), build the
substation and its voltage level, at the line's nominal voltage, with
`create_network_elements`, then cut the line at its busbar:

```
modify_network_topology("connect_voltage_level_on_line",
    {"bbs_or_bus_id": "VL_NEW_1_1", "line_id": "LINE_A_B",
     "position_percent": 30})
```

* The line is replaced by `LINE_A_B_1` (from its side 1 to the new busbar) and
  `LINE_A_B_2` (from the busbar to side 2), unless `line1_id` / `line2_id` name
  them; its impedance is split at `position_percent` (from side 1, 50 by
  default) and its limits are copied to both halves.
* **A line cannot change voltage**: a busbar at another nominal voltage is
  refused. Connect at another voltage with a transformer, as in the workflow
  above.
* A **tee connection** -- a new line tapped from a point along the existing
  one, without a substation there -- is `create_line_on_line`, with the new
  line's `new_line_id`, `new_line_r` and `new_line_x`. The tapped line has **no
  limits**: rate it with `loading_limits`. A tee can later become a real
  connection with `replace_tee_point_by_voltage_level_on_line`.
* `create_coupling_device` couples two busbars of one voltage level, with a
  breaker and its disconnectors. A new voltage level with N busbars needs N-1
  of them -- busbar 1 to each other one, in any one section, since the sections
  along a busbar are already joined.
* `revert_connect_voltage_level_on_line` and `revert_create_line_on_line` merge
  the halves back into one line; the switches left without equipment are
  removed with them. If that empties a voltage level, pypowsybl removes it and
  its substation, and the reply says so in `notes`. In a bus/breaker voltage
  level, pypowsybl cannot remove the bus it created for a cut or a tap: it
  stays, empty, and a line tapped there and reverted cannot be tapped there
  again (the bus is named after the line).
* If pypowsybl fails part way, the failure reply lists in `created_ids` and
  `removed_ids` what it had already changed: check those before going on.

The same rule applies as for building: only when the user asked for this
change. Opening a line is `set_line_status`, and switching busbars apart is
`set_switch_status`; neither is a topology modification.

## Element types without a profile

Some types -- `bus`, `busbar_section`, `switch`, `internal_connection`,
`three_windings_transformer`, the `area*` types -- have no profile. They are marked
`checked: schema only` in `describe_element_creation()` and created straight
from the pypowsybl schema:

* only attribute names and types are checked; required attributes, accepted
  values and topology rules are pypowsybl's, and its refusal comes back as is;
* **no switching equipment is built** and no connection point is resolved: give
  `voltage_level_id` with `bus_id` (bus/breaker) or `node` (node/breaker), the
  way pypowsybl takes them;
* a `switch` in a bus/breaker voltage level must name two buses of that voltage
  level (`bus1_id`, `bus2_id`); any other is refused before pypowsybl sees it,
  as pypowsybl would keep a broken switch under that id;
* a `busbar_section` cannot be added to a node/breaker voltage level created
  here, nor to any whose sections have a position: every later feeder bay in
  it would fail. Create the voltage level with the busbars and sections it
  needs (`aligned_buses_or_busbar_count`, `section_count`) instead;
* an `area_voltage_levels` or `area_boundaries` item adds one entry to the
  area, keeping the ones it has; a voltage level belongs to at most one area
  of each type;
* in a batch each one takes its place after what it names -- buses after
  their voltage level, switches after their buses, area boundaries after the
  equipment they name -- so they too can be listed in any order.

Prefer a profiled type whenever one does the job.

The detailed DC model -- `dc_node`, `dc_line`, `dc_ground`,
`voltage_source_converter` -- is not offered at all: once one of them exists,
every AC load flow fails until an OpenLoadFlow parameter (`acDcNetwork`) is set
that `update_loadflow_params()` cannot reach. Model an HVDC link with two
`vsc_converter_station` (or `lcc_converter_station`) and an `hvdc_line`.

## Undoing or retiring equipment

`remove_network_elements(element_ids=[...])` deletes elements of any type, and
picks the right removal from the type it finds in the network:

* A **feeder** (load, generator, line, transformer, shunt, SVC, converter
  station) goes **with its bay**, so no orphan breakers or disconnectors are
  left behind.
* An **HVDC line** goes with its converter stations.
* A **voltage level** or a **substation** takes down everything it contains, and
  for a voltage level the far end of its lines and transformers as well. Both
  are refused unless `cascade=True`; the refusal lists what would go -- the
  equipment inside, and by id what lies beyond it: branches, HVDC links with
  their far-end converter station, tie lines -- so ask first, then confirm.
  A voltage level left with no equipment (only its own buses or busbar
  sections) is removed without `cascade`.
* A **switch, bus or busbar section** is removed as is -- prefer removing the
  equipment itself, since deleting topology can disconnect things silently.

Ids of different types can be mixed in one call, in any order. This is not the
same as `set_line_status(line_id, False)`, which only *opens* a branch: an open
branch still exists and is still seen by a security analysis. Use
`set_line_status` for an N-1 study, removal for a structural change. Removal is
**not undoable** in the session; reload the file (or `duplicate_session()`
beforehand) to get the original back -- `clone_variant()` is not enough, it
isolates the state of the network, not its structure.

**Limits, tap changers and reactive limits cannot be removed on their own.**
They have no id of their own: `remove_network_elements` on the line,
transformer or generator they belong to removes that whole element. To change
them, create them again -- that replaces them. When the user asks to remove one,
say it cannot be done, and offer to replace it, or to remove and recreate the
element if that is what they want.

## Practical notes

* Every creation, removal and topology change that changes the network clears the cached
  load-flow results: re-run `run_loadflow()` before reading any flow or voltage.
  A rejected call leaves them in place.
* Ids must be free across the whole network, whatever the element type.
* `create_network_elements` checks every item against its schema before creating
  anything -- attribute names, required attributes, types, accepted values and
  table rows such as tap-changer steps, and ids used twice in the batch (an
  element and its limits or tap changers share an id, which is fine) -- so such a
  mistake leaves the network untouched. A failure that only the
  network can detect (an id already in the network, a connection point that does not
  exist) stops the batch there; the reply says how far it got (`stopped_at`,
  and the `status` of each item).
* Everything happens in memory. `export_network()` saves the extended grid.
* `describe_element_creation()` with no argument lists every type that can be
  created. If a type is not there, say so instead of inventing an
  `element_type`; `generate_python_script()` can produce pypowsybl code for what
  the server does not expose. `docs/grid_extension.md` carries
  the full supported / not-supported table.
* Whether a regulating tap changer or SVC is actually simulated depends on the
  load-flow parameters (transformer voltage control, phase shifter regulation,
  see `get_loadflow_params()`).
