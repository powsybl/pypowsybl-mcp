#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import patch

import pytest
from loguru import logger

from pypowsybl_mcp.utils import logging_setup
from pypowsybl_mcp.utils.logging_setup import LOG_FILE_NAME, setup_file_logging


@pytest.fixture(autouse=True)
def reset_file_sink():
    """Remove the sink a test added so tests stay independent."""
    logging_setup._file_sink_id = None
    yield
    if logging_setup._file_sink_id is not None:
        logger.remove(logging_setup._file_sink_id)
        logging_setup._file_sink_id = None


def test_logs_are_written_to_log_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / "nested" / "logs"
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    log_file = setup_file_logging()
    logger.info("hello from the test")
    logger.complete()

    assert log_file == log_dir / LOG_FILE_NAME
    assert "hello from the test" in log_file.read_text(encoding="utf-8")


def test_relative_log_dir_is_resolved_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", "logs")

    assert setup_file_logging() == tmp_path / "logs" / LOG_FILE_NAME


def test_default_log_dir_is_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOG_DIR", raising=False)

    assert setup_file_logging() == tmp_path / "logs" / LOG_FILE_NAME


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_log_dir_disables_file_logging(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", value)

    assert setup_file_logging() is None
    assert logging_setup._file_sink_id is None
    assert list(tmp_path.iterdir()) == []


def test_second_call_does_not_add_another_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    setup_file_logging()
    first_sink = logging_setup._file_sink_id
    setup_file_logging()
    logger.info("only once")
    logger.complete()

    assert logging_setup._file_sink_id == first_sink
    content = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert content.count("only once") == 1


def test_unwritable_log_dir_falls_back_to_stderr(tmp_path, monkeypatch):
    # A regular file where the directory should be makes mkdir fail.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")
    monkeypatch.setenv("LOG_DIR", str(blocker / "logs"))

    with patch("pypowsybl_mcp.utils.logging_setup.logger.warning") as mock_warning:
        assert setup_file_logging() is None

    mock_warning.assert_called_once()
    assert "stderr only" in mock_warning.call_args[0][0]
    assert logging_setup._file_sink_id is None
