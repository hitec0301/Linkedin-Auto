#!/bin/sh
# Start command for the web service.
#
# Migrations run here rather than in a separate release step, because Railway
# has no release phase and a schema that is applied by hand is a schema that
# will one day be forgotten. `alembic upgrade head` is a no-op when there is
# nothing to do, so a redeploy costs a connection and a query.
#
# This assumes one web replica. Two replicas starting together would both try
# to migrate; Alembic takes a lock and one waits, but if you ever scale the web
# service beyond one, move this into a one-off job instead.
set -e

echo "applying migrations"
alembic upgrade head

# The only service left. It serves the API and the built front end, and it
# also runs the publish checker - the one thing that still needs a clock
# running when nobody is looking at the screen - in a background thread.
export LNP_PUBLISH_CHECKER=1

echo "starting on port ${PORT:-8000}"
exec python -m uvicorn lnp.api.app:app \
  --host 0.0.0.0 --port "${PORT:-8000}" --app-dir src --proxy-headers
