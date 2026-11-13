### Upgrading pypowsybl

`pypowsybl-mcp` pins one exact pypowsybl version (`pypowsybl==1.15.0` in `pyproject.toml`), and pypowsybl bundles one
exact powsybl-core version (7.2.1 for pypowsybl 1.15.0; `python -c "import pypowsybl; pypowsybl.print_version()"`
lists them). Moving to another version is a deliberate change. This page says what checks itself, what still needs
a person, and in which order to do it.

---

#### 1. What checks itself

Most of what an upgrade can break is caught without anyone remembering to look:

| Check | Where it runs | What it catches |
|-------|---------------|-----------------|
| The test suite (`uv run pytest`) | locally, CI | Anything the server does on real networks: creation and removal on bus/breaker and node/breaker grids (with load flows), sensitivity analyses on IEEE 14, the element-type map derived from pypowsybl's getters |
| Name checks of the creation profiles (`tests/plugins/grid_extension/creation/test_profiles_consistency.py`) | locally, CI, **and server startup** | An attribute, a creation call, a bay helper or a documentation call a profile relies on, renamed or removed upstream. At startup such a profile is disabled, its type falling back to schema-only creation |
| Name checks of the topology operations (`tests/plugins/grid_extension/creation/test_topology.py`) | locally, CI, **and server startup** | An attribute or a call a topology operation relies on, renamed or removed upstream; a new topology modification. At startup such an operation is no longer offered |
| Coverage snapshot (`tests/plugins/grid_extension/creation/test_documented_coverage.py`) | locally, CI | **Fails on every version change, by design**, until the coverage is reviewed and recorded (step 5 below) |
| Coverage comparison | **server startup** | The element types, attributes and bay helpers that changed since the recorded version, logged and served on `/admin/creation-coverage` |
| Accepted values, offline (`tests/plugins/grid_extension/creation/test_enum_values.py`) | locally, CI, **and server startup** after a version change | A value a profile offers (an energy source, a regulation mode...) that pypowsybl no longer accepts. At startup it is dropped from the profile |
| Accepted values, against powsybl-core (`scripts/creation_enums.py --check`) | CI, Docker image build, **and server startup** in the background (unless `MCP_CREATION_ONLINE_ENUM_CHECK=false`) | A value pypowsybl **added** or deprecated, a value removed, a workaround no longer needed. Reads the powsybl-core sources of the bundled version from GitHub |

A broken creation profile therefore never fails in front of a user: the startup check sets it aside and says so. That
is a safety net for deployments, not a substitute for the review below.

---

#### 2. What still needs a person

##### Element creation and removal (the grid extension)

1. **What `scripts/creation_enums.py` reports.** For each value pypowsybl added: offer it, by adding it to the
   profile's `enums` in `pypowsybl_mcp/plugins/grid_extension/creation/profiles.py`, or record it in `LEFT_OUT` in
   `creation/enum_check.py` with the reason it is not offered. For a workaround no longer needed (an SVC `OFF` mode,
   a phase shifter `FIXED_TAP` mode), use the new value in the profile, its guidance and the `grid-extension` skill,
   and remove it from `WORKAROUNDS`.
2. **Newly required attributes.** pypowsybl's metadata never says what is required, so a field that became mandatory
   is noticed only when a creation fails. Read the release notes, and add it to the profile's `required`.
3. **New bay helpers.** The startup report and the snapshot list them (`bays`). A type whose profile connects to a
   bus or busbar section picks its bay up by itself. `ground`, whose profile connects to a configured bus, needs its
   `connection` changed to use one; `three_windings_transformer`, created from its schema only, needs a profile.
