#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""The creation profiles, checked against the pypowsybl actually installed.

Profiles are written, and the coverage documented, against one pypowsybl
version; the server may well run on another. Two things guard against that:

``coverage_snapshot.json``
    What the creation tools covered on the version they were checked against:
    which element types have a profile, which are schema-only, which are not
    supported, and the attributes of each. Written next to this module by
    ``scripts/creation_coverage.py --write``, so it ships with the package.

:func:`startup_check`
    Run once when the creation tools are registered. It compares the installed
    version with the snapshot and reports what changed, and it runs the name
    checks of :data:`PROFILE_CHECKS` on every profile: a profile naming an
    attribute or a call pypowsybl no longer has is set aside, its type falling
    back to schema-only, instead of failing in front of a user. The report is
    logged and served on ``/admin/creation-coverage``.

The checks only see *names*. A changed enumeration or a newly required
attribute still needs the review described in ``docs/tools_reference.md``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pypowsybl as pp
from loguru import logger

from pypowsybl_mcp.creation.executors import resolve_call
from pypowsybl_mcp.creation.profiles import (
    PROFILES,
    UNPROFILED,
    Connection,
    CreationProfile,
    schema_only_profile,
)
from pypowsybl_mcp.creation.schema import (
    CREATION_METHODS,
    ElementSchema,
    creatable_element_types,
)

SNAPSHOT_PATH = Path(__file__).with_name("coverage_snapshot.json")

#: the report of the last startup check, served by /admin/creation-coverage
LAST_REPORT: dict[str, Any] | None = None

# ---------------------------------------------------------------------------
# Profile checks, shared with tests/creation/test_profiles_consistency.py
# ---------------------------------------------------------------------------


def _known_names(profile: CreationProfile, schema: ElementSchema) -> set[str]:
    """Attributes a profile may legitimately refer to."""
    names = set(schema.names) | set(profile.virtual)
    if profile.rows_attribute:
        names.add(profile.rows_attribute)
    return names


def _unknown(label: str, names, known: set[str]) -> list[str]:
    missing = sorted(set(names) - known)
    return [f"{label} unknown to pypowsybl: {', '.join(missing)}"] if missing else []


def _check_creatable(profile, schema):
    if profile.element_type in creatable_element_types():
        return []
    return ["pypowsybl no longer exposes creation metadata for this element type"]


def _check_executor_calls(profile, schema):
    missing = profile.executor.missing_call()
    return [f"{missing} no longer exists in pypowsybl"] if missing else []


def _check_documentation_calls(profile, schema):
    return [
        f"{call}, documenting its attributes, no longer exists in pypowsybl"
        for call in profile.docs
        if resolve_call(call) is None
    ]


def _check_required(profile, schema):
    return _unknown("required", profile.required, _known_names(profile, schema))


def _check_managed(profile, schema):
    return _unknown("managed", profile.managed, set(schema.names))


def _check_defaults_units_enums(profile, schema):
    known = _known_names(profile, schema)
    return [
        problem
        for label, names in (
            ("defaults", profile.defaults),
            ("units", profile.units),
            ("enums", profile.enums),
        )
        for problem in _unknown(label, names, known)
    ]


def _check_rules(profile, schema):
    known = _known_names(profile, schema)
    return [
        problem
        for item in profile.rules
        for problem in _unknown(f"rule {type(item).__name__}", item.fields, known)
    ]


def _check_derivations(profile, schema):
    known = _known_names(profile, schema)
    return [
        problem
        for derivation in profile.derivations
        for problem in _unknown(
            f"derivation {type(derivation).__name__}", [derivation.produces], known
        )
    ]


def _check_virtual_not_upstream(profile, schema):
    clashing = sorted(set(profile.virtual) & set(schema.names))
    if not clashing:
        return []
    return [
        (
            f"virtual attribute(s) {', '.join(clashing)} are now pypowsybl fields: "
            "drop them from 'virtual'"
        )
    ]


def _check_rows_attribute_not_upstream(profile, schema):
    if profile.rows_attribute and profile.rows_attribute in schema.names:
        return [f"table attribute '{profile.rows_attribute}' is now a pypowsybl field"]
    return []


def _check_id_is_index(profile, schema):
    if (
        schema.field(profile.id_field) is None
        or not schema.field(profile.id_field).is_index
    ):
        return [f"'{profile.id_field}' is no longer the index of the creation call"]
    return []


def _check_connection_attributes(profile, schema):
    names = set(schema.names)
    expected = {
        Connection.SINGLE: ["bus_or_busbar_section_id"],
        Connection.DOUBLE: ["bus_or_busbar_section_id_1", "bus_or_busbar_section_id_2"],
    }.get(profile.connection, [])
    return _unknown("connection attribute", expected, names)


#: every name check run on a profile, by name; each returns its problems
PROFILE_CHECKS: dict[str, Callable[[CreationProfile, ElementSchema], list[str]]] = {
    "creatable": _check_creatable,
    "executor_calls": _check_executor_calls,
    "documentation_calls": _check_documentation_calls,
    "required": _check_required,
    "managed": _check_managed,
    "defaults_units_enums": _check_defaults_units_enums,
    "rules": _check_rules,
    "derivations": _check_derivations,
    "virtual_not_upstream": _check_virtual_not_upstream,
    "rows_attribute_not_upstream": _check_rows_attribute_not_upstream,
    "id_is_index": _check_id_is_index,
    "connection_attributes": _check_connection_attributes,
}


def profile_problems(profile: CreationProfile) -> list[str]:
    """What, in `profile`, the installed pypowsybl no longer matches."""
    try:
        schema = profile.schema
    except Exception as error:  # noqa: BLE001 - any upstream change can surface here
        return [f"its pypowsybl schema cannot be read: {error}"]
    return [
        problem
        for check in PROFILE_CHECKS.values()
        for problem in check(profile, schema)
    ]


