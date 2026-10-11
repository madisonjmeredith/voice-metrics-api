# syntax=docker/dockerfile:1
# praat-parselmouth has no aarch64 wheels and MFA's Kaldi builds are linux-64,
# so this image is amd64 only. Build with --platform linux/amd64 on Apple Silicon.
FROM --platform=linux/amd64 mambaorg/micromamba:2.9.0-debian13 AS base

ARG MAMBA_DOCKERFILE_ACTIVATE=1

# Release assets, not `mfa model download`: that uses the GitHub API, whose rate limit fails CI.
ENV MODEL_DIR=/opt/mfa-models
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p $MODEL_DIR/acoustic $MODEL_DIR/dictionary \
    && curl -fsSL -o /tmp/english_us_arpa.zip \
        https://github.com/MontrealCorpusTools/mfa-models/releases/download/acoustic-english_us_arpa-v3.0.0/english_us_arpa.zip \
    && unzip -q /tmp/english_us_arpa.zip -d $MODEL_DIR/acoustic \
    && rm /tmp/english_us_arpa.zip \
    && curl -fsSL -o $MODEL_DIR/dictionary/english_us_arpa.base.dict \
        https://github.com/MontrealCorpusTools/mfa-models/releases/download/dictionary-english_us_arpa-v3.0.0/english_us_arpa.dict \
    && chown -R $MAMBA_USER:$MAMBA_USER $MODEL_DIR
USER $MAMBA_USER

COPY --chown=$MAMBA_USER:$MAMBA_USER env.yaml /tmp/env.yaml
RUN micromamba install -y -n base -f /tmp/env.yaml && micromamba clean --all --yes

COPY --chown=$MAMBA_USER:$MAMBA_USER requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# MFA creates its root directory at import time, so it must be writable. Nothing
# in it needs to persist: models are passed by path from MODEL_DIR.
ENV MFA_ROOT_DIR=/tmp/mfa

WORKDIR /app

COPY --chown=$MAMBA_USER:$MAMBA_USER dictionary/custom.dict /app/dictionary/custom.dict
RUN cat $MODEL_DIR/dictionary/english_us_arpa.base.dict /app/dictionary/custom.dict \
        > $MODEL_DIR/dictionary/english_us_arpa.dict

COPY --chown=$MAMBA_USER:$MAMBA_USER app/ /app/app/

ENV PORT=8080
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1

# CI builds this stage and runs pytest in it; the shipped image stops at base.
FROM base AS test
COPY --chown=$MAMBA_USER:$MAMBA_USER requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
COPY --chown=$MAMBA_USER:$MAMBA_USER pytest.ini /app/
COPY --chown=$MAMBA_USER:$MAMBA_USER tests/ /app/tests/
