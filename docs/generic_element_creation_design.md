### Design: a generic, introspection-driven creation tool

This document describes a small
generic tool set that discovers its parameters from `pypowsybl` itself for elements creation. It states what
introspection can and cannot supply (verified against `pypowsybl` **1.15.0**, the pinned
version), the architecture that follows from that, and the advantages and limitations of the
approach — in particular what happens when a new `pypowsybl` version adds, renames or removes
creation functions.

---

#### 1. The problem

The current surface is one MCP tool per element type: `create_substation`,
`create_voltage_level`, `create_load`, `create_generator`, `create_line`, `create_transformer`,
`create_battery`, `create_shunt_compensator`, `create_static_var_compensator`, `create_ground`,
`create_operational_limits`, `create_reactive_limits`, `create_ratio_tap_changer`,
`create_phase_tap_changer` — 14 tools, ~2 500 lines, out of 58 tools on the server.

Three costs follow from that shape:

- **Every new element type is a new tool.** Three-windings transformers, HVDC links, converter
  stations, boundary and tie lines, DC nodes are all missing today, and each one is a new tool
  plus its docstring, tests and documentation row.
- **Repetition.** The same pipeline (resolve network, check the id is free, resolve the
  connection point, derive the feeder position, invalidate the load flow, format the report) is
  written 14 times, and a fix to one of them has to be replayed in the others.
- **Context budget.** 14 verbose tool descriptions sit in the model's context permanently,
  whether or not the conversation is about building anything.

And building one site — substation, two voltage levels, transformer, load, line, limits — costs
six to eight round trips.

---

#### 2. What `pypowsybl` introspection provides

Two C-API calls carry the field list, and both follow the installed version automatically.

**Plain creation metadata** — the attributes of `Network.create_<x>()`:

```python
from pypowsybl import _pypowsybl as _pp

_pp.get_network_elements_creation_dataframes_metadata(_pp.ElementType.LOAD)
# → [[id*, voltage_level_id, bus_id, connectable_bus_id, node, name, type, p0, q0]]
```

**Bay creation metadata** — the attributes of the `pypowsybl.network.create_<x>_bay()` helpers,
which are what the tools actually call so that switching equipment is built for the hosting
topology:

```python
_pp.get_network_modification_metadata_with_element_type(
    _pp.NetworkModificationType.CREATE_FEEDER_BAY, _pp.ElementType.LOAD
)
# → [[id*, voltage_level_id, bus_id, connectable_bus_id, node, name, type, p0, q0,
#     bus_or_busbar_section_id, position_order, direction, feeder_type]]
```

Both return **groups** of series, which handles the multi-dataframe cases: a shunt compensator
comes back as three groups (the shunt, the linear section model, the non-linear model), a tap
changer as two (the changer and its steps).

Other `NetworkModificationType` members are introspectable the same way and cover the rest of
the creation surface: `CREATE_LINE_FEEDER`, `CREATE_TWO_WINDINGS_TRANSFORMER_FEEDER`,
`VOLTAGE_LEVEL_TOPOLOGY_CREATION`, `CREATE_COUPLING_DEVICE`, `CREATE_LINE_ON_LINE`,
`CONNECT_VOLTAGE_LEVEL_ON_LINE`.

Each field is a `SeriesMetadata` with `name`, `type` (0 = str, 1 = float, 2 = int, 3 = bool),
`is_index`, `is_modifiable`, `is_default`.

**33 element types expose creation metadata in 1.15.0**, which is the natural upper bound of
what a generic tool could reach:

