# The image every service runs: the web app and the four scheduled jobs. One
# image, one build, and the only difference between the services is the start
# command - so a job can never be running different code from the API that
# shows its results.
# Stage one builds the front end. Node is needed to produce the bundle and
# never to serve it, so it does not travel into the runtime image.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

# Fail loudly and log immediately: a container whose output is buffered looks
# hung when it is merely working.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so editing source does not reinstall the world.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src/ ./src/
COPY jobs/ ./jobs/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY tests/ ./tests/
COPY pytest.ini ./
COPY alembic.ini ./
COPY alembic/ ./alembic/

# The built front end, served by the API from the same origin. Same origin is
# what lets the session cookie be SameSite=Lax with no CORS configuration to
# get wrong.
COPY --from=web /web/dist ./web/dist

# Prove the image works while it is being built, so a broken one fails here
# rather than at 07:00 on a Monday with nobody watching.
RUN python -c "import sys; sys.path.insert(0,'src'); \
import lnp.models, lnp.drafting, lnp.linkedin, lnp.tokens, \
lnp.runner, lnp.db.store, lnp.api.app; \
print('imports ok')" \
 && python -m pytest -x

# Overridden per service. Defaults to the web app: it is the service that has
# to be up, and it is the one that cannot post.
ENV PORT=8000
CMD ["sh", "scripts/serve.sh"]
