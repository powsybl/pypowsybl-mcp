---
name: grid-extension
description: How to add equipment to a network (create_substation, create_voltage_level, create_load, create_generator, create_line, create_transformer, create_battery, create_shunt_compensator, create_static_var_compensator, create_ground), how to rate and regulate it (create_operational_limits, create_reactive_limits, create_ratio_tap_changer, create_phase_tap_changer), how to remove it again (remove_network_elements), and how to run a connection study - for instance connecting a new datacenter to the grid. Use when asked to connect, add, attach or build a new load, new generation, storage, compensation, a new site, a line or a transformer, to set limits or ratings on a created element, to add a tap changer, to remove, delete or retire an element, or when a creation tool answered that a bus or busbar section could not be used.
---

# Skill: extending a grid and running a connection study

The creation tools add equipment that does not exist yet. They are the
counterpart of `modify_network`, which only changes elements already in the
network.

| Tool                   | Creates                                                          |
| ---------------------- | ---------------------------------------------------------------- |
| `create_substation`    | A site container (no electrical equipment of its own)            |
| `create_voltage_level` | The busbar system of a site, **and its connection points**       |
| `create_load`          | A consumer (datacenter, factory, ...), connected through its bay |
| `create_generator`     | A production unit, connected through its bay                     |
| `create_line`          | An AC line, with the bay at each end                             |
| `create_transformer`   | A two-windings transformer, with the bay at each end              |
| `create_battery`       | A storage unit, connected through its bay                        |
| `create_shunt_compensator` | A capacitor bank or a reactor, connected through its bay      |
| `create_static_var_compensator` | An SVC (continuous reactive control), through its bay    |
| `create_ground`        | An earthing connection on a bus (bus/breaker only)               |

What is created then has to be **rated and regulated**, or the analyses cannot
see it:

| Tool                        | Attaches to                                      |
| --------------------------- | ------------------------------------------------ |
| `create_operational_limits` | A branch: permanent (+ temporary) current ratings |
| `create_reactive_limits`    | A generator, battery or converter: Q range or Q(P) curve |
| `create_ratio_tap_changer`  | A two-windings transformer: voltage regulation    |
| `create_phase_tap_changer`  | A two-windings transformer: active-power control  |

`remove_network_elements` is the inverse of all of them: one tool, any
element type, ids only.

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
  the remedial actions available there are the operational ones (`modify_network`
  to redispatch, `set_line_status` / `set_switch_status` for topology,
  `set_tap_position` for taps), never building new equipment.
* Never delete an element to make a violation, an overload or a divergence go
  away, and never "clean up" a network by removing what looks unused.
* Never invent equipment to complete a network that looks incomplete: report what
  is missing instead.

If extending or reducing the grid looks like the answer to the question and the
user did not ask for it, say so and ask -- describing what you would build or
remove -- rather than doing it. When in doubt, the answer is to not modify the
structure.

## The one rule about connection points

Equipment is never attached to a voltage level, it is attached to a **bus or
busbar section**, and only one family of ids works:

* **`NODE_BREAKER` voltage level** -> a **busbar section** id
  (`get_network_element_data(element_type='busbar_section')`).
* **`BUS_BREAKER` voltage level** -> a **configured bus** id, i.e. a bus of the
  bus/breaker view
  (`get_network_element_data(element_type='bus_from_bus_breaker_view')`).

The bus ids that appear in load-flow results, in `check_voltage_violations` or
in `get_network_element_data(element_type='bus')` belong to the **bus view**.
They are computed from the topology (`VL1_0`, `S1VL2_0`, ...) and **cannot host
an element**. On an IEEE test case, `VL1_0` is a bus-view bus while `B1` is the
configured bus to connect to. When a creation tool rejects an id it lists the
ones that would have worked in that voltage level -- use them instead of
guessing.

Check the topology kind of the voltage level you target with
`get_network_element_data(element_type='voltage_level')`; use the same kind for
a new voltage level as the network it will be connected to.

You never pass a `node` number nor create switches by hand: each tool builds
the bay (breaker, disconnectors) required by the hosting topology.

## Workflow: connecting a new datacenter

Say a 300 MW datacenter has to be connected to a 135 kV grid.

1. `get_network_info()` and `run_loadflow()` -- establish the baseline *before*
   the extension, so the impact can be attributed to it.
