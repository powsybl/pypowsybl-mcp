#
# Copyright (c) 2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# SPDX-License-Identifier: MPL-2.0
# This file is part of pypowsybl-mcp.
#

source .venv/bin/activate

git pull

export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

echo "Using HOST_UID=$HOST_UID"
echo "Using HOST_GID=$HOST_GID"

mkdir -p data logs

docker compose down
docker compose up --build -d