```
ALIAS, AREA, AREA_BOUNDARIES, AREA_VOLTAGE_LEVELS, BATTERY, BOUNDARY_LINE, BUS,
BUSBAR_SECTION, DC_GROUND, DC_LINE, DC_NODE, GENERATOR, GROUND, HVDC_LINE,
INTERNAL_CONNECTION, LCC_CONVERTER_STATION, LINE, LOAD, MINMAX_REACTIVE_LIMITS,
OPERATIONAL_LIMITS, PHASE_TAP_CHANGER, RATIO_TAP_CHANGER, REACTIVE_CAPABILITY_CURVE_POINT,
SHUNT_COMPENSATOR, STATIC_VAR_COMPENSATOR, SUBSTATION, SWITCH, THREE_WINDINGS_TRANSFORMER,
TIE_LINE, TWO_WINDINGS_TRANSFORMER, VOLTAGE_LEVEL, VOLTAGE_SOURCE_CONVERTER,
VSC_CONVERTER_STATION
```

Element-type **names** need no invention: the repo already derives them from `ElementType`
lowercased (`pypowsybl_mcp/utils/element_types.py`), which is the vocabulary of
`get_network_element_data`, `create_contingencies_list` and the `element-types` skill. The same
names, and the same `element_type_hint()` "did you mean", are reused here.

---

#### 3. What introspection does *not* provide

Four gaps, and they are the reason a purely introspective tool would be worse than what exists
today rather than better.

1. **Required versus optional is not exposed.** On creation metadata `is_modifiable` and
   `is_default` are `False` for every field of every type. Nothing distinguishes `p0` (a load is
   meaningless without it) from `name` (decoration). A tool built on metadata alone would accept
   `{"id": "X"}` and let powsybl raise a Java-level error.
2. **No semantics.** No units (`p0` in MW, `target_v` in kV, `b_per_section` in siemens), no
   legal enum values (`energy_source`, `regulation_mode`, `topology_kind`, `direction`), no
   conditional requirements (`target_v` is required only when `voltage_regulator_on` is true;
   `target_q` only when it is false), no sane defaults, no guidance on plausible magnitudes.
3. **No mapping to a callable.** Metadata says *what* a load bay takes, not that the call is
   `pypowsybl.network.create_load_bay(network, **attrs)` while a shunt needs
   `create_shunt_compensator_bay(network, shunt_df=..., linear_model_df=...)` with real
   DataFrames, and a substation needs `network.create_substations(**attrs)` with no bay at all.
4. **The metadata is permissive, not authoritative.** `CREATE_FEEDER_BAY` metadata is returned
   for *every* element type, including ones that can have no bay — asking for a substation
   feeder bay yields a perfectly well-formed field list:

   ```
   SUBSTATION → id, name, country, tso, TSO, bus_or_busbar_section_id, position_order,
                direction, feeder_type
   ```

   Introspection would therefore invite the model to create a "substation connected to a busbar
   section". Something outside the metadata has to say which executor is legal for which type.
   (The same call also exposes raw artifacts such as the duplicate `tso` / `TSO` pair, which a
   caller should never see.)

**Conclusion driving the design: introspection supplies the *shape*, a small declarative overlay
supplies the *semantics*, and one engine supplies the *behaviour*.**

---

#### 4. Architecture

##### Layer 1 — introspected schema (no maintenance)

At import, build one `ElementSchema` per element type from the metadata calls above: field
names, python types, index field, group structure. Nothing is hand-written here, so this layer
tracks the installed `pypowsybl` version by construction.

##### Layer 2 — creation profile (hand-written, ~20 lines per type)

Only what metadata cannot carry:

```python
@dataclass(frozen=True)
class CreationProfile:
    executor: Executor  # how to call pypowsybl for this type
    required: tuple[str, ...]  # beyond id and the connection point
    derived: tuple[str, ...]  # filled by the engine, never asked of the caller
    defaults: Mapping[str, Any]
    units: Mapping[str, str]
    enums: Mapping[str, tuple[str, ...]]
    rules: tuple[Rule, ...]  # conditional requirements, cross-field checks
    guidance: str  # one paragraph: what it is for, how to pick values
```

