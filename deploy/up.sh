#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

python init-local-env.py

docker compose --env-file env/local.env --profile worker up -d --build

docker compose --env-file env/local.env --profile worker ps

port="${QA_WEBAPP_PORT:-8800}"
cat <<EOF

qa-workflow is starting.
Open: http://127.0.0.1:${port}
Health: http://127.0.0.1:${port}/healthz

Knowledge base bootstrap runs in the kb-migrate service before web/worker start.
To create the first user inside the web container, run:
  docker compose --env-file env/local.env exec web python -m webapp.auth adduser <username> --name <display-name> --role admin
EOF
