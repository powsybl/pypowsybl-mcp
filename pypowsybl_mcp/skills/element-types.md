---
name: element-types
description: How to name network element types in PyPowsybl MCP tools (element_type argument of get_network_element_data, get_network_elements_ids, create_contingencies_list, run_security_analysis). Use when you need to translate everyday grid vocabulary ("transformer", "SVC", "busbar") into the canonical name a tool expects, or when a tool answered "Invalid element type".
---

# Skill: Naming network element types

Several tools take an `element_type` argument: `get_network_element_data`,
`get_network_elements_ids`, `create_contingencies_list` and the
`auto_contingencies` filter of `run_security_analysis`.

## The one rule

**An element type is named exactly like the pypowsybl `Network` getter that
returns its table, minus the `get_` prefix.**

| pypowsybl getter                  | element_type                |
| --------------------------------- | --------------------------- |
| `get_lines()`                     | `lines`                     |
| `get_2_windings_transformers()`   | `2_windings_transformers`   |
| `get_static_var_compensators()`   | `static_var_compensators`   |
| `get_busbar_sections()`           | `busbar_sections`           |

There are no aliases, no abbreviations and no shorter synonyms: `transformers`,
`svc`, `2wt`, `xfmr` and `bus_bars` are **not** valid values. The accepted names
follow the installed pypowsybl version, so anything the network object can
return as a table is available — including `branches`, `injections`,
`identifiables`, `terminals`, `areas`, `batteries`, `tie_lines`,
`boundary_lines`, `operational_limits`, `ratio_tap_changers`,
`phase_tap_changers` and the `dc_*` tables.

Use `get_online_resource(class_object='network')` (see the `remote-resource`
skill) to list the getters of the pypowsybl version actually running, and hence
the element types it supports.

## Everyday wording to canonical name

Grid operators rarely speak in getter names. Translate before calling:

| The user says                              | element_type to use                                        |
| ------------------------------------------ | ---------------------------------------------------------- |
| transformer, transfo, TR, two-winding      | `2_windings_transformers` — **"transformer" alone always means the two-winding one** |
| three-winding transformer, autotransformer with tertiary | `3_windings_transformers`                     |
| PST, phase shifter, phase-shifting transformer | `2_windings_transformers` (tap details in `phase_tap_changers`) |
| SVC, static var compensator, reactive compensator | `static_var_compensators`                           |
| shunt, capacitor bank, reactor             | `shunt_compensators`                                       |
| dangling line                              | `boundary_lines` (renamed upstream in pypowsybl 1.15)      |
| busbar, bar                                | `busbar_sections`                                          |
| node, bus, electrical node                 | `buses` (topology view: `bus_breaker_view_buses`)          |
| HVDC link, DC link                         | `hvdc_lines`                                               |
| converter station                          | `vsc_converter_stations` or `lcc_converter_stations`        |
| branch (line *or* transformer)             | `branches`, or query `lines` and `2_windings_transformers` separately |
| substation vs. voltage level               | `substations` (site) vs. `voltage_levels` (one voltage inside it) |

When a request mixes lines and transformers ("show me every overloaded
branch"), either query `branches` once, or run the tool twice — once with
`lines`, once with `2_windings_transformers` — and merge the results.

## Types that accept a subset

`create_contingencies_list` and `run_security_analysis(auto_contingencies=...)`
build N-1 contingencies, which only makes sense for a few types: `lines`,
`generators`, `2_windings_transformers`, `hvdc_lines`.

## When a call is rejected

An unknown name comes back as `Invalid element type '<name>'` followed by a
"Did you mean …?" suggestion and the full list of accepted names. Read the
suggestion and retry with a canonical name; do not invent variants.