```python
PROFILES["load"] = CreationProfile(
    executor=FeederBay(pp.network.create_load_bay),
    required=("p0",),
    derived=("node", "bus_id", "connectable_bus_id", "feeder_type", "position_order"),
    defaults={"q0": 0.0},
    units={"p0": "MW", "q0": "MVAr"},
    enums={"type": ("UNDEFINED", "AUXILIARY", "FICTITIOUS")},
    guidance="A consumption point: a datacenter, a factory, an extra demand at an existing "
    "bus. q0 around 0.2*p0 corresponds to a 0.98 power factor.",
)

PROFILES["generator"] = CreationProfile(
    executor=FeederBay(pp.network.create_generator_bay),
    required=("target_p", "max_p"),
    derived=("node", "bus_id", "connectable_bus_id", "feeder_type", "position_order"),
    defaults={"min_p": 0.0, "voltage_regulator_on": False},
    units={
        "target_p": "MW",
        "max_p": "MW",
        "min_p": "MW",
        "target_v": "kV",
        "target_q": "MVAr",
        "rated_s": "MVA",
    },
    enums={"energy_source": ("HYDRO", "NUCLEAR", "WIND", "THERMAL", "SOLAR", "OTHER")},
    rules=(
        RequiredIf("target_v", when="voltage_regulator_on"),
        DefaultIfNot("target_q", 0.0, when="voltage_regulator_on"),
        Ordered("min_p", "target_p", "max_p"),
    ),
    guidance="A production unit. target_v is in kV, not per-unit, and should be close to the "
    "nominal voltage of the hosting voltage level.",
)
```

Executors are a closed set, one class each:

| Executor | Underlying call | Types |
|---|---|---|
| `FeederBay(fn)` | `create_<x>_bay(network, **attrs)` | load, generator, battery, shunt compensator, SVC, converter stations |
| `LineBays()` | `create_line_bays` | line |
| `TransformerBays()` | `create_2_windings_transformer_bays` | two-windings transformer |
| `Raw(method)` | `Network.create_<x>s(**attrs)` | substation, ground, bus, busbar section, switch, HVDC line, DC devices |
| `Composite(...)` | several calls in order | voltage level (`create_voltage_levels` + `create_voltage_level_topology`) |
| `MultiFrame(...)` | one call, several DataFrames | shunt (linear model), ratio/phase tap changer (steps) |
| `Attachment(method)` | attaches to an existing element | operational limits, reactive limits |

##### Where the profile content comes from: mining the docstrings

Profiles do not have to be written from a blank page. `pypowsybl` documents its creation
attributes in the docstrings themselves, in a regular format:

```
Valid attributes are:

- **id**: the identifier of the new load
- **p0**: active power load, in MW
- **q0**: reactive power load, in MVar
- **direction**: optionally, in node/breaker, the direction of the load, ... default is BOTTOM.
```

This is the same text that `resources.py` serves through `get_online_resource`: the
readthedocs pages it fetches are Sphinx autodoc output of these docstrings. Two consequences
worth being precise about:

- For **generating profiles**, read the docstrings locally with `inspect.getdoc`. It is offline,
  needs no HTTP or SSL configuration, and — decisively — it is the *installed* version.
  `URL_BASE` in `resources.py` points at `.../en/latest/`, so the online page may document a
  different version than the pinned one, which is precisely the mismatch a profile must not
  inherit.
- For **runtime**, `get_online_resource` keeps its role: it is the escape hatch that
  `describe_element_creation` can point at for a type that has no profile yet, and it carries
  the narrative pages that docstrings do not have. The `remote-resource` skill already teaches
  that path.

**What mining actually yields.** Measured over 270 creation fields across 22 element types on
1.15.0, taking the correct metadata variant per type (`CREATE_FEEDER_BAY`, `CREATE_LINE_FEEDER`,
`CREATE_TWO_WINDINGS_TRANSFORMER_FEEDER` or plain creation metadata) and following the
"same attributes as `Network.create_x`" cross-references:

