#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Import options of the loading tools: boundary sets, post-processors, parameters.

The CGMES tests load a real file: the MicroGrid BE network exported to CGMES
references two base voltages of the ENTSO-E boundary set, so it only loads when
a boundary set defines them - exactly the case the boundary options are for.
"""

import re
import shutil
import zipfile
from unittest.mock import MagicMock

import pypowsybl as pp
import pytest
from cachetools import TTLCache

from pypowsybl_mcp.tools.utils.import_options import (
    BOUNDARY_LOCATION_PARAMETER,
    BOUNDARY_SETS_DIR_ENV,
    CGMES_POST_PROCESSORS_PARAMETER,
    build_load_arguments,
    default_boundary_set_available,
    describe_import_options,
    explain_load_error,
    import_format_of,
    list_boundary_sets,
    resolve_boundary_set,
    ships_boundary,
)
from pypowsybl_mcp.tools.utils.io import IOTools

_RDF_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:cim="http://iec.ch/TC57/2013/CIM-schema-cim16#" '
    'xmlns:md="http://iec.ch/TC57/61970-552/ModelDescription/1#">\n'
)


def _boundary_profile(profile: str, model_id: str, body: str) -> str:
    return (
        _RDF_HEADER + f'  <md:FullModel rdf:about="urn:uuid:{model_id}">\n'
        "    <md:Model.scenarioTime>2014-06-01T00:00:00Z</md:Model.scenarioTime>\n"
        "    <md:Model.created>2014-06-01T00:00:00Z</md:Model.created>\n"
        "    <md:Model.version>1</md:Model.version>\n"
        f"    <md:Model.profile>http://entsoe.eu/CIM/{profile}/3/1</md:Model.profile>\n"
        "    <md:Model.modelingAuthoritySet>http://tscnet.eu/BD</md:Model.modelingAuthoritySet>\n"
        "  </md:FullModel>\n" + body + "</rdf:RDF>\n"
    )


@pytest.fixture(scope="module")
def cgmes_files(tmp_path_factory):
    """(CGMES zip without its boundary set, a boundary set zip defining it)."""
    root = tmp_path_factory.mktemp("cgmes")
    cgmes_zip = root / "micro_be.zip"
    network = pp.network.create_micro_grid_be_network()
    network.save(str(cgmes_zip), format="CGMES")

    # The base voltages the profiles reference but do not define come from
    # the boundary set.
    defined, referenced = set(), set()
    with zipfile.ZipFile(cgmes_zip) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode()
            defined |= set(re.findall(r'rdf:ID="_([^"]+)"', text))
            referenced |= set(re.findall(r'BaseVoltage rdf:resource="#_([^"]+)"', text))
            if name.endswith("_EQ.xml"):
                eq = text
    missing = referenced - defined
    assert missing, "the export no longer needs a boundary set"

    # Nominal voltage of each missing base voltage, from the voltage levels.
    vls = network.get_voltage_levels()
    nominal = {}
    for vl_id, body in re.findall(
        r'<cim:VoltageLevel rdf:ID="_([^"]+)">(.*?)</cim:VoltageLevel>', eq, re.DOTALL
    ):
        base = re.search(r'BaseVoltage rdf:resource="#_([^"]+)"', body).group(1)
        if base in missing and vl_id in vls.index:
            nominal[base] = vls.loc[vl_id, "nominal_v"]
    body = "".join(
        f'  <cim:BaseVoltage rdf:ID="_{base}">\n'
        f"    <cim:BaseVoltage.nominalVoltage>{value:g}</cim:BaseVoltage.nominalVoltage>\n"
        "  </cim:BaseVoltage>\n"
        for base, value in nominal.items()
    )
    boundary_zip = root / "boundary" / "BDS_test.zip"
    boundary_zip.parent.mkdir()
    with zipfile.ZipFile(boundary_zip, "w") as archive:
        archive.writestr(
            "20140601T0000Z__ENTSOE_EQBD_001.xml",
            _boundary_profile(
                "EquipmentBoundary", "2399cbd0-9a39-11e0-aa80-0800200c9a66", body
            ),
        )
        archive.writestr(
            "20140601T0000Z__ENTSOE_TPBD_001.xml",
            _boundary_profile(
                "TopologyBoundary", "2399cbd1-9a39-11e0-aa80-0800200c9a66", ""
            ),
        )
    return cgmes_zip, boundary_zip


@pytest.fixture
def boundary_sets_dir(cgmes_files, monkeypatch):
    _, boundary_zip = cgmes_files
    monkeypatch.setenv(BOUNDARY_SETS_DIR_ENV, str(boundary_zip.parent))
    return boundary_zip.parent


class _Ctx:
    def __init__(self):
        self.session = MagicMock()
        self.session.session_id = "import-options"
        self.request_id = "test"


@pytest.fixture
def io_tools():
    return IOTools(TTLCache(maxsize=10, ttl=3600))


# --- build_load_arguments ----------------------------------------------------


def test_no_option_gives_no_argument():
    args = build_load_arguments()
    assert args.parameters == {}
    assert args.post_processors == []
    assert args.summary() == {}


def test_post_processors_are_routed_by_kind():
    args = build_load_arguments(post_processors=["EntsoeCategory", "geoJsonImporter"])
    # A CGMES one is a parameter of the CGMES importer, a generic one is not
    assert args.parameters == {CGMES_POST_PROCESSORS_PARAMETER: "EntsoeCategory"}
    assert args.post_processors == ["geoJsonImporter"]
    assert args.summary()["post_processors"] == ["EntsoeCategory", "geoJsonImporter"]


def test_cgmes_post_processors_parameter_is_merged_with_the_list():
    args = build_load_arguments(
        parameters={CGMES_POST_PROCESSORS_PARAMETER: ["cgmesDLImport"]},
        post_processors=["EntsoeCategory", "cgmesDLImport"],
    )
    assert (
        args.parameters[CGMES_POST_PROCESSORS_PARAMETER]
        == "cgmesDLImport,EntsoeCategory"
    )


def test_parameter_values_become_strings():
    args = build_load_arguments(
        parameters={
            "iidm.import.cgmes.convert-boundary": True,
            "iidm.import.xml.missing-permanent-limit-percentage": 90,
            "iidm.import.xml.included.extensions": ["detail", "entsoeCategory"],
        }
    )
    assert args.parameters == {
        "iidm.import.cgmes.convert-boundary": "true",
        "iidm.import.xml.missing-permanent-limit-percentage": "90",
        "iidm.import.xml.included.extensions": "detail,entsoeCategory",
    }


def test_unknown_parameter_is_refused():
    # PowSyBl would silently ignore it and load the file without the option
    with pytest.raises(ValueError, match="Unknown import parameter"):
        build_load_arguments(parameters={"iidm.import.cgmes.boundary-locatio": "x"})


def test_boundary_location_cannot_be_set_directly():
    with pytest.raises(ValueError, match="cannot be set directly"):
        build_load_arguments(parameters={BOUNDARY_LOCATION_PARAMETER: "/etc"})


def test_unknown_post_processor_is_refused():
    with pytest.raises(ValueError, match="Unknown post-processor 'Nope'"):
        build_load_arguments(post_processors=["Nope"])
    with pytest.raises(ValueError, match="Unknown CGMES post-processor"):
        build_load_arguments(parameters={CGMES_POST_PROCESSORS_PARAMETER: "Nope"})


def test_boundary_location_is_used_but_not_reported():
    args = build_load_arguments(
        boundary_location="/srv/bds/x.zip", boundary="boundary_set 'x.zip'"
    )
    assert args.parameters == {BOUNDARY_LOCATION_PARAMETER: "/srv/bds/x.zip"}
    assert args.summary() == {"boundary": "boundary_set 'x.zip'"}


def test_missing_boundary_error_gets_a_hint():
    error = pp.PyPowsyblError("nominalVoltage not found for BaseVoltage 6389")
    assert "boundary set" in explain_load_error(error, boundary_given=False)
    # Not when a boundary set was given: it is then not the missing piece
    assert explain_load_error(error, boundary_given=True) == str(error)
    assert explain_load_error(ValueError("bad file"), False) == "bad file"


# --- boundary sets -----------------------------------------------------------


def test_no_boundary_sets_without_the_setting(monkeypatch):
    monkeypatch.delenv(BOUNDARY_SETS_DIR_ENV, raising=False)
    assert list_boundary_sets() == []
    with pytest.raises(ValueError, match="provides none"):
        resolve_boundary_set("anything")


def test_boundary_sets_are_directories_and_zips(tmp_path, monkeypatch):
    (tmp_path / "BDS_2025").mkdir()
    (tmp_path / "BDS_2024.zip").write_bytes(b"")
    (tmp_path / "EQBD.xml").write_text("")  # not a set: PowSyBl cannot read it
    (tmp_path / ".hidden").mkdir()
    monkeypatch.setenv(BOUNDARY_SETS_DIR_ENV, str(tmp_path))
    assert list_boundary_sets() == ["BDS_2024.zip", "BDS_2025"]
    assert resolve_boundary_set("BDS_2025") == str(tmp_path / "BDS_2025")


def test_boundary_set_name_cannot_escape_the_directory(boundary_sets_dir):
    for name in ("../boundary", "/etc", "..", ""):
        with pytest.raises(ValueError, match="Unknown boundary set"):
            resolve_boundary_set(name)


def test_describe_import_options(boundary_sets_dir):
    options = describe_import_options()
    assert "CGMES" in options["formats"]
    assert "EntsoeCategory" in options["post_processors"]["cgmes"]
    assert options["boundary_sets"] == ["BDS_test.zip"]
    assert "parameters" not in options

    cgmes = describe_import_options("cgmes")
    assert cgmes["format"] == "CGMES"
    names = [p["name"] for p in cgmes["parameters"]]
    assert CGMES_POST_PROCESSORS_PARAMETER in names
    # Set with boundary_set / boundary_url, never listed as a parameter
    assert BOUNDARY_LOCATION_PARAMETER not in names
    with pytest.raises(ValueError, match="Unknown import format"):
        describe_import_options("nope")


# --- real loads --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cgmes_without_boundary_fails_with_a_hint(io_tools, cgmes_files):
    cgmes_zip, _ = cgmes_files
    result = await io_tools.load_network_from_file(str(cgmes_zip), "be", ctx=_Ctx())
    assert result["status"] == "error"
    assert "boundary set" in result["message"]


@pytest.mark.asyncio
async def test_cgmes_with_a_server_boundary_set(
    io_tools, cgmes_files, boundary_sets_dir
):
    cgmes_zip, _ = cgmes_files
    result = await io_tools.load_network_from_file(
        str(cgmes_zip),
        "be",
        boundary_set="BDS_test.zip",
        post_processors=["EntsoeCategory"],
        ctx=_Ctx(),
    )
    assert result["status"] == "success", result
    assert result["source_format"] == "CGMES"
    assert result["post_processors"] == ["EntsoeCategory"]
    assert result["boundary"] == "boundary_set 'BDS_test.zip'"
    # The server path stays on the server
    assert str(boundary_sets_dir) not in str(result)


@pytest.mark.asyncio
async def test_cgmes_with_a_downloaded_boundary_set(io_tools, cgmes_files):
    cgmes_zip, boundary_zip = cgmes_files
    result = await io_tools.load_network_from_url(
        cgmes_zip.as_uri(),
        "be",
        boundary_url=boundary_zip.as_uri(),
        ctx=_Ctx(),
    )
    assert result["status"] == "success", result
    assert result["boundary"] == "boundary_url (BDS_test.zip)"
    assert "be" in io_tools.get_proxy("import-options").networks


@pytest.mark.asyncio
async def test_both_boundary_options_are_refused(io_tools, cgmes_files):
    cgmes_zip, boundary_zip = cgmes_files
    result = await io_tools.load_network_from_file(
        str(cgmes_zip),
        "be",
        boundary_set="BDS_test.zip",
        boundary_url=boundary_zip.as_uri(),
        ctx=_Ctx(),
    )
    assert result["status"] == "error"
    assert "not both" in result["message"]


@pytest.mark.asyncio
async def test_get_import_options_tool(io_tools):
    result = await io_tools.get_import_options("XIIDM")
    assert result["status"] == "success"
    assert result["format"] == "XIIDM"
    assert (await io_tools.get_import_options("nope"))["status"] == "error"


def _with_default_location(monkeypatch, location):
    import pypowsybl_mcp.tools.utils.import_options as module

    frame = module._format_parameters("CGMES").copy()
    frame.loc[BOUNDARY_LOCATION_PARAMETER, "default"] = location
    monkeypatch.setattr(module, "_format_parameters", lambda fmt: frame)


def test_default_boundary_set_is_reported_without_its_path(tmp_path, monkeypatch):
    location = tmp_path / "boundary"
    _with_default_location(monkeypatch, str(location))
    assert default_boundary_set_available() is False  # missing

    location.mkdir()
    assert default_boundary_set_available() is False  # empty

    (location / "BDS.zip").write_bytes(b"")
    assert default_boundary_set_available() is True
    options = describe_import_options()
    assert options["default_boundary_set"] is True
    assert str(location) not in str(options)


def test_default_boundary_set_as_an_archive(tmp_path, monkeypatch):
    archive = tmp_path / "BDS.zip"
    archive.write_bytes(b"")
    _with_default_location(monkeypatch, str(archive))
    assert default_boundary_set_available() is True


def test_default_boundary_set_ignores_files_powsybl_does_not_read(
    tmp_path, monkeypatch
):
    location = tmp_path / "boundary"
    location.mkdir()
    (location / "README.md").write_text("Put the ENTSO-E boundary set here.")
    _with_default_location(monkeypatch, str(location))
    assert default_boundary_set_available() is False

    (location / "EQBD.xml").write_text("<rdf:RDF/>")
    assert default_boundary_set_available() is True


# --- what PowSyBl ignores ----------------------------------------------------


@pytest.fixture(scope="module")
def cgmes_with_boundary(tmp_path_factory, cgmes_files):
    """The CGMES zip with the boundary profiles in it, under names that do not
    say "BD": PowSyBl tells a boundary file by its header profile."""
    cgmes_zip, boundary_zip = cgmes_files
    path = tmp_path_factory.mktemp("cgmes_bd") / "micro_be_with_bd.zip"
    shutil.copy(cgmes_zip, path)
    with zipfile.ZipFile(boundary_zip) as src, zipfile.ZipFile(path, "a") as dst:
        for i, name in enumerate(src.namelist()):
            dst.writestr(f"ENTSOE_{i}.xml", src.read(name))
    return path


def test_ships_boundary(cgmes_files, cgmes_with_boundary, tmp_path):
    cgmes_zip, boundary_zip = cgmes_files
    assert ships_boundary(str(cgmes_with_boundary)) is True
    assert ships_boundary(str(boundary_zip)) is True
    assert ships_boundary(str(cgmes_zip)) is False
    # A directory of profiles, and paths that are not CGMES at all
    with zipfile.ZipFile(cgmes_with_boundary) as archive:
        archive.extractall(tmp_path / "profiles")
    assert ships_boundary(str(tmp_path / "profiles")) is True
    assert ships_boundary(str(tmp_path / "missing.zip")) is False


@pytest.mark.asyncio
async def test_boundary_set_ignored_when_the_archive_ships_its_own(
    io_tools, cgmes_with_boundary, boundary_sets_dir
):
    result = await io_tools.load_network_from_file(
        str(cgmes_with_boundary), "be", boundary_set="BDS_test.zip", ctx=_Ctx()
    )
    assert result["status"] == "success", result
    assert result["import_format"] == "CGMES"
    # Not reported as applied: PowSyBl read the archive's boundary instead
    assert "boundary" not in result
    assert "ships its own boundary profiles" in " ".join(result["warnings"])


@pytest.mark.asyncio
async def test_boundary_set_used_is_reported_without_warning(
    io_tools, cgmes_files, boundary_sets_dir
):
    cgmes_zip, _ = cgmes_files
    result = await io_tools.load_network_from_file(
        str(cgmes_zip), "be", boundary_set="BDS_test.zip", ctx=_Ctx()
    )
    assert result["status"] == "success", result
    assert result["boundary"] == "boundary_set 'BDS_test.zip'"
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_boundary_url_without_extension(io_tools, cgmes_files, tmp_path):
    """A URL such as .../download?id=42 saves a file PowSyBl would skip."""
    cgmes_zip, boundary_zip = cgmes_files
    served = tmp_path / "download"
    shutil.copy(boundary_zip, served)
    result = await io_tools.load_network_from_url(
        cgmes_zip.as_uri(), "be", boundary_url=served.as_uri(), ctx=_Ctx()
    )
    assert result["status"] == "success", result
    assert result["boundary"] == "boundary_url (download.zip)"


@pytest.mark.asyncio
async def test_options_of_another_format_are_reported_as_ignored(
    io_tools, cgmes_files, boundary_sets_dir, tmp_path
):
    # An XIIDM file whose network came from IEEE-CDF: source_format says
    # IEEE-CDF, yet the XIIDM importer is the one that reads it.
    path = tmp_path / "ieee14.xiidm"
    pp.network.create_ieee14().save(str(path))
    result = await io_tools.load_network_from_file(
        str(path),
        "ieee14",
        parameters={
            "iidm.import.cgmes.naming-strategy": "cgmes",
            "iidm.import.xml.with-automation-systems": False,
        },
        post_processors=["EntsoeCategory"],
        boundary_set="BDS_test.zip",
        ctx=_Ctx(),
    )
    assert result["status"] == "success", result
    assert result["source_format"] == "IEEE-CDF"
    assert result["import_format"] == "XIIDM"
    assert "boundary" not in result
    warnings = " ".join(result["warnings"])
    assert "iidm.import.cgmes.naming-strategy" in warnings
    assert "iidm.import.xml.with-automation-systems" not in warnings
    assert "EntsoeCategory" in warnings
    assert "boundary_set 'BDS_test.zip' not used" in warnings


@pytest.mark.asyncio
async def test_matching_options_give_no_warning(io_tools, tmp_path):
    path = tmp_path / "ieee14.jiidm"
    pp.network.create_ieee14().save(str(path), format="JIIDM")
    result = await io_tools.load_network_from_file(
        str(path),
        "ieee14",
        parameters={"iidm.import.xml.with-automation-systems": False},
        ctx=_Ctx(),
    )
    assert result["status"] == "success", result
    assert result["import_format"] == "JIIDM"
    assert "warnings" not in result


def test_import_format_without_a_report():
    network = MagicMock(source_format="UCTE")
    assert import_format_of(network, None) == "UCTE"
    assert import_format_of(MagicMock(source_format="unknown"), "{}") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["XIIDM", "JIIDM", "MATPOWER"])
async def test_network_url_without_extension(io_tools, tmp_path, fmt):
    """A network served as .../download?id=42 is still read by its importer."""
    saved = tmp_path / "saved"
    saved.mkdir()
    pp.network.create_ieee14().save(str(saved / "grid"), format=fmt)
    (served,) = saved.iterdir()
    shutil.copy(served, tmp_path / "download")
    result = await io_tools.load_network_from_url(
        (tmp_path / "download").as_uri(), "ieee14", ctx=_Ctx()
    )
    assert result["status"] == "success", result
    assert result["import_format"] == fmt
