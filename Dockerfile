# syntax=docker/dockerfile:1
# praat-parselmouth has no aarch64 wheels and MFA's Kaldi builds are linux-64,
# so this image is amd64 only. Build with --platform linux/amd64 on Apple Silicon.
FROM --platform=linux/amd64 mambaorg/micromamba:2.9.0-debian13@sha256:a8d98f861cb0d415870c515fcb71658802d4e9254e7d8a361b7e6fc866801ced AS base

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
    && echo "d35ce271ded357d833d2f4b8d1041dc3748b9538567ba13f2c697f4e4126711b  /tmp/english_us_arpa.zip" | sha256sum -c - \
    && unzip -q /tmp/english_us_arpa.zip -d $MODEL_DIR/acoustic \
    && rm /tmp/english_us_arpa.zip \
    && curl -fsSL -o $MODEL_DIR/dictionary/english_us_arpa.base.dict \
        https://github.com/MontrealCorpusTools/mfa-models/releases/download/dictionary-english_us_arpa-v3.0.0/english_us_arpa.dict \
    && echo "e8c6c7b036ae2b7c78d2768b8dc6b1f9359175b842956d00b48c53c9c332e6b0  $MODEL_DIR/dictionary/english_us_arpa.base.dict" | sha256sum -c - \
    && chown -R $MAMBA_USER:$MAMBA_USER $MODEL_DIR

ENV WHISPER_MODEL_DIR=/opt/whisper/tiny.en
RUN mkdir -p $WHISPER_MODEL_DIR && cd $WHISPER_MODEL_DIR \
    && printf '%s\n' \
        "14b1b421a90349bc551b881461426b561a874049cb9e4c4864f2ca384f6a7cc5  config.json" \
        "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a  model.bin" \
        "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df  tokenizer.json" \
        "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf  vocabulary.txt" \
       > /tmp/whisper.sha256 \
    && while read -r sum f; do \
         curl -fsSL -o $f https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/0d3d19a32d3338f10357c0889762bd8d64bbdeba/$f || exit 1; \
       done < /tmp/whisper.sha256 \
    && sha256sum -c /tmp/whisper.sha256 && rm /tmp/whisper.sha256 \
    && chown -R $MAMBA_USER:$MAMBA_USER $WHISPER_MODEL_DIR
USER $MAMBA_USER

COPY --chown=$MAMBA_USER:$MAMBA_USER env.lock /tmp/env.lock
RUN micromamba install -y -n base -f /tmp/env.lock && micromamba clean --all --yes

COPY --chown=$MAMBA_USER:$MAMBA_USER requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

# MFA creates its root directory at import time, so it must be writable. Nothing
# in it needs to persist: models are passed by path from MODEL_DIR.
ENV MFA_ROOT_DIR=/tmp/mfa

WORKDIR /app

COPY --chown=$MAMBA_USER:$MAMBA_USER LICENSE NOTICE /app/

COPY --chown=$MAMBA_USER:$MAMBA_USER dictionary/ /app/dictionary/
RUN cat $MODEL_DIR/dictionary/english_us_arpa.base.dict \
        /app/dictionary/cmudict.dict /app/dictionary/custom.dict \
        > $MODEL_DIR/dictionary/english_us_arpa.dict

COPY --chown=$MAMBA_USER:$MAMBA_USER app/ /app/app/

ENV PORT=8080
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1

# CI builds this stage and runs pytest in it; the shipped image stops at base.
FROM base AS test
COPY --chown=$MAMBA_USER:$MAMBA_USER requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
COPY --chown=$MAMBA_USER:$MAMBA_USER pytest.ini env.yaml env.lock requirements.txt requirements.lock /app/
COPY --chown=$MAMBA_USER:$MAMBA_USER tests/ /app/tests/