| Extracted | Coverage | Verdict |
|---|---|---|
| Field description | **87 %** of fields (235/270) | Good enough to seed `guidance` and per-field help |
| Unit (`in MW`, `in kV`, `in Ohm`, …) | 26 % of described fields | Covers the numeric fields that matter (`target_p`, `target_v`, `r`, `x`); the rest is manual |
| Marked "optional" | 14 % of described fields | **Not** a usable signal for required-ness: `name` is optional and rarely marked, `target_v` is conditionally required and never marked |
| Enumerated values | 3 % of described fields | Unusable: they are truncated in the source, e.g. `energy_source` is documented as `(HYDRO, NUCLEAR, ...)` |
| Default value | 2 % of described fields | Anecdotal |

So docstrings supply the *prose* almost completely and the *semantics* barely at all. The three
things a profile most needs — which fields are required, the legal enum values, the conditional
rules — are the three the docstrings do not carry.

**The consequence for the design**: a build-time generator, not a runtime authority.

```
scripts/generate_profiles.py
    introspection (field names, types, groups)
  + docstring mining (description, unit where stated, default where stated)
  → draft profile module, with required/enums/rules left as TODO markers
  → a human curates it once per element type; the result is committed
```

Re-running the generator after a `pypowsybl` upgrade produces a diff that shows exactly what
changed upstream — new fields, renamed fields, reworded descriptions — as a reviewable git diff
rather than as a surprise at call time. The curated parts (required, enums, rules, guidance) are
preserved across regenerations by merging on field name.

The precedent for the curated half already exists in `resources.py`: `KNOWN_NAMING_PITFALLS`
attaches a hand-written correction to a specific documentation page, on the grounds that the
upstream text is misleading and the correction has to reach the agent at the point of reading.
A profile's `rules` and `guidance` are the same idea applied to creation.

##### Layer 3 — the engine (one pipeline, written once)

```
resolve network
  → normalise attribute names (aliases: bus → bus_or_busbar_section_id, load_type → type, …)
  → reject unknown attributes (with the schema in the error)
  → check the id is free across all identifiables
  → resolve the connection point(s), reusing the existing resolver and its
    bus-view-versus-configured-bus diagnostics
  → apply defaults
  → run derivations (position order from existing positions, rated_u1/u2 from the ends'
    nominal voltages, min_p = -max_p for a battery, connectable_bus from bus)
  → run rules (conditional requirements, ordering, ranges)
  → dispatch to the executor
  → invalidate cached load-flow results
  → report
```

Everything the 14 tools validate today lives here once, expressed as data instead of as 14
copies of the same `if`.

##### The consistency test

The overlay refers to field names that come from `pypowsybl`. A test walks every profile and
asserts that each name in `required`, `derived`, `units`, `enums` and every `rule` exists in the
introspected schema for that type, and that every executor attribute exists on the installed
`pypowsybl`. **A field renamed or removed upstream turns into a red test at CI time rather than
a runtime error in front of a user.** This test is what makes "generic plus introspection"
honest rather than a source of silent drift.

---

#### 5. Tool surface

Three tools replace fourteen.

```
describe_element_creation(element_type=None)
create_network_element(element_type, attributes, network_id=None)
create_network_elements(items, network_id=None)
```

- **`describe_element_creation`** is the discovery tool and replaces the per-tool JSON schema.
  For one type it returns the fields split into required / optional / derived, with units, enum
  values, defaults, the connection style and the guidance paragraph, assembled from layers 1
  and 2. Called with no argument it lists the creatable types with a one-line summary each.
- **`create_network_element`** creates one element.
- **`create_network_elements`** creates several in one call: the engine orders them
  (containers before what they contain, elements before the limits and tap changers attached to
  them) and rolls the batch back if any item fails. A whole datacenter site becomes one call
  instead of six.