2. `create_substation(substation_id='SUB_DC', country='FR')` -- the new site.
3. `create_voltage_level(voltage_level_id='VL_DC', substation_id='SUB_DC',
   nominal_v=135.0, low_voltage_limit=128.0, high_voltage_limit=145.0)` --
   nominal voltage matches the grid it connects to. The answer lists the created
   connection points (e.g. `VL_DC_1_1`): **that id is what the next steps take**.
   Without voltage limits, `check_voltage_violations` can never report the new
   busbar.
4. `create_load(load_id='DATACENTER', bus_or_busbar_section_id='VL_DC_1_1',
   p0=300.0, q0=60.0)` -- `q0` around `0.2 * p0` corresponds to a 0.98 power
   factor.
5. `create_line(line_id='LINE_B4_DC', bus_or_busbar_section_id_1='B4',
   bus_or_busbar_section_id_2='VL_DC_1_1', r=0.5, x=5.0)` -- the connection
   itself. Until a line (or a transformer) exists, the new site is an
   electrical island and the load flow will not converge on it. Copy `r`/`x`
   from a comparable existing line
   (`get_network_element_data(element_type='line')`) rather than inventing
   values; orders of magnitude at 400 kV are r ~ 0.03 Ohm/km, x ~ 0.3 Ohm/km.
6. `create_operational_limits('LINE_B4_DC', permanent_limit=800.0)` -- **do not
   skip this**: a branch with no limits can never be reported as overloaded, so
   without it the next steps cannot conclude anything about the new line. Copy
   the rating of a comparable existing line.
7. `run_loadflow()` -- does it still converge?
8. `get_overloaded_elements()` and `check_voltage_violations()` -- is the
   connection acceptable in N?
9. `run_security_analysis()` -- is it still acceptable after an N-1? Use
   `set_line_status('LINE_B4_DC', False)` to check the site is not left alone
   on a single circuit.
10. Optionally `create_generator(...)` -- local generation, e.g. a solar farm
    with `voltage_regulator_on=True` and `target_v` in **kV** close to the
    nominal voltage, often turns a diverging or violated case into an
    acceptable one; give it a `create_reactive_limits()` range, or the load flow
    will hold the setpoint with unlimited reactive power. `create_battery()`,
    `create_shunt_compensator()` and `create_static_var_compensator()` cover
    storage and reactive compensation the same way.
11. `visualize_network()` or `plot_substation_single_line_diagram('SUB_DC')` to
    show the result, `export_network()` to keep it.

Working on a `clone_variant` (or a duplicated network) keeps the original case
available for comparison; note that creating equipment changes the network
itself, not only the variant state.

## Workflow: connecting a site at another voltage

A transformer is what links two voltages, and powsybl only accepts one **inside
a single substation**. So a site whose internal network runs at another voltage
than the grid it connects to has two voltage levels in the same substation:

1. `create_substation('SUB_DC', country='FR')`
2. `create_voltage_level('VL_DC_135', 'SUB_DC', nominal_v=135.0)` -- the grid side
3. `create_voltage_level('VL_DC_63', 'SUB_DC', nominal_v=63.0)` -- the site side
4. `create_transformer('TR_DC', 'VL_DC_135_1_1', 'VL_DC_63_1_1', r=0.2, x=10.0,
   rated_s=400.0)` -- `rated_u1`/`rated_u2` default to the nominal voltages of
   both ends, which is what sets the ratio; `x` is roughly
   `usc% / 100 * rated_u1^2 / rated_s`
5. `create_load('DATACENTER', 'VL_DC_63_1_1', p0=100.0, q0=20.0)` -- on the site side
6. `create_line('LINE_B4_DC', 'B4', 'VL_DC_135_1_1', r=0.5, x=5.0)` -- to the grid
7. `create_operational_limits('TR_DC', permanent_limit=1800.0)` and the same for
   the line, so both can be reported as overloaded
8. `create_ratio_tap_changer('TR_DC', regulating=True, target_v=63.0)` -- the
   on-load tap changer a real transformer has, without which the site voltage
   cannot be controlled and `set_tap_position()` has nothing to move. The steps
   are generated (±10% in 17 positions by default)
9. `run_loadflow()`, then the checks above

Trying to put a transformer between two substations is refused: use a line
there. Use `create_phase_tap_changer()` instead when the point is to control the
active power through the transformer rather than the voltage.

## Undoing or retiring equipment

