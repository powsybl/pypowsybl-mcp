---
name: remote-resource
description: Fetch official pypowsybl API documentation on demand through the PyPowsybl MCP server. Use when you need details about a pypowsybl module or method (network, loadflow, security, sensitivity, rao, ...) — first to discover which methods a module exposes, then to read the detailed documentation of a specific method.
---

# Skill: Remote Resource Documentation Fetcher

This skill allows fetching and using documentation for `pypowsybl` classes and methods directly from the official online documentation. Documentation is returned as markdown directly in the tool responses, and cached server-side so it can be re-read cheaply.

## When to use this (call trigger)

**Call `get_online_resource` whenever you have a pypowsybl API question** — any
time you need a method's exact name, signature, parameters, accepted values,
return type, or behaviour, and any time you are about to write or generate
pypowsybl code. **Do not answer such questions from prior knowledge.** The
pypowsybl API changes across versions and your recollection may be outdated,
incomplete, or wrong. This documentation is the single source of truth: consult
it first, then answer.

This is a distinct concern from the *action* tools (`create_ieee_network`,
`run_loadflow`, `modify_network`, `get_network_element_data`, …), which
*operate on* a loaded network. When the user wants something *done* to a
network, use those tools. When you need to *know how the pypowsybl API works*,
use this skill.

Two tools are involved, both returning the markdown in a `content` field of their JSON response:

- `get_online_resource(class_object, method_name)` — **download** a page from the online documentation, cache it, and return its markdown. Use this for any page you have not yet fetched this session.
- `read_resource(resource_id)` — return a page **already fetched this session**, instantly and with no download. Returns `{"success": false, ...}` when the page has not been fetched yet or has expired.

You do not need to read any `resources://...` URI yourself: the markdown is always in the `content` field of the tool response.

## Resource identifiers

Each cached page has an id (also exposed as a `resources://temp/{id}` URI):

- `{class_object}` — module overview page listing all methods (fetched with an empty `method_name`).
- `{class_object}-{method_name}` — detailed documentation of one method.

Where:
- `{class_object}` is one of the supported `pypowsybl` modules.
- `{method_name}` is the specific method name. This is either a **module-level
  function** (e.g. `create_empty`, documented at
  `pypowsybl.network.create_empty`) or a **method on a class** such as `Network`
  (e.g. `Network.disconnect`, documented at
  `pypowsybl.network.Network.disconnect`). Pass the class-qualified form —
  `method_name="Network.disconnect"` — to fetch a class method.

## Naming pitfalls (verify behaviour, not name)

Some pypowsybl names are misleading: the verb does not describe what the method
operates on, or a name reads as a module-level function when it is really a
method on a class. **Rely on the fetched signature, parameters and description,
never on what the name implies.** When a fetched page starts with a
`> **Naming note ...**` banner, read that banner first.

Known traps (fetch the full doc before using):

| `method_name` you pass | What it actually does |
|---|---|
| `Network.disconnect` | Opens the switches isolating ONE element (line/generator/...) by id. It does **not** tear down the Network object. Class method. |
| `Network.connect` | Re-closes the switches to reconnect ONE element by id. It does **not** connect the Network to anything. Class method. |

If a method's behaviour ever surprises you, fetch its full documentation and
re-read it before acting.

`read_resource` accepts either the bare id (`network-create_empty`) or the full URI (`resources://temp/network-create_empty`).

## Supported Class Objects

- `network`
- `loadflow`
- `rao`
- `security`
- `sensitivity`
- `flowdecomposition`
- `dynamic`
- `shortcircuit`
- `voltage_initializer`

## How to Retrieve and Use a Resource

The rule is simple: **fetch with `get_online_resource`; use `read_resource`
only to re-read a page you already fetched this session.** Do not open with a
`read_resource` call on a page you have never fetched — it will only miss.

### Step 1: Fetch with `get_online_resource`

#### Substep 1a: Discover methods
If you don't know the exact method name, call `get_online_resource` with the
`method_name` parameter empty:
- `class_object`: e.g., `"network"`
- `method_name`: `""` (empty string)

This downloads the overview of all methods for the class, caches it under id
`{class_object}`, and returns its markdown in `content`.

#### Substep 1b: Fetch specific method documentation
Once you have identified the method from the class page, call
`get_online_resource` again with both parameters:
- `class_object`: e.g., `"network"`
- `method_name`: e.g., `"create_empty"`

This downloads the detailed documentation for that method, caches it under id
`{class_object}-{method_name}`, and returns its markdown in `content`.

### Step 2: Re-read from the cache with `read_resource` (optional)
If, later in the same session, you need a page you already fetched, call
`read_resource` to get it back instantly without re-downloading:
- module page: `read_resource("{class_object}")`
- method page: `read_resource("{class_object}-{method_name}")`

If it returns `{"success": false, ...}` (not fetched yet or expired), fetch it
with `get_online_resource`.

### Step 3: Use the documentation
Read the markdown from the `content` field of whichever tool returned
`success: true`. There is no separate resource-read step: `read_resource` and
`get_online_resource` both hand you the content directly.