Removal already has this shape — `remove_network_elements` takes ids of any type, reads the type
from the network and picks the matching removal — so the surface becomes symmetric and the
vocabulary uniform: `create_network_element(s)` / `remove_network_elements` / `modify_network`.

##### How the model learns the parameters without a static schema

Three mechanisms, in decreasing order of how often they should be needed:

1. **A cheat sheet in the tool's own docstring.** The minimal field set of the six most-used
   types (load, generator, line, transformer, substation, voltage level) is inlined in
   `create_network_element`'s description, which is always in context. The common path costs no
   round trip.
2. **The error carries the schema.** Any rejection — missing required field, unknown attribute,
   bad enum value, failed rule — returns the relevant slice of the descriptor along with the
   message. A wrong call corrects itself in one round trip instead of a guessing loop.
3. **`describe_element_creation`** for anything outside the cheat sheet.

##### Worked example

```jsonc
// one call builds the site
create_network_elements(items=[
  {"element_type": "substation",     "id": "SUB_DC", "country": "FR"},
  {"element_type": "voltage_level",  "id": "VL_135", "substation_id": "SUB_DC",
                                     "nominal_v": 135.0, "low_voltage_limit": 128.0,
                                     "high_voltage_limit": 145.0},
  {"element_type": "voltage_level",  "id": "VL_63",  "substation_id": "SUB_DC",
                                     "nominal_v": 63.0},
  {"element_type": "two_windings_transformer", "id": "TR",
                                     "bus_or_busbar_section_id_1": "VL_135_1_1",
                                     "bus_or_busbar_section_id_2": "VL_63_1_1",
                                     "r": 0.2, "x": 10.0, "rated_s": 400.0},
  {"element_type": "load",           "id": "DATACENTER",
                                     "bus_or_busbar_section_id": "VL_63_1_1",
                                     "p0": 100.0, "q0": 20.0},
  {"element_type": "line",           "id": "LINE_B4_DC",
                                     "bus_or_busbar_section_id_1": "B4",
                                     "bus_or_busbar_section_id_2": "VL_135_1_1",
                                     "r": 0.5, "x": 5.0},
  {"element_type": "operational_limits", "element_id": "LINE_B4_DC",
                                     "permanent_limit": 800.0},
])
```

`rated_u1` / `rated_u2` are derived from the two ends' nominal voltages, `position_order` from
the existing positions, `connectable_bus_id` from the bus — none of them are asked for.

---

#### 6. Advantages

##### Maintainability against new `pypowsybl` versions

This is the strongest argument for the design. What a version upgrade costs, for each kind of
upstream change:

| Upstream change | One tool per type (today) | Generic design |
|---|---|---|
| **New optional field** on an existing type (say `condenser` on generator) | Invisible until someone reads the release notes, adds a parameter, a docstring line and a test | Appears immediately in `describe_element_creation` and is accepted by the engine. Add a unit or enum entry only if it needs one |
| **New required field** | Silent runtime failure at the first call | Same failure, but the message carries the introspected schema showing the new field; a profile line makes it explicit |
| **Field renamed or removed** | The tool keeps passing the old keyword; breaks at call time, in front of a user | The consistency test fails at CI, naming the profile and the field |
| **New element type** (a new DC device, say) | A new tool: ~150 lines plus docstring, tests, docs row | Appears in the type list automatically; becomes creatable with a ~20-line profile. No new tool, no new docs row |
| **New bay helper upstream** | A new tool | One `executor=` line in the profile |
| **A call is renamed** (1.16 replaces `create_operational_limits` with `create_loading_limits`) | The tool breaks at call time | One line in the profile; the executor-attribute check fails at import/CI until it is changed |
| **A type disappears** | Dead tool nobody notices | The profile fails its consistency test |
| **Anything upstream changes at all** | Read the release notes and hope | Re-run the profile generator: the diff lists every added, renamed and reworded field |

In short: **additive upstream changes are free, breaking ones become CI failures instead of user-facing errors, and new element types cost a table entry instead of a tool.**