`remove_network_elements(element_ids=[...])` deletes elements of any type; the
right removal is chosen from the type found in the network:

* A **feeder** (load, generator, line, transformer, shunt, SVC, converter
  station) is removed **with its bay**, so the breakers and disconnectors that
  attached it go too -- no orphan switching equipment is left behind.
* An **HVDC line** is removed with its converter stations.
* A **voltage level** or a **substation** takes down everything it contains,
  and for a voltage level the far end of its lines and transformers as well.
  Both are refused unless `cascade=True` is passed; the refusal says how many
  connectables and which branches would go, so ask first, then confirm.
* A **switch, bus or busbar section** is removed as is. Prefer removing the
  equipment itself: deleting topology can disconnect things silently.

Ids of any types can be mixed in one call, in any order -- feeders are handled
before the containers holding them, so a whole site can be listed at once. Each
id gets its own line in the report (`removed` / `REFUSED` / `NOT FOUND` /
`FAILED`), and one failure does not abort the others.

This is not the same as `set_line_status(line_id, False)`, which only *opens* a
branch: an open branch still exists and is still seen by a security analysis, a
removed one is gone. Use `set_line_status` for an N-1 study, removal for a
structural change.

Removal is **not undoable** in the session: the element is gone from the
in-memory network. Reload the file (or `duplicate_session()` beforehand) to get
the original back -- `clone_variant()` is not enough, it isolates the state of
the network, not its structure.

## What these tools do not cover

The tools follow **pypowsybl 1.15**, and they expose the equipment a connection
study needs, not the whole creation API. There is **no tool** for the following,
so do not announce them and do not invent tool names -- say what is missing, and
if the user needs it, `generate_python_script()` can produce pypowsybl code that
does it (`get_online_resource(class_object='network')` gives the exact
signatures). `docs/tools_reference.md` carries the full call-by-call table.

* **Building a network from scratch** (`create_empty`). Start from a loaded file
  or from `create_ieee_network()`.
* **Individual topology primitives**: buses, busbar sections, switches, internal
  connections, busbar coupling (`create_coupling_device`). Connection points come
  from `create_voltage_level`; there is no way to add one to an existing voltage
  level, nor to run a coupling maneuver by creating a breaker.
* **Tapping an existing line** to insert a new substation on it
  (`create_line_on_line`, `connect_voltage_level_on_line`). Run a new line from
  an existing busbar with `create_line()` instead.
* **Three-windings transformers**, and any tap changer on one.
* **HVDC links and the DC grid** (HVDC lines, converter stations, DC nodes, DC
  lines). Existing HVDC links can still be inspected and removed.
* **Boundary lines, tie lines and areas** (CGMES boundary modelling).
* **Non-linear shunt models** (per-section susceptance tables); only the linear
  model is exposed.
* **Grounds in node/breaker voltage levels**: pypowsybl 1.15 has no ground bay,
  so `create_ground` only works on a bus of a bus/breaker voltage level.
* **Extensions** of any kind; the bay tools fill in the position extension by
  themselves and nothing else can be set.

Two limits of the pinned version show up in the tool arguments: a static var
compensator has no `OFF` regulation mode (create it with `regulating=False`),
and a phase tap changer has no `FIXED_TAP` mode (same). A created transformer
also has no tap changer until `create_ratio_tap_changer` or
`create_phase_tap_changer` adds one, and a created branch has no limits until
`create_operational_limits` sets them.

## Practical notes

* Every creation clears the cached load-flow results: re-run `run_loadflow()`
  before reading any flow or voltage.
* Ids must be free across the whole network, whatever the element type.
* A new line or transformer has **no current limits** until
  `create_operational_limits()` gives it some, and an element without limits can
  never be reported as overloaded by `get_overloaded_elements()`, by
  `loading_percent` or by a security analysis. Rate every branch you create.
* Connecting two connection points of different nominal voltages with a line is
  accepted but flagged: physically a transformer belongs there.
* Everything happens in memory. `export_network()` saves the extended grid.
* Only two-windings transformers can be created (three-windings ones have no bay
  creation in pypowsybl). A created transformer has no tap changer until
  `create_ratio_tap_changer` / `create_phase_tap_changer` adds one.
* Whether a regulating tap changer or SVC is actually simulated depends on the
  load-flow parameters (transformer voltage control, phase shifter regulation,
  see `get_loadflow_params()`).
