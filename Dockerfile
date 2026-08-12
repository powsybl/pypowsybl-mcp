#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

# Use the specified base image
FROM python:3.13-slim-bookworm

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    JAVA_HOME=/usr/lib/jvm/default-java

# Install system dependencies
# - default-jre-headless for pypowsybl's core
# - libfontconfig1 and libxrender1 for visualization tools
# - locales for potential locale-specific data handling
# - curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jre-headless \
    libfontconfig1 \
    libxrender1 \
    locales \
    curl \
    && sed -i '/fr_FR.UTF-8/s/^# //g' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=fr_FR.UTF-8 \
    LANGUAGE=fr_FR:fr \
    LC_ALL=fr_FR.UTF-8

# Install uv for dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Handle UID and GID for the application user to avoid permission issues with volumes
# Use build args with defaults that can be overridden
ARG USER_UID=1000
ARG USER_GID=1000

# Create a non-root user
RUN groupadd --gid $USER_GID appuser \
    && useradd --uid $USER_UID --gid $USER_GID -m appuser

# Install project dependencies
# We copy only the files needed for installation first to leverage Docker cache
COPY pyproject.toml ./
RUN uv pip install --system --no-cache-dir .

# Copy the rest of the application code
COPY . .

# Ensure the appuser has permissions for the /app directory and create data/logs dirs
RUN mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

# Runtime-configurable port with default
ENV MCP_PORT=9992 \
    MCP_VERIFY_SSL=false

# Start the server
# Use python -m to run the package
CMD ["python", "-m", "pypowsybl_mcp.server", "--port", "${MCP_PORT}"]