4. **Removal.** The identifiable type names in `FEEDER_TYPES` (`removal_tools.py`), and the cascade rules of
   `_cascade`, which mirror what `remove_voltage_levels` removes; a test compares them on five sample networks only.
   **Topology operations.** A topology modification new upstream fails
   `test_every_pypowsybl_topology_modification_is_offered` (`creation/test_topology.py`): give it a `TopologyOperation`
   in `creation/topology.py`, or exclude it in `schema._topology_calls` with the reason. The checks of each operation
   encode what pypowsybl did not check on 1.15 (a position over 100 %, a busbar at another voltage, the orphan
   switches of a revert): reread them against the release notes, a fix upstream makes one redundant, not wrong.
5. **Docstring mining.** It relies on pypowsybl's `- **name**: description` docstring format; if that changes, the
   descriptions vanish from `describe_element_creation` without an error. The duplicate-`target_v` workaround in
   `doc_mining.py` may no longer be needed.
6. **The examples of `creation/enum_check.py`.** They use ids of pypowsybl's four-substations sample network; if
   pypowsybl changes that network, the enum tests fail and the ids need updating. Likewise `MODULES`, where
   powsybl-core keeps each enum's source: if a file moves, the enum script reports it as not found.

##### The rest of the server

7. **Load flow parameters.** `pypowsybl_mcp/config/LF_default_parameters.toml` names pypowsybl parameters and enum
   values (`voltage_init_mode`, `balance_type`...), and `update_loadflow_params` names more. The load flow tests mock
   pypowsybl, so check these against the new version by hand.
8. **Security analysis, I/O and visualization.** Their tests also mostly mock pypowsybl. Smoke-test them on the
   running server: a security analysis, loading and exporting a network, a single-line and a network-area diagram
   (`pypowsybl_mcp/config/visualization_default_parameters.toml` names diagram parameters).
9. **Online documentation.** `get_online_resource` reads pypowsybl's documentation at `latest`
   (`URL_BASE` in `tools/utils/resources.py`), not at the pinned version, and `KNOWN_NAMING_PITFALLS` corrects some
   of its pages: check they still hold.
10. **Generated scripts.** The prompts of `generate_python_script` (`llm_utils/agents/code_generation.py`) cite
    pypowsybl calls.

---

#### 3. Procedure

```bash
git switch -c chore/pypowsybl-X.Y                       # see AGENTS.md for branch names
# set pypowsybl==X.Y.Z in pyproject.toml, then:
uv lock --upgrade-package pypowsybl && uv sync --all-extras

uv run pytest                                           # 1. what breaks (test_documented_coverage always does)
uv run python -c "import json; from pypowsybl_mcp.plugins.grid_extension.creation import drift; \
print(json.dumps(drift.startup_check(apply=False), indent=2))"   # 2. the startup report, without a server
uv run python scripts/creation_enums.py                 # 3. accepted values vs powsybl-core
# 4. the review of section 2, and the fixes it calls for
uv run python scripts/creation_coverage.py --write      # 5. record the new coverage, then read its diff:
git diff docs/grid_extension.md pypowsybl_mcp/plugins/grid_extension/creation/coverage_snapshot.json
uv run pytest && uv run ruff check . && uv run python scripts/creation_enums.py --check   # 6. all green
```

Step 5 comes last on purpose: the snapshot records the coverage someone reviewed. Recording it before the review
would make the startup check compare pypowsybl with itself.

---

#### 4. Text that states the version

Generated text follows by itself (the coverage tables of [grid_extension.md](grid_extension.md) and the snapshot,
through step 5). These are written by hand and should be reread:

- the counts of [element_creation_and_removal.md](element_creation_and_removal.md) §3, its 1.15 examples (§4, §15)
  and its `LEFT_OUT` list (§15);
- the workaround comments in `profiles.py` (no `OFF` mode for an SVC, no `FIXED_TAP` mode for a phase shifter);
- the version limits in the `grid-extension` skill (`pypowsybl_mcp/plugins/grid_extension/skills/grid-extension.md`);
- the measurements in the `doc_mining.py` docstring;
- the examples of [monitoring.md](monitoring.md), which show version numbers.
