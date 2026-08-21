# The image every Railway service runs. One image, four services, four
# schedules - the only difference between them is the start command.
#
# Plain python:3.12-slim rather than a uv image. uv earns its place in
# setup.sh because there it replaces whatever Python is on your machine; inside
# a container the base image already pins the interpreter, so uv would be a
# dependency buying nothing.
FROM python:3.12-slim

# Fail loudly and log immediately: a container whose output is buffered looks
# hung when it is merely working.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LNP_DATA_DIR=/data

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

# Where the mounted volume lands. Tokens rotate, and a rotation written to the
# container filesystem is a rotation lost on the next deploy.
RUN mkdir -p /data

# Prove the image works while it is being built, so a broken one fails here
# rather than at 07:00 on a Monday with nobody watching.
RUN python -c "import sys; sys.path.insert(0,'src'); \
import lnp.models, lnp.sheets, lnp.drafting, lnp.linkedin, lnp.tokens; \
print('imports ok')" \
 && python -m pytest -x

# Overridden per service. Defaults to the job that cannot post.
CMD ["python", "jobs/curate.py"]