##### Other advantages

- **Coverage becomes cheap.** The gaps documented in `tools_reference.md` — three-windings
  transformers, HVDC links and converter stations, boundary and tie lines, DC nodes and lines,
  busbar sections and switches, coupling devices, line tapping — are a profile each. The
  "not available" list shrinks to what `pypowsybl` itself cannot do.
- **One place for behaviour.** Loadflow invalidation, id checks, connection-point diagnostics,
  feeder-position derivation, report formatting: fixed once, applied to every type. Today a fix
  has to be replayed 14 times, and nothing enforces that it is.
- **Batch creation.** Ordering and rollback across a whole site is expressible only when one
  call sees the whole list.
- **Smaller permanent context.** Three tool descriptions instead of fourteen; the detail is
  fetched on demand for the type actually being created.
- **Generated documentation.** The coverage table in `tools_reference.md` is hand-written today
  and can drift. From profiles plus introspection it can be generated, so "what is and is not
  supported" is always true of the installed version.
- **A path for `modify_network`.** On *element dataframe* metadata (as opposed to creation
  metadata) `is_modifiable` **is** populated — 17 modifiable fields on a generator. The same
  engine would let `modify_network` cover every type and every modifiable field with no overlay
  at all, since an update has no required fields beyond the id. It supports 3 types and 2
  parameters each today.

---

#### 7. Limitations

##### Inherent to the approach

- **The MCP client stops validating arguments.** With `attributes` as a free-form object, the
  required fields, types and enums are no longer in the tool's JSON schema, so nothing rejects a
  malformed call before it reaches the server. Validation moves entirely to runtime. The three
  discovery mechanisms mitigate this; they do not remove it.
- **Errors replace schemas as the teaching signal.** A model that gets it wrong learns from the
  error, which costs a round trip and depends on the model actually reading the message. A typed
  signature teaches before the call; this design teaches after it. Expect slightly more
  back-and-forth on unusual element types, and design the error text as carefully as a docstring.
- **Tool listings become uninformative to humans.** A user browsing the tool list in an MCP
  client sees "create a network element" instead of fourteen self-describing entries. The
  documentation and the `grid-extension` skill have to carry that weight.
- **Mining does not remove the manual step.** Docstrings give descriptions and some units;
  required-ness, enum values and conditional rules stay hand-written and hand-verified (see the
  measurements above). A generator that filled those in by guessing would produce confident,
  wrong schemas — worse than leaving them blank.
- **The overlay is real work and cannot be skipped.** "Introspection-driven" is only half true:
  every executable type needs its ~20 lines. The saving is real (~150 lines and a tool per type
  becomes ~20 lines and a table entry) but this is not a zero-maintenance design, and a profile
  written carelessly produces a tool that is *worse* than a typed one because nothing else
  documents the type.
- **Metadata permissiveness must be gated by hand.** As shown above, `pypowsybl` will describe a
  substation feeder bay. The set of legal executors per type is knowledge that lives only in the
  overlay; if a profile is wrong, the engine will confidently build nonsense.
- **Multi-DataFrame types do not fit a flat dictionary.** Shunt compensators (shunt + section
  model) and tap changers (changer + steps) need nested attributes, e.g.
  `{"steps": [{"rho": 0.95}, …]}`. That is an extra shape the caller has to get right, and the
  generated steps of the current tap-changer tools (±10 % over 17 positions) are a convenience
  that has to be re-expressed as a derivation rather than as a signature default.
- **Some knowledge is neither in the metadata nor mechanical.** That `x` must not be zero, that
  an overhead line at 400 kV is roughly 0.3 Ω/km, that a transformer must stay inside one
  substation, that a created branch is invisible to overload analysis until it is rated — this
  is physics and powsybl semantics. It survives only if the profiles carry it in `rules` and
  `guidance`, and it is the part most likely to be lost in a rewrite.

##### Bounded by `pypowsybl` itself