# ---------------------------------------------------------------------------
# Coverage snapshot
# ---------------------------------------------------------------------------


def coverage_snapshot(
    profiles: dict[str, CreationProfile] = PROFILES,
    unprofiled: dict[str, CreationProfile] = UNPROFILED,
) -> dict[str, Any]:
    """What the creation tools cover on the installed pypowsybl."""

    def attributes(registry: dict[str, CreationProfile]) -> dict[str, list[str]]:
        return {
            name: sorted(profile.schema.names)
            for name, profile in sorted(registry.items())
        }

    return {
        "pypowsybl_version": pp.__version__,
        "profiled": attributes(profiles),
        "schema_only": attributes(unprofiled),
        "unsupported": sorted(
            set(creatable_element_types()) - set(profiles) - set(unprofiled)
        ),
    }


def write_snapshot(path: Path = SNAPSHOT_PATH) -> None:
    path.write_text(json.dumps(coverage_snapshot(), indent=2) + "\n")


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        logger.warning(f"No usable creation coverage snapshot at {path}: {error}")
        return None


def _groups(snapshot: dict[str, Any]) -> dict[str, str]:
    """Element type -> "profiled", "schema_only" or "unsupported"."""
    groups = {name: "unsupported" for name in snapshot.get("unsupported", [])}
    for group in ("profiled", "schema_only"):
        groups.update(dict.fromkeys(snapshot.get(group, {}), group))
    return groups


def _changes(recorded: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Element types that changed group, and attributes that came or went."""
    before, after = _groups(recorded), _groups(current)
    moved = {
        name: {"was": before.get(name), "now": after.get(name)}
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }
    was = {**recorded.get("profiled", {}), **recorded.get("schema_only", {})}
    now = {**current["profiled"], **current["schema_only"]}
    attributes = {}
    for name in sorted(set(was) & set(now)):
        added = sorted(set(now[name]) - set(was[name]))
        removed = sorted(set(was[name]) - set(now[name]))
        if added or removed:
            attributes[name] = {"added": added, "removed": removed}
    return {"element_types": moved, "attributes": attributes}


# ---------------------------------------------------------------------------
# Startup check
# ---------------------------------------------------------------------------


def startup_check(
    profiles: dict[str, CreationProfile] = PROFILES,
    unprofiled: dict[str, CreationProfile] = UNPROFILED,
    snapshot_path: Path = SNAPSHOT_PATH,
    apply: bool = True,
) -> dict[str, Any]:
    """Check the profiles against the installed pypowsybl, and report.

    With ``apply`` (the startup case), a profile that no longer matches is
    removed from `profiles` and its type added to `unprofiled` when pypowsybl
    still has a ``create_*`` call for it, and the report is kept for
    ``/admin/creation-coverage``. It has to run before requests are served:
    it changes the registries every engine reads.
    """
    global LAST_REPORT

    broken = {
        name: problems
        for name, profile in sorted(profiles.items())
        if (problems := profile_problems(profile))
    }
    effective = {name: item for name, item in profiles.items() if name not in broken}
    fallback = dict(unprofiled)
    for name, problems in broken.items():
        method = CREATION_METHODS.get(name)
        if method is None:
            continue
        try:
            fallback[name] = schema_only_profile(name, method)
        except Exception as error:  # noqa: BLE001 - its schema may be gone too
            problems.append(f"no schema-only fallback either: {error}")

    recorded = load_snapshot(snapshot_path)
    current = coverage_snapshot(effective, fallback)
    changes = _changes(recorded, current) if recorded is not None else {}
    verified_against = recorded.get("pypowsybl_version") if recorded else None
    version_changed = recorded is not None and verified_against != pp.__version__

    if broken:
        status = "profiles disabled"
    elif recorded is None:
        status = "no snapshot"
    elif version_changed or changes["element_types"] or changes["attributes"]:
        status = "changed"
    else:
        status = "verified"

    report = {
        "status": status,
        "pypowsybl_version": pp.__version__,
        "verified_against": verified_against,
        "version_changed": version_changed,
        "element_types": {
            "profiled": len(current["profiled"]),
            "schema_only": len(current["schema_only"]),
            "unsupported": len(current["unsupported"]),
        },
        "changes": changes,
        "disabled_profiles": broken,
        "checked_at": datetime.now(tz=UTC).isoformat(),
    }

    if apply:
        for name in broken:
            profiles.pop(name, None)
        unprofiled.update(fallback)
        LAST_REPORT = report
        _log(report)
    return report


def _log(report: dict[str, Any]) -> None:
    version = report["pypowsybl_version"]
    for name, problems in report["disabled_profiles"].items():
        logger.error(
            f"Creation profile '{name}' disabled on pypowsybl {version}: "
            + "; ".join(problems)
        )
    changes = report["changes"]
    if report["status"] == "verified":
        logger.info(f"Creation profiles verified against pypowsybl {version}")
    elif report["verified_against"] is None:
        logger.warning(
            "Creation profiles could not be compared with a coverage snapshot; "
            "run scripts/creation_coverage.py --write"
        )
    elif report["version_changed"] or changes["element_types"] or changes["attributes"]:
        moved = [
            f"{name} ({move['was'] or 'absent'} -> {move['now'] or 'absent'})"
            for name, move in changes["element_types"].items()
        ]
        logger.warning(
            f"pypowsybl {version} is installed, the creation profiles were checked "
            f"against {report['verified_against']}. Element types changed: "
            f"{', '.join(moved) or 'none'}; attributes changed on "
            f"{len(changes['attributes'])} type(s). Details on "
            "/admin/creation-coverage; review the profiles, then run "
            "scripts/creation_coverage.py --write"
        )
