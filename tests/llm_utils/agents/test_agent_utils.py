#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import MagicMock, patch

from pypowsybl_mcp.llm_utils.agents.agent_utils import create_agent


def test_create_agent_defaults_model_settings_when_not_provided():
    with (
        patch(
            "pypowsybl_mcp.llm_utils.agents.agent_utils.LitellmModel"
        ) as mock_model_cls,
        patch("pypowsybl_mcp.llm_utils.agents.agent_utils.Agent") as mock_agent_cls,
        patch(
            "pypowsybl_mcp.llm_utils.agents.agent_utils.ModelSettings"
        ) as mock_settings_cls,
    ):
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_settings = MagicMock()
        mock_settings_cls.return_value = mock_settings
        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent

        result = create_agent(
            name="my-agent",
            model_name="gpt-4",
            api_key="key",
            base_url="http://localhost",
            instructions="do stuff",
            mcp_servers=[],
        )

        mock_settings_cls.assert_called_once_with()
        mock_agent_cls.assert_called_once_with(
            name="my-agent",
            model=mock_model,
            instructions="do stuff",
            mcp_servers=[],
            tools=None,
            model_settings=mock_settings,
        )
        assert result == mock_agent


def test_create_agent_uses_provided_model_settings():
    with (
        patch("pypowsybl_mcp.llm_utils.agents.agent_utils.LitellmModel"),
        patch("pypowsybl_mcp.llm_utils.agents.agent_utils.Agent") as mock_agent_cls,
        patch(
            "pypowsybl_mcp.llm_utils.agents.agent_utils.ModelSettings"
        ) as mock_settings_cls,
    ):
        custom_settings = MagicMock()

        create_agent(
            name="my-agent",
            model_name="gpt-4",
            api_key="key",
            base_url="http://localhost",
            instructions="do stuff",
            mcp_servers=[],
            model_settings=custom_settings,
        )

        mock_settings_cls.assert_not_called()
        _, kwargs = mock_agent_cls.call_args
        assert kwargs["model_settings"] == custom_settings
