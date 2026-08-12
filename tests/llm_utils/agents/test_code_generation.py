#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pypowsybl_mcp.llm_utils.agents import code_generation


@pytest.mark.asyncio
async def test_generate_code_from_macro_success():
    mock_result = MagicMock()
    mock_result.final_output = "print('hello world')"

    with patch.object(
        code_generation.Runner, "run", AsyncMock(return_value=mock_result)
    ):
        response = await code_generation.generate_code_from_macro(
            actions="create_ieee14; run_ac_loadflow",
            reference_code="def foo(): pass",
        )

    assert response == "print('hello world')"


@pytest.mark.asyncio
async def test_generate_code_from_macro_without_final_output_attr():
    # Runner.run may return something without a `final_output` attribute;
    # in that case the function should fall back to str(result).
    with patch.object(
        code_generation.Runner, "run", AsyncMock(return_value="raw_result = 1")
    ):
        response = await code_generation.generate_code_from_macro(
            actions="create_ieee14",
        )

    assert response == "raw_result = 1"


@pytest.mark.asyncio
async def test_generate_code_from_macro_invalid_syntax():
    mock_result = MagicMock()
    mock_result.final_output = "def main(:\n    pass"

    with patch.object(
        code_generation.Runner, "run", AsyncMock(return_value=mock_result)
    ):
        response = await code_generation.generate_code_from_macro(
            actions="create_ieee14",
        )

    assert response.startswith("# Error: Generated code has a syntax error")


@pytest.mark.asyncio
async def test_generate_code_from_macro_timeout(monkeypatch):
    async def slow_run(*args, **kwargs):
        await asyncio.sleep(1)
        return MagicMock(final_output="too late")

    monkeypatch.setattr(code_generation, "AI_TIMEOUT", 0.01)

    with patch.object(code_generation.Runner, "run", slow_run):
        response = await code_generation.generate_code_from_macro(
            actions="create_ieee14",
        )

    assert response.startswith("# Error: Code generation timed out after")


@pytest.mark.asyncio
async def test_generate_code_from_macro_generic_exception():
    with patch.object(
        code_generation.Runner,
        "run",
        AsyncMock(side_effect=ValueError("boom")),
    ):
        response = await code_generation.generate_code_from_macro(
            actions="create_ieee14",
        )

    assert response == "# Error: Error during code generation: boom"
