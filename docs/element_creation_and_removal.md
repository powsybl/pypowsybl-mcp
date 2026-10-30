### Creating and removing network elements

This page explains how the creation and removal tools work: what each tool does, where the knowledge about
every element type comes from, what happens during a call, and how the whole thing stays in step with the installed
`pypowsybl`. It describes the code as it is.

They form the **grid-extension plugin** (see [grid_extension.md](grid_extension.md)): a module of the server,
`pypowsybl_mcp/plugins/grid_extension/`, active by default and turned off with `MCP_DISABLED_PLUGINS=grid_extension`.

These are long-term study tools (connection studies, network development). They change the structure of the
in-memory network, and are only meant to be used when the user explicitly asks for equipment to be built or retired.

---

#### 1. The tools

| Tool                                                       | What it does                                                                                     |
|------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| `describe_element_creation(element_type=None)`             | Lists the creatable types, or describes one: required and optional attributes, units, accepted values, rules, guidance. |
| `create_network_element(element_type, attributes, network_id=None)` | Creates one element of any type.                                                         |
| `create_network_elements(items, network_id=None)`          | Creates several elements in one call, sorted so that containers come before what they contain.   |
| `remove_network_elements(element_ids, cascade=False, network_id=None)` | Removes elements of any type by id, with the removal that suits each type.            |

There is one creation tool for every element type, rather than one tool per type. The attributes of a type are not in
the tool signature: `attributes` is an open object, and three things tell the caller what to put in it:

