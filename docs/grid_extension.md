### Grid extension plugin

A plugin shipped with [pypowsybl-mcp](../README.md) that **creates and removes network elements**: substations, voltage levels,
loads, generators, lines, transformers, limits, tap changers and the rest of what pypowsybl can create, with the
switching equipment they need. It serves long-term studies — connecting a datacenter, a plant or a new site,
developing the grid — and has no use in others, such as real-time studies, which is why it is a plugin rather than a
set of built-in tools.

[element_creation_and_removal.md](element_creation_and_removal.md) explains how it works.

---

#### Activation

The plugin is a module of the server, `pypowsybl_mcp/plugins/grid_extension/`, loaded through the standard mechanism of
[plugins.md](plugins.md): `pypowsybl-mcp` declares the entry point `grid_extension` in both `pypowsybl_mcp.plugins.v1`
(tools) and `pypowsybl_mcp.resources.v1` (skill).

- **Active by default.** Installing the server installs it, and the server loads it at startup:
  `Loaded plugin 'grid_extension'.`
- **Turned off** without uninstalling it, for a deployment that never builds equipment:

  ```bash
  MCP_DISABLED_PLUGINS=grid_extension python -m pypowsybl_mcp.server
  ```

  The tools, the skill and the admin route below then all disappear, keeping the tool list a model sees smaller.

Once loaded, it adds:

| What                                   | Name                                                                                         |
|----------------------------------------|----------------------------------------------------------------------------------------------|
| Tools                                  | `describe_element_creation`, `create_network_element`, `create_network_elements`, `remove_network_elements` |
| Skill (resource and prompt)            | `grid-extension` (`skills://skills/grid-extension`)                                          |
| Admin route (token-protected)          | `GET /admin/creation-coverage` (see [monitoring.md](monitoring.md))               |

---

#### Tools

##### Creation

| Tool                        | Description                                                                                            |
|-----------------------------|----------------------------------------------------------------------------------------------------------|
| `describe_element_creation` | List the element types that can be created, or describe the attributes, rules and units of one of them. |
| `create_network_element`    | Create one element of any type from `element_type` and an `attributes` object.                          |
| `create_network_elements`   | Create several elements in one call, ordered so that containers come before what they contain.          |

One generic tool creates every element type. The attributes a type takes are read from the
installed `pypowsybl` (its own creation metadata), and a small declarative profile per type adds what that metadata does
not say: which attributes are required, their units and accepted values, the conditional rules, and which pypowsybl call
is legal for that type. Injections and branches go through the *bay* helpers, so the switching equipment the hosting
topology requires is built automatically and the caller only ever names a bus or busbar section.

The creatable types that have no profile yet are not refused: they are created **from their schema only**, through the
plain `Network.create_*` call. Attribute names and types are checked, everything else is left to pypowsybl, and no
switching equipment is built; `describe_element_creation` marks them `checked: schema only`. The tables at the end of
this page list which types fall in each group.

Since the arguments are an open object rather than a typed signature, three things replace the per-tool JSON schema: a
cheat sheet of the most-used types inlined in the tool description, `describe_element_creation` for everything else, and
**errors that carry the descriptor** — a rejected call comes back with the required attributes, units and accepted
values of that element type, so it can be corrected without a further lookup.
[element_creation_and_removal.md](element_creation_and_removal.md) explains how creation and removal work.

##### Removal

| Tool                      | Description                                                                                            |
|---------------------------|----------------------------------------------------------------------------------------------------------|
| `remove_network_elements` | Remove any elements by id: feeders go with their bays, voltage levels and substations cascade (opt-in). |

Removal is generic for the same reason creation is: an id is all it takes, the type is read from the network and the
matching pypowsybl removal is applied. Removing a voltage level or a substation requires `cascade=True`, since it also
deletes everything they contain; without it the tool reports what *would* be removed, including what lies beyond the
voltage level (branches, HVDC links with their far-end converter station, tie lines). Unlike `set_line_status`, a
removed element no longer exists in the network at all.

---

#### Coverage of the pypowsybl creation API

The tables below are generated by `scripts/creation_coverage.py --write`, which also records them in
`pypowsybl_mcp/plugins/grid_extension/creation/coverage_snapshot.json`. The server compares the installed pypowsybl with that
snapshot at startup and disables any profile that no longer matches; the report is on `/admin/creation-coverage`.

<!-- begin generated creation coverage -->

Generated from the profiles and from pypowsybl `1.15.0` by `scripts/creation_coverage.py`.

