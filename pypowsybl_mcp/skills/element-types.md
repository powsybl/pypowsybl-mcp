---
name: element-types
description: How to name network element types in PyPowsybl MCP tools (element_type argument of get_network_element_data, create_contingencies_list, run_security_analysis). Use when you need to translate everyday grid vocabulary ("transformer", "SVC", "busbar") into the canonical name a tool expects, or when a tool answered "Invalid element type".
---

# Skill: Naming network element types

Several tools take an `element_type` argument: `get_network_element_data`
(including its `get_only_ids=True` mode, which returns just the element IDs),
`create_contingencies_list` and the `auto_contingencies` filter of
`run_security_analysis`.

## The one rule

**An element type is named exactly like the pypowsybl `ElementType` enum member
that it maps to, lowercased.** Names are singular.

Note the getter that returns the table is often *plural* and does not match the
name character-for-character — the mapping is maintained for you:

| element_type                 | pypowsybl getter                  | ElementType enum              |
| ---------------------------- | --------------------------------- | ----------------------------- |
| `line`                       | `get_lines()`                     | `LINE`                        |
| `two_windings_transformer`   | `get_2_windings_transformers()`   | `TWO_WINDINGS_TRANSFORMER`    |
| `static_var_compensator`     | `get_static_var_compensators()`   | `STATIC_VAR_COMPENSATOR`      |
| `busbar_section`             | `get_busbar_sections()`           | `BUSBAR_SECTION`              |

There are no aliases, no abbreviations and no plurals: `transformers`,
`transformer`, `svc`, `2wt`, `xfmr`, `lines` and `bus_bars` are **not** valid
values. The accepted names follow the installed pypowsybl version, so anything
the network object can return as a table is available — including `branch`,
`injection`, `identifiable`, `terminal`, `area`, `battery`, `tie_line`,
`boundary_line`, `selected_operational_limits`, `ratio_tap_changer`,
`phase_tap_changer` and the `dc_*` tables.

Use `get_online_resource(class_object='network')` (see the `remote-resource`
skill) to list the getters of the pypowsybl version actually running, and hence
the element types it supports.

## Everyday wording to canonical name

Grid operators rarely speak in enum names. Translate before calling:

| The user says                              | element_type to use                                        |
| ------------------------------------------ | ---------------------------------------------------------- |
| transformer, transfo, TR, two-winding      | `two_windings_transformer` — **"transformer" alone always means the two-winding one** |
| three-winding transformer, autotransformer with tertiary | `three_windings_transformer`                  |
| PST, phase shifter, phase-shifting transformer | `two_windings_transformer` (tap details in `phase_tap_changer`) |
| SVC, static var compensator, reactive compensator | `static_var_compensator`                            |
| shunt, capacitor bank, reactor             | `shunt_compensator`                                        |
| dangling line                              | `boundary_line` (renamed upstream in pypowsybl 1.15)       |
| busbar, bar                                | `busbar_section`                                           |
| node, bus, electrical node                 | `bus` (topology view: `bus_from_bus_breaker_view`)         |
| HVDC link, DC link                         | `hvdc_line`                                                |
| converter station                          | `vsc_converter_station` or `lcc_converter_station`         |
| branch (line *or* transformer)             | `branch`, or query `line` and `two_windings_transformer` separately |
| substation vs. voltage level               | `substation` (site) vs. `voltage_level` (one voltage inside it) |

When a request mixes lines and transformers ("show me every overloaded
branch"), either query `branch` once, or run the tool twice — once with `line`,
once with `two_windings_transformer` — and merge the results.

## Types that accept a subset

`create_contingencies_list` and `run_security_analysis(auto_contingencies=...)`
build N-1 contingencies, which only makes sense for a few types: `line`,
`generator`, `two_windings_transformer`, `hvdc_line`.

## When a call is rejected

An unknown name comes back as `Invalid element type '<name>'` followed by a
"Did you mean …?" suggestion and the full list of accepted names. Read the
suggestion and retry with a canonical name; do not invent variants.
