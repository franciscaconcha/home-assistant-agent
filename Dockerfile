ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:444b42594e7f467425872fc35e077a227d377f0fe0726bc818706d62175fad8b
FROM ${HERMES_IMAGE}

USER root
COPY --from=ghcr.io/astral-sh/uv:0.9.4@sha256:c4089b0085cf4d38e38d5cdaa5e57752c1878a6f41f2e3a3a234dc5f23942cb4 /uv /usr/local/bin/household-uv
COPY pyproject.toml uv.lock /opt/household-tools/
COPY THIRD_PARTY_NOTICES.md /opt/household-tools/
COPY src /opt/household-tools/src
RUN cd /opt/household-tools && \
    UV_PROJECT_ENVIRONMENT=/opt/household-tools/.venv \
    household-uv sync --locked --no-dev --no-editable --python /usr/bin/python3 && \
    chmod -R a+rX /opt/household-tools

# Preserve Hermes's upstream entrypoint, supervision, and runtime privilege drop.