- a **cheat sheet** of the minimal attributes of the most-used types, inlined in the description of
  `create_network_element` (so it is always in the client's context);
- `describe_element_creation`, for everything else;
- **errors that carry the relevant part of the descriptor**, so a rejected call can be corrected without a further
  lookup (see §10).

When `network_id` is omitted, every tool works on the session's current network.

---

#### 2. Code map

| Path                                        | Role                                                                                       |
|---------------------------------------------|--------------------------------------------------------------------------------------------|
| `pypowsybl_mcp/plugins/grid_extension/plugin.py` | The plugin entry points: registers the tools, the skill and `/admin/creation-coverage`. |
| `pypowsybl_mcp/plugins/grid_extension/creation_tools.py` | The three creation tools and the cheat sheet. Thin: they resolve the network and call the engine. |
| `pypowsybl_mcp/plugins/grid_extension/removal_tools.py` | The removal tool, its per-type dispatch and the cascade summary.                           |
| `pypowsybl_mcp/plugins/grid_extension/creation/schema.py`          | What pypowsybl says about its creation calls: field names, types, dataframe groups, and the `create_*` method of each type. Nothing hand-written. |
| `pypowsybl_mcp/plugins/grid_extension/creation/doc_mining.py`      | Attribute descriptions and units parsed from pypowsybl docstrings. Informational only.     |
| `pypowsybl_mcp/plugins/grid_extension/creation/profiles.py`        | The hand-written overlay: one profile per element type, plus the schema-only profiles derived for the others. |
| `pypowsybl_mcp/plugins/grid_extension/creation/rules.py`           | Constraints a profile can declare (conditional requirements, orderings, bounds).           |
| `pypowsybl_mcp/plugins/grid_extension/creation/derivations.py`     | Values the engine fills in on the caller's behalf (feeder positions, rated voltages, tap steps, limit rows). |
| `pypowsybl_mcp/plugins/grid_extension/creation/executors.py`       | How each kind of element is handed to pypowsybl (bay helpers, plain calls, several dataframes). |
| `pypowsybl_mcp/plugins/grid_extension/creation/engine.py`          | The single pipeline every creation goes through, and the descriptors.                      |
| `pypowsybl_mcp/plugins/grid_extension/creation/context.py`         | `CreationError`, connection points, and the context shared by rules, derivations and executors. |
| `pypowsybl_mcp/plugins/grid_extension/creation/drift.py`           | The profile checks against the installed pypowsybl, the coverage snapshot and the startup check. |
| `pypowsybl_mcp/plugins/grid_extension/creation/coverage_snapshot.json` | What the tools covered on the pypowsybl version they were checked against. Generated.  |
| `scripts/creation_coverage.py`              | Generates the coverage tables of `docs/grid_extension.md` and the snapshot.                    |
| `pypowsybl_mcp/plugins/grid_extension/skills/grid-extension.md` | The skill teaching a model the workflows (connecting a datacenter, a site at another voltage). |

---

#### 3. Three groups of element types

Every element type pypowsybl can create falls in one of three groups. The current lists are generated in
[grid_extension.md](grid_extension.md#coverage-of-the-pypowsybl-creation-api).

| Group           | Count (1.15.0) | What is checked                                                                 | Switching equipment |
|-----------------|----------------|---------------------------------------------------------------------------------|---------------------|
| **Profiled**    | 20             | Attribute names, types, required attributes, accepted values, rules; connection points are resolved and diagnosed. | Built by the bay helpers for injections and branches. |
| **Schema only** | 12             | Attribute names and types; everything else is left to pypowsybl, whose error is returned as is. | None: the caller gives `voltage_level_id` with `bus_id` or `node`. |
| **Unsupported** | 1 (`alias`)    | Refused: pypowsybl creates aliases with `add_aliases`, not a `create_*` call.   | —                   |

A name that is not creatable at all is refused with a message that says why:

- a computed or read-only type (`branch`, `injection`, `selected_operational_limits`, …): *"cannot be created:
  pypowsybl computes it from other elements or only reads it"*;
- an unsupported type (`alias`): *"has creation metadata in pypowsybl but no Network.create_\* call, so this tool does
  not support it yet"*;
- an unknown name: *"No element type 'swtich' exists. Did you mean 'switch'?"*

Element-type names are the pypowsybl `ElementType` members lowercased — the same vocabulary as
`get_network_element_data` and the `element-types` skill.

---

#### 4. Where the knowledge comes from

##### The introspected schema

`schema.py` reads the attributes of every creation call from two pypowsybl C-API calls, so this part follows the
installed version without maintenance:

- `get_network_elements_creation_dataframes_metadata(element_type)` — the attributes of `Network.create_<x>()`;
- `get_network_modification_metadata_with_element_type(modification, element_type)` — the attributes of the bay
  helpers (`create_load_bay`, `create_line_bays`, …), which add the placement fields `bus_or_busbar_section_id`,
  `position_order`, `direction`.

Both return groups of fields, one per dataframe the call takes: a shunt compensator comes back as three groups (the
shunt, the linear model, the non-linear model), a tap changer as two (the changer and its steps). Each field has a
name, a type (string, number, integer, boolean) and an index flag that names the key column (`id` for most types).

`schema.py` also builds `CREATION_METHODS`, the `Network.create_*` method behind each element type. Every such method
funnels into `Network._create_elements(element_type, …)`; at import, each one is called once on an empty network with
that funnel replaced by a spy that records the element type and stops before the C layer. It runs once, before the
server handles any request.

What the metadata does **not** say is why a hand-written overlay exists:

- which attributes are required — `is_modifiable` and `is_default` are false for every creation field;
- units, accepted values of enumerations, conditional requirements;
- which call is legal for a type — `CREATE_FEEDER_BAY` metadata is returned even for a substation;
- quirks to hide — the substation metadata lists both `tso` and `TSO`.

##### Profiles

A profile (`CreationProfile` in `profiles.py`) supplies exactly that:

| Field            | Meaning                                                                                                  |
|------------------|----------------------------------------------------------------------------------------------------------|
| `executor`       | How to call pypowsybl for this type (§7).                                                                |
| `connection`     | How the element joins the network (§6).                                                                  |
| `required`       | Attributes without which the element is refused, beyond its id and connection point.                     |
| `managed`        | Attributes the caller must not pass: computed by the engine, filled by the bay helper, or metadata artifacts. |
| `defaults`       | Values applied when an attribute is missing.                                                             |
| `units`, `enums` | Units shown in descriptors and reports; the accepted values of enumerations.                              |
| `rules`          | Constraints checked before pypowsybl is called (§8).                                                     |
| `derivations`    | Values the engine computes when the caller leaves them out (§8).                                         |
| `virtual`        | Convenience attributes that are not pypowsybl fields, turned into real ones by a derivation (§9).         |
| `rows_attribute` | The attribute holding a table of rows, for calls that take one (§9).                                     |
| `target_types`   | For attachments: the element types they may attach to.                                                  |
| `order`          | Position in a batch (§11).                                                                               |
| `summary`, `guidance` | One line, and a paragraph on what the element is for and how to choose values.                      |
| `docs`           | The pypowsybl calls whose docstrings document the attributes, named as strings and resolved when used.   |

For example, the load profile:

```python
CreationProfile(
    element_type="load",
    summary="A consumption point: a datacenter, a factory, an extra demand.",
    executor=FeederBay("create_load_bay"),
    connection=Connection.SINGLE,
    order=40,
    required=("p0",),
    managed=BAY_MANAGED,  # voltage_level_id, bus_id, connectable_bus_id, node, feeder_type
    defaults={"q0": 0.0},
    units={"p0": "MW", "q0": "MVAr"},
    enums={"type": ("UNDEFINED", "AUXILIARY", "FICTITIOUS"), "direction": DIRECTIONS},
    derivations=(
        derive.PositionOrder("position_order"),
        derive.DirectionOnlyInNodeBreaker("direction"),
    ),
    guidance="p0 is positive for consumption: a 300 MW datacenter is p0=300. ...",
    docs=("pp.network.create_load_bay", "Network.create_loads"),
)
```

Calls are named as strings everywhere (executors and `docs`), so a pypowsybl upgrade that removes one cannot stop the
server from importing; it disables that profile instead (§14).

##### Docstring mining

pypowsybl documents its creation attributes in a regular `- **name**: description` format. `doc_mining.py` parses the
docstrings of a profile's `docs` calls **at runtime**, so the descriptions shown by `describe_element_creation` always
come from the installed version. When a name is documented twice, the first description wins (pypowsybl 1.15
documents the shunt deadband as a second `target_v`). A unit stated in a description ("in MW") is used when the
profile gives none. Mining never changes behavior: required attributes, enumerations and rules stay hand-written.

##### Schema-only profiles

Every creatable type without a hand-written profile but with a `create_*` method gets one derived from its schema
alone (`UNPROFILED`, built by `schema_only_profile`). It uses the `Raw` executor on the main dataframe only (any
further dataframe of the call is not accepted), a generic guidance paragraph, the first line of
the pypowsybl docstring as summary, and batch order 30. The engine skips its network checks: the id of such a type may
name another element (an internal connection is keyed on its voltage level, an `area_voltage_levels` row on an
existing area), so pypowsybl is left to check ids and references. A hand-written profile always wins over the
fallback.

---

#### 5. A creation, step by step

`CreationEngine.create(network, element_type, attributes)` runs the same steps for every type:

1. **Look the profile up** — hand-written first, then schema-only; otherwise refuse with one of the messages of §3.
2. **Normalize the attribute names** — strip them, and map the everyday aliases to pypowsybl names:
   `bus`, `bus_id`, `busbar_section_id`, `connection_point` → `bus_or_busbar_section_id`; `bus1`/`bus2` (and
   `bus1_id`/`bus2_id`) → `bus_or_busbar_section_id_1`/`_2`; `load_type` → `type`. `id` and `element_id` both map to the
   profile's id attribute.
3. **Take the id out** — it is required.
4. **Reject unknown and managed attributes** — an unknown name comes back with up to three close matches and the list
   of accepted attributes; a managed one is refused with the list of what is set automatically.
5. **Resolve the connection points** (§6) and check that the id of the new element is free across *every* element of
   the network, whatever its type. For an attachment, check instead that the target exists and is of an accepted
   type. Skipped for schema-only types.
6. **Check the required attributes.**
7. **Check the values** — enumerations are upper-cased and checked against their accepted values; other values are
   coerced to the field type (`"0.95"` becomes `0.95`); the rows of a table attribute are checked the same way (§9).
8. **Apply defaults, then derivations, then rules** (§8).
9. **Call pypowsybl** through the executor, with the attributes that are set, virtual ones excluded.
10. **Report** — a `CreationResult`: whether an element was created or an existing one updated, the pypowsybl
    calls used, a one-line message (the required values with their units, where it is connected), the ids the
    executor generated (the connection points of a new voltage level), and notes from rules or from the schema-only
    path. The tools turn it into the JSON reply of §10.

For example:

```text
create_network_element("load", {"id": "DATACENTER", "bus": "VL_DC_1_1", "p0": 300.0, "q0": 60.0})
```
```json
{
  "success": true,
  "network_id": "net1",
  "element_type": "load",
  "methods": ["pp.network.create_load_bay"],
  "created_ids": ["DATACENTER"],
  "updated_ids": [],
  "created_count": 1,
  "connection_points": [],
  "notes": [],
  "errors": [],
  "message": "created load 'DATACENTER' (p0=300.0 MW) connected to 'VL_DC_1_1' in VL_DC",
  "loadflow_invalidated": true
}
```

Steps 1 to 8 raise `CreationError` before anything reaches pypowsybl. Executors never raise it, so a `CreationError`
always means the network is unchanged.

---

#### 6. Connecting an element

##### Connection kinds

| `Connection` | Attributes                                              | Used by                                               |
|--------------|---------------------------------------------------------|-------------------------------------------------------|
| `NONE`       | none (containers, or links naming existing elements)    | substation, voltage level, HVDC line, tie line, schema-only types |
| `SINGLE`     | `bus_or_busbar_section_id`                              | loads, generators, batteries, shunts, SVCs, converter stations, boundary lines |
| `DOUBLE`     | `bus_or_busbar_section_id_1`, `bus_or_busbar_section_id_2` | lines, two-windings transformers                   |
| `BUS`        | `bus_or_busbar_section_id`, which must be a configured bus | ground (pypowsybl has no ground bay)              |
| `TARGET`     | the id of an existing element                           | operational limits, reactive limits, tap changers     |

##### Connection points

An injection or a branch attaches to a **connection point**: a busbar section in a node/breaker voltage level, or a
bus of the bus/breaker view in a bus/breaker one. pypowsybl only answers "Bus or busbar section X not found" when
given anything else, so `resolve_connection_point` tells the three usual mistakes apart and names the ids that would
have worked:

- a bus of the **bus view** (`VL1_0`, the ids load-flow results report), which is computed from the topology and can
  host nothing;
- a bus of the bus/breaker view of a **node/breaker** voltage level, where a busbar section is required;
- an id that does not exist.

Injections and branches are created through the pypowsybl **bay helpers**, which build the switching equipment the
topology needs: in node/breaker, a breaker and a closed disconnector to the busbar section; in bus/breaker, a direct
connection to the bus. Node/breaker bays also need a feeder position: `position_order` is derived from the highest
existing position in the network plus a step, and `direction` is left to pypowsybl's default (`BOTTOM`) unless given.
Both are dropped in bus/breaker topology, where they mean nothing.

##### Voltage levels

A voltage level without connection points can host nothing, so creating one is two pypowsybl calls:
`Network.create_voltage_levels`, then `create_voltage_level_topology`, which fills it with buses (bus/breaker) or
busbar sections (node/breaker). Defaults give one busbar and one section, in `BUS_BREAKER` topology; with several
sections, the switches between them default to disconnectors. pypowsybl generates the ids of the connection points
(`VL_DC_1_1` for busbar 1, section 1) and the reply lists them in `connection_points`, since every following creation
needs them. If the
topology call fails, the voltage level is removed again, so a corrected call is not refused as a duplicate id.

---

#### 7. Executors

| Executor        | pypowsybl call                                                    | Types                                                  |
|-----------------|-------------------------------------------------------------------|--------------------------------------------------------|
| `FeederBay`     | `pp.network.create_<x>_bay(network, **attributes)`                | load, generator, battery, SVC, VSC and LCC converter stations |
| `MultiFrameBay` | one bay call with several dataframes, each passed only when filled | shunt compensator (shunt + linear or non-linear sections), boundary line (line + optional generation part) |
| `BranchBays`    | `create_line_bays`, `create_2_windings_transformer_bays`          | line, two-windings transformer                         |
| `VoltageLevel`  | `Network.create_voltage_levels` + `create_voltage_level_topology` | voltage level                                          |
| `Raw`           | `Network.create_<x>s(**attributes)`, no switching equipment       | substation, ground, HVDC line, tie line, every schema-only type |
| `RowGroup`      | a header dataframe and a table of rows                            | ratio and phase tap changers                           |
| `Attachment`    | rows attached to an existing element                              | operational limits, min/max reactive limits, capability curve |

The executor, not the metadata, decides which call is legal for a type: that is how a "substation feeder bay", which
the metadata would happily describe, can never be attempted.

---

#### 8. Rules and derivations

Rules run after defaults and derivations, so they see the final values:

| Rule                 | Meaning                                                           | Example                                         |
|----------------------|-------------------------------------------------------------------|-------------------------------------------------|
| `RequiredIf`         | an attribute is required when another is true                     | generator `target_v` when `voltage_regulator_on` |
| `RequiredWhenEquals` | required when another attribute has a given value                 | SVC `target_v` when `regulation_mode` is `VOLTAGE` |
| `DefaultUnless`      | give a value when missing, unless another attribute is true        | generator `target_q` = 0 unless regulating      |
| `Ordered`            | values must be non-decreasing                                     | `min_p <= target_p <= max_p`                    |
| `NonZero`, `Positive`, `AtLeast`, `AtMost` | bounds                                       | line `x` non-zero, at least one busbar          |
| `DistinctEnds`       | the two ends of a branch are different connection points          | line, transformer                               |
| `SameSubstation`     | both ends in one substation                                       | two-windings transformer                        |
| `BusBreakerOnly`     | refused in node/breaker topology                                  | ground                                          |
| `WarnIfDifferent`    | not an error: a note in the reply                                 | a line between two different nominal voltages   |
| `RowsRequired`       | a table attribute needs a minimum number of rows                  | at least two tap positions, two curve points    |
| `ColumnRequired`, `AtMostRows` | every row gives a column; a count bounded by the number of rows | every shunt section has `b`; sections in service |
| `Exclusive`          | two ways of describing one thing, never both                      | identical shunt sections, or one row per section |
| `OptionalPart`       | a group of attributes absent, or given as a whole                 | the generation part of a boundary line          |

| Derivation                   | Fills in                                                              |
|------------------------------|-----------------------------------------------------------------------|
| `PositionOrder`              | the feeder position of a node/breaker bay                             |
| `DirectionOnlyInNodeBreaker` | drops `direction` in bus/breaker topology                             |
| `FromConnection`             | the voltage level and bus of an element created without a bay (ground) |
| `NominalVoltage`             | a transformer's `rated_u1`/`rated_u2` from the nominal voltages of its ends |
| `Negated`                    | a battery's `min_p` as `-max_p`                                       |
| `TapSteps`, `MiddleTap`      | the steps of a tap changer from a range, starting on the middle step  |
| `SectionModel`               | a shunt's `model_type`: `NON_LINEAR` when `sections` are given, `LINEAR` otherwise |
| `LimitRows`                  | the rows of a limits group from a permanent rating and temporary ones |

---

#### 9. Table and virtual attributes

Some calls take a table rather than a single row. The profile names the attribute holding it (`rows_attribute`):

| Type                              | Table attribute | Rows                                                  |
|-----------------------------------|-----------------|-------------------------------------------------------|
| `ratio_tap_changer`               | `steps`         | `{rho, r, x, g, b}`, defaults filled per row          |
| `phase_tap_changer`               | `steps`         | `{alpha, rho, r, x, g, b}`                            |
| `shunt_compensator` (non-linear)  | `sections`      | `{b, g}` per section, `b` cumulative up to that section |
| `reactive_capability_curve_point` | `points`        | `{p, min_q, max_q}`, at least two                     |
| `operational_limits`              | `limits`        | one row per (side, duration)                          |

Rows are checked like top-level attributes: column names (with "did you mean"), types and accepted values. For
attachments, every row must give the same columns, since the rows become one list per column.

Writing these tables by hand is where callers go wrong, so profiles also declare **virtual** attributes, turned into
rows by a derivation:

- tap changers: `step_count` and `range_percent` (ratio) or `max_angle_degrees` (phase) generate a regular ladder,
  ±10 % or ±10° over 17 positions by default, starting on the middle step;
- limits: `permanent_limit`, `temporary_limits` (a list of `{value, acceptable_duration}`) and `limit_type` generate
  the rows, the permanent one with an acceptable duration of -1. `side` defaults to `BOTH`. Calling again for the same
  element and side **replaces** its limits.

A test asserts that no virtual attribute is also a pypowsybl field, so if upstream ever provides one, the profile has
to stop faking it.

---

#### 10. Describing a type, errors and replies

`describe_element_creation()` lists every creatable type with its summary; schema-only types carry
`checked: schema only`. `describe_element_creation(element_type)` returns the descriptor:

| Key                             | Content                                                                 |
|---------------------------------|-------------------------------------------------------------------------|
| `summary`, `guidance`           | what the element is for and how to choose values                        |
| `created_by`, `how`             | the pypowsybl call(s), and what they build                              |
| `id_attribute`                  | `id`, or `element_id` for attachments                                   |
| `required`, `optional`          | attributes with type, unit, accepted values, default and mined description |
| `set_automatically`             | managed and derived attributes                                          |
| `connection` / `attaches_to`    | the connection attributes, or the accepted target types                 |
| `table_attribute`               | the name and columns of the table attribute                             |
| `rules`, `derived`              | the constraints checked and the values computed                         |
| `checked`                       | present on schema-only types                                            |

Rejections carry the part of the descriptor that would have avoided them, as the `hint` of each entry of `errors`:
the accepted attributes for an unknown name, the full descriptor for a missing required attribute, the accepted
values for a bad enumeration, the columns for a bad row, the connection attributes when one is missing, the accepted
target types for a wrong attachment, the supported types for an unknown type.

##### The replies

The three mutation tools answer in JSON, adapted from the design note's proposal, with the **same keys whatever the
outcome**, and a one-line `message` for a human or a model to read:

| Key | `create_network_element` | `create_network_elements` | `remove_network_elements` |
|-----|--------------------------|---------------------------|---------------------------|
| `success` | the element is in the network as requested | every item was applied | every requested id was removed |
| `network_id` | ✓ | ✓ | ✓ |
| ids | `created_ids` (a new element), `updated_ids` (limits, a tap changer… added to an existing one), `created_count` | the same, over the batch, plus `not_created_count` and `stopped_at` | `removed_ids`, `removed_count` |
| details | `element_type`, `methods` (pypowsybl calls), `connection_points`, `notes` | `items`: one per item, in the order applied | `items`: one per id, in the order processed |
| `errors` | `[{problem, hint}]` | `[{item, element_type, id, problem, hint}]` | `[{id, problem}]` |
| `message`, `loadflow_invalidated` | ✓ | ✓ | ✓ |

A batch item has `item` (its position in the request, from 1), `element_type`, `id`, `status` (`created`,
`updated`, `failed` or `not_attempted`), `methods`, `message`, `connection_points`, `notes`, `problem` and `hint`. A
removal item has `id`, `element_type`, `status` (`removed`, `refused`, `not_found` or `failed`), `methods`,
`message`, `problem` and `cascade` (below). `loadflow_invalidated` is false exactly when nothing can have changed.

---

#### 11. Batches

`create_network_elements(items)` takes one object per element: the attributes of `create_network_element` plus
`element_type`.

**Before anything is created**, every item goes through the checks that do not need the network: item shape,
element type, attribute names, id, required attributes, types, accepted values and table rows. A mistake there
rejects the whole batch and the network is untouched.

**Then the items are sorted** by profile order, keeping the given order among equals, and created one by one:

| Order | Types                                                                 |
|-------|-----------------------------------------------------------------------|
| 10    | substation                                                            |
| 20    | voltage level                                                         |
| 30    | schema-only types (buses, busbar sections, switches, DC nodes, …)     |
| 40    | injections                                                            |
| 45    | lines, two-windings transformers                                      |
| 47    | HVDC lines (after their converter stations), tie lines (after their boundary lines) |
| 60    | limits, reactive limits, tap changers                                 |

When the pre-check rejects items, `errors` lists **all** of them at once, `items` is empty and nothing is created.

The checks that need the network (an id already taken, a connection point that does not exist) run as each element
is created. A failure there **stops the batch**: the elements created before it stay, `items` lists them as `created`
or `updated`, then the failed one with its `problem` and `hint`, then the rest as `not_attempted`; `stopped_at` is
the failed item. There is no rollback, which would mean copying the whole
network; `duplicate_session()` beforehand is the way to keep an untouched copy.

---

#### 12. Cached load-flow results

A creation or removal that changes the network clears the session's cached load-flow results for that network, so
nothing computed on the previous structure is reported afterwards. A rejected call keeps them: a `CreationError`
means nothing reached pypowsybl, and a removal whose ids were all refused or not found changed nothing. A pypowsybl
error, or a batch that stopped half way, counts as a change, since part of it may have been applied.

---

#### 13. Removal

`remove_network_elements(element_ids, cascade=False)` needs only ids: the type of each element is read from the
network and the matching pypowsybl removal is applied.

| Element                                                     | Removal                                           |
|-------------------------------------------------------------|---------------------------------------------------|
| loads, generators, batteries, lines, transformers, shunts, SVCs, boundary lines, converter stations | `remove_feeder_bays`: the element **and** the breakers and disconnectors that attached it |
| HVDC line                                                   | `remove_hvdc_lines`, with its converter stations   |
| voltage level                                               | `remove_voltage_levels`, needs `cascade=True`      |
| substation                                                  | its voltage levels, then the substation; needs `cascade=True` if it holds any |
| anything else (switches, buses, busbar sections, …)         | `Network.remove_elements`                          |
| the network itself                                          | refused: `remove_network()` drops a network from the session |

Ids may mix types and be given in any order: feeders are removed first, then voltage levels, then substations, so a
cascade never deletes an id still to be processed. Each id gets one item in the reply, with a `status` of `removed`,
`refused`, `not_found` or `failed`; an unknown id does not abort the others.

##### The cascade gate

Removing a voltage level removes far more than its id names: everything it contains, every branch with an end in it,
the HVDC lines whose converter station is in it **with the station at their far end**, and the tie lines whose
boundary line is in it. Without `cascade=True` the item is `refused`, and says what would go, the equipment inside
counted by type and what lies beyond listed by id — as text in `problem`, and as data in `cascade` (also given, for
the record, on a voltage level or substation actually removed):

```json
{
  "id": "S1VL2",
  "element_type": "voltage_level",
  "status": "refused",
  "methods": [],
  "problem": "voltage level 'S1VL2' not removed: removing it also removes 2 busbar section(s), 3 generator(s), 2 hvdc converter station(s), 3 load(s), 1 shunt compensator(s), 33 switch(es); and beyond it: 2 hvdc converter station(s) (LCC2, VSC2), 2 hvdc line(s) (HVDC1, HVDC2), 1 two windings transformer(s) (TWT). Call again with cascade=True to confirm",
  "cascade": {
    "inside": {"busbar_section": 2, "generator": 3, "hvdc_converter_station": 2, "load": 3, "shunt_compensator": 1},
    "switches": 33,
    "beyond": {"hvdc_converter_station": ["LCC2", "VSC2"], "hvdc_line": ["HVDC1", "HVDC2"],
               "two_windings_transformer": ["TWT"]}
  }
}
```

The summary scans every element table for the voltage-level columns of each end, then follows HVDC lines and tie
lines. A test compares it with what `remove_voltage_levels` actually removes, for every voltage level of five sample
networks. Switches are counted inside the voltage level only: the bays at the far end of its branches go too.

Removal is not undoable, and limits or tap changers have no removal of their own (removing their id would remove
the line or transformer).

---

#### 14. Staying in step with pypowsybl

The schema follows the installed version by construction; the profiles do not. Three mechanisms keep them honest.

**Name checks** (`PROFILE_CHECKS` in `drift.py`). Twelve checks verify that every name a profile uses still exists
in the installed pypowsybl: the element type is creatable, the executor and documentation calls exist, the
required, managed, defaulted, enumerated and rule attributes are fields of the schema, every derivation produces a
real field, no virtual or table attribute has become a pypowsybl field, the id is still the index, the connection
attributes are present. `tests/plugins/grid_extension/creation/test_profiles_consistency.py` runs them for every profile.

**Coverage tables and snapshot.** `scripts/creation_coverage.py --write` regenerates the coverage tables of
`docs/grid_extension.md` and `pypowsybl_mcp/plugins/grid_extension/creation/coverage_snapshot.json`, which ships with the package: the pypowsybl
version, the group of every type and the attributes of each. `--check`, run by `test_documented_coverage.py`, fails
whenever either no longer matches, including after every version bump.

**Startup check.** When the creation tools are registered, before any request, `startup_check()` runs the name checks
and compares the installed version with the snapshot:

- a profile that fails a check is **disabled**: removed from the profiles, its type falling back to schema-only
  creation, with an `ERROR` log naming the problem;
- a version change, or a coverage that moved, is logged as a `WARNING` with the types that changed group and the
  number whose attributes changed;
- otherwise, one `INFO` line: "Creation profiles verified against pypowsybl 1.15.0".

The report is served on `GET /admin/creation-coverage`, protected by `MCP_AUTH_TOKEN` (see
[monitoring.md](monitoring.md)).

These checks only see names. When upgrading pypowsybl, still review by hand:

1. the enumerations in `profiles.py` — a new value is refused, a removed one accepted until pypowsybl rejects it (1.15
   has no `OFF` mode for an SVC and no `FIXED_TAP` mode for a phase shifter, so both use `regulating: false`);
2. newly required attributes, which nothing flags before a call fails;
3. new bay helpers or network modifications — a bay for `ground` or `three_windings_transformer` would let them move
   to a bay executor;
4. the docstring format the mining relies on;
5. the identifiable type names in `FEEDER_TYPES` (`removal_tools.py`);
6. then run the tests, `scripts/creation_coverage.py --write`, and read the diff of `docs/grid_extension.md`.

---

#### 15. Limitations

- **No client-side validation.** `attributes` is an open object, so MCP clients cannot check it against a schema;
  everything is validated by the server, and a model learns the attributes from the cheat sheet, the descriptor or a
  rejection.
- **No rollback.** A batch stopped half way keeps what it created; removal is final.
- **No dry run.** There is no `validate_only` mode: a call that passes every check is applied.
- **Schema-only types** get no switching equipment, no required attributes, no accepted values, no id check, and only
  their main dataframe. `area_boundaries`, created at order 30, comes before any line of the same batch.
- **Default limits group only.** Operational limits go in the default group; a named group is not the active one.
- **Name checks are not semantic checks** (§14): enumeration values and required attributes can drift silently.
- **Knowledge beyond the metadata** — that `x` must not be zero, that a transformer stays in one substation, that a
  branch without limits can never be overloaded — exists only where a profile carries it in a rule or its guidance.

---

#### 16. Adding or improving a profile

1. Add a `CreationProfile` with `_register(...)` in `profiles.py`: pick the executor and connection kind, list the
   required attributes, the managed ones, defaults, units, enumerations, rules and derivations, and write the summary
   and guidance. Name calls as strings.
2. Run `uv run pytest tests/plugins/grid_extension`: the consistency tests flag any name the profile gets wrong.
3. Add engine tests on a real network (`tests/plugins/grid_extension/creation/test_engine.py`), ideally one that runs a load flow on the
   result.
4. Run `uv run python scripts/creation_coverage.py --write`: the type moves from "schema only" to
   "profiled" in `docs/grid_extension.md` and in the snapshot.
5. If the type deserves it, extend the cheat sheet in `creation_tools.py` and the `grid-extension` skill.

---

#### 17. Tests

| File                                         | Covers                                                                     |
|----------------------------------------------|----------------------------------------------------------------------------|
| `tests/plugins/grid_extension/creation/test_schema.py`              | the introspected schema and docstring mining                               |
| `tests/plugins/grid_extension/creation/test_engine.py`              | the pipeline against real networks: every executor, rules, rows, batches, schema-only types, load flows on the result |
| `tests/plugins/grid_extension/creation/test_profiles_consistency.py`| the name checks for every profile, design invariants, the schema-only registry |
| `tests/plugins/grid_extension/creation/test_documented_coverage.py` | the generated tables and the shipped snapshot                              |
| `tests/plugins/grid_extension/creation/test_drift.py`               | the startup check: verified, upgrade simulated, profiles disabled, missing snapshot |
| `tests/plugins/grid_extension/test_creation_tools.py` | the MCP tools: descriptors, rejections, load-flow cache, end-to-end site creation |
| `tests/plugins/grid_extension/test_removal_tools.py` | removal per type, the cascade gate, the cascade summary against real removals |
| `tests/plugins/grid_extension/test_plugin.py` | the entry points, the disable switch, the skill, `/admin/creation-coverage` |
