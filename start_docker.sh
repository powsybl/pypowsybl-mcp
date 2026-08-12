#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

source .venv/bin/activate

git pull

export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

echo "Using HOST_UID=$HOST_UID"
echo "Using HOST_GID=$HOST_GID"

mkdir -p data logs

docker compose down
docker compose up --build -d