- Nothing here creates a network from scratch (`create_empty`), nor extends the set of things
  `pypowsybl` can build. Types with no creation metadata stay out of reach.
- The multi-group metadata does not say which group is required or mutually exclusive — the
  shunt's linear and non-linear models come back as two peer groups with no indication that
  exactly one must be given.
- Type codes distinguish str/float/int/bool only. Enumerations arrive as plain strings, so their
  legal values will always come from the overlay, and the overlay is version-specific: 1.15 has
  no `OFF` regulation mode for an SVC and no `FIXED_TAP` mode for a phase tap changer, and
  nothing in the metadata says so.

##### Residual risk

The single biggest risk is that the overlay silently disagrees with reality — a `required` list
that is too short, a missing rule, an enum value that no longer exists. The consistency test
catches *name* drift. It cannot catch *semantic* drift, which stays the responsibility of the
end-to-end tests that build a real network and run a load flow, as the current test suite does.

---

#### 8. Decisions taken

1. **Scope of the first profile set.** Eighteen element types, the fourteen the per-type surface
   covered plus `vsc_converter_station`, `lcc_converter_station`, `hvdc_line` and
   `reactive_capability_curve_point` — the last four cost about twenty lines each, which is the
   point. The fifteen creatable types with no profile are listed in `docs/tools_reference.md`.
2. **Sugar tools.** None: three generic tools, no typed shortcuts. The cheat sheet inlined in
   `create_network_element`'s description covers the common types without a round trip.
3. **Batch semantics.** Neither of the two options as stated. Every item is checked against its
   schema *before* anything is created, so a typo leaves the network untouched; the checks that
   need the network (an id already taken, a connection point an earlier item was supposed to
   create) can only run as each element is created, so they stop the batch there and the report
   says how far it got. A true rollback would mean deep-copying the network, which is not worth
   its cost on a real grid — `duplicate_session()` is the honest undo.
4. **Profile generation.** No code generation. Descriptions and units are mined from the
   docstrings **at runtime**, inside `describe_element_creation`, so they always match the
   installed pypowsybl and there is no generated file to go stale. The properties the generator
   was wanted for are delivered by two other means: `tests/creation/test_profiles_consistency.py`
   fails when a profile names an attribute or a callable pypowsybl no longer has, and
   `scripts/creation_coverage.py --check` (run as a test) fails when the documented coverage
   stops matching the profiles and the installed version.
5. **Attribute naming.** Raw pypowsybl names, with a short alias table for the everyday ones
   (`bus` → `bus_or_busbar_section_id`, `bus1`/`bus2`, `element_id` for the target of an
   attachment). Keeping the raw names means the descriptor, the documentation and the mined
   docstrings all speak the same vocabulary.

---

#### 9. What the implementation added to this design

Three things the proposal did not foresee, each forced by something found while building it:

* **Virtual attributes.** Some calls are unusable when expressed exactly as pypowsybl shapes
  them: the steps of a tap changer are a table of `rho` values, and a limits group is one row per
  (side, duration) with the permanent rating carrying `acceptable_duration: -1`. Profiles may
  therefore declare *virtual* attributes — `step_count` and `range_percent`, `permanent_limit`
  and `temporary_limits` — which a derivation turns into the real rows. A test asserts that a
  virtual name is never also a pypowsybl field, so if upstream ever provides one the profile has
  to stop faking it.
* **A `report_extra` hook on executors.** Creating a voltage level generates the ids of its
  connection points, and every following creation needs them; the executor reports them.
* **`Connection.BUS`.** `ground` has no bay helper upstream, so it takes a configured bus and is
  refused in node/breaker topology — the case that proves the executor, not the metadata, decides
  what is legal.

And one limitation of the pinned version worth repeating, because it is visible in the profiles:
a static var compensator has no `OFF` regulation mode and a phase tap changer no `FIXED_TAP`
mode, so both are created with `regulating: false` instead.
