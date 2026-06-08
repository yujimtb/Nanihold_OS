FROM python:3.11-slim

ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=${USER_UID}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv

ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
    && python -m venv "${VIRTUAL_ENV}" \
    && groupadd --gid "${USER_GID}" "${USERNAME}" \
    && useradd --uid "${USER_UID}" --gid "${USER_GID}" -m "${USERNAME}" \
    && chown -R "${USERNAME}:${USERNAME}" "${VIRTUAL_ENV}" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY --chown=${USERNAME}:${USERNAME} pyproject.toml README.md ./
COPY --chown=${USERNAME}:${USERNAME} vsm ./vsm

RUN python -m pip install --upgrade pip \
    && python -m pip install -e .

USER ${USERNAME}