**18 element types have a profile**: required attributes, units, accepted values and rules are checked, and injections and branches are created with their switching equipment.

| `element_type` | pypowsybl call behind it | What it is |
|----------------|--------------------------|------------|
| `battery` | `pp.network.create_battery_bay` | A storage unit: produces when target_p is positive, absorbs when negative. |
| `generator` | `pp.network.create_generator_bay` | A production unit: a plant, a wind or solar farm. |
| `ground` | `Network.create_grounds` | An earthing connection on a bus. |
| `hvdc_line` | `Network.create_hvdc_lines` | A DC link between two converter stations that already exist. |
| `lcc_converter_station` | `pp.network.create_lcc_converter_station_bay` | The AC end of a line-commutated HVDC link. |
| `line` | `pp.network.create_line_bays` | An AC line between two connection points, with a bay at each end. |
| `load` | `pp.network.create_load_bay` | A consumption point: a datacenter, a factory, an extra demand. |
| `minmax_reactive_limits` | `Network.create_minmax_reactive_limits` | A constant reactive capability range for a generator, battery or converter. |
| `operational_limits` | `Network.create_operational_limits` | The current (or power) ratings of a branch, without which it can never be overloaded. |
| `phase_tap_changer` | `Network.create_phase_tap_changers` | Active-power control on a two-windings transformer (phase shifter). |
| `ratio_tap_changer` | `Network.create_ratio_tap_changers` | Voltage regulation on a two-windings transformer (on-load tap changer). |
| `reactive_capability_curve_point` | `Network.create_curve_reactive_limits` | A reactive capability curve Q(P), as a list of points. |
| `shunt_compensator` | `pp.network.create_shunt_compensator_bay` | A capacitor bank (positive susceptance) or a reactor (negative). |
| `static_var_compensator` | `pp.network.create_static_var_compensator_bay` | Continuous reactive control between b_min and b_max. |
| `substation` | `Network.create_substations` | A geographical site holding voltage levels; no equipment of its own. |
| `two_windings_transformer` | `pp.network.create_2_windings_transformer_bays` | A transformer between two voltage levels of one substation. |
| `voltage_level` | `Network.create_voltage_levels`, `pp.network.create_voltage_level_topology` | The busbar system of a site at one nominal voltage, created with the connection points equipment attaches to. |
| `vsc_converter_station` | `pp.network.create_vsc_converter_station_bay` | The AC end of a voltage-source HVDC link. |

**14 element types have no profile and are created from their schema only**: attribute names and types are checked, the rest is left to pypowsybl, and no switching equipment is built.

| `element_type` | pypowsybl call behind it | What it is |
|----------------|--------------------------|------------|
| `area` | `Network.create_areas` | Create areas. |
| `area_boundaries` | `Network.create_areas_boundaries` | Define boundaries of (existing) areas. |
| `area_voltage_levels` | `Network.create_areas_voltage_levels` | Associate voltage levels to (existing) areas. |
| `boundary_line` | `Network.create_boundary_lines` | Creates boundary lines. |
| `bus` | `Network.create_buses` | Creates buses in bus-breaker voltage levels. |
| `busbar_section` | `Network.create_busbar_sections` | Creates bus bar sections. |
| `dc_ground` | `Network.create_dc_grounds` | Creates DC grounds. |
| `dc_line` | `Network.create_dc_lines` | Creates DC lines. |
| `dc_node` | `Network.create_dc_nodes` | Creates DC nodes. |
| `internal_connection` | `Network.create_internal_connections` | Creates internal connections. |
| `switch` | `Network.create_switches` | Creates switches. |
| `three_windings_transformer` | `Network.create_3_windings_transformers` | Creates three-winding transformers. |
| `tie_line` | `Network.create_tie_lines` | Creates tie lines from two boundary lines. |
| `voltage_source_converter` | `Network.create_voltage_source_converters` | Creates voltage source converter. |

**1 element type expose creation metadata but no `Network.create_*` call**, so `create_network_element` does not support them yet:

```
alias
```

Giving a schema-only type a profile is an entry in `pypowsybl_mcp/plugins/grid_extension/creation/profiles.py`, not a new tool; the profile then takes over from the fallback.

<!-- end generated creation coverage -->

---

#### Development

```bash
uv sync --all-extras                                    # installs the server, editable, with its entry points
uv run pytest tests/plugins/grid_extension              # the plugin's tests
uv run python scripts/creation_coverage.py --write      # after changing a profile or pypowsybl
```
