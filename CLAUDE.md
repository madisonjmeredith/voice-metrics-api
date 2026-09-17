# CLAUDE.md

Guidance for Claude Code working in this repository. `README.md` is the
user-facing document; this file covers why the code looks the way it does.

## What this is

A FastAPI service that scores a recording plus its transcript for pitch and
resonance, called from a queued job in the Laravel app that consumes it. It
replaces that app's older in-repo service, which ignored the transcript.

The sibling `~/Web/personal/formantry` is a public acoustic-analysis service
that this repo copies code from (`converter.py`, the analyzer, the test
synthesizer). Nothing from this repo goes back the other way. Ever.

## Layout

| File | Role |
| --- | --- |
| `app/main.py` | FastAPI app, validation, error-to-status table, response assembly |
| `app/worker.py` | One aligner process: model paths, warm-up, hard timeout, restart on crash |
| `app/aligner.py` | In-process MFA alignment via kalpy; span dataclasses |
| `app/analyzer.py` | Praat pitch and formant tracks (from formantry) |
| `app/measurement.py` | One F0/F1–F3 reading per phone at its midpoint |
| `app/scoring.py` | Port of the reference `resonance.py`: outliers, z-scores, weights |
| `app/reference/` | `stats.json` and `weights.json`, copied verbatim from upstream |
| `app/warmup.wav` | 1.3 s clip aligned at startup so the model loads before traffic |
| `tests/textgrids.py` | praatio TextGrid parser, used only to turn checked-in alignments into fixtures |
| `spike/` | Timing script and the parity-number generator; not shipped |

## Decisions that were measured or negotiated, not defaulted

**The aligner is loaded once and kept in a worker process.** Measured in the
image under emulation: model plus lexicon load about 8 s once, then 0.02–0.7 s
per clip. The CLI `mfa align_one` took 9.4 s cold and 3.1 s with `--no_clean`
for the same clip because it re-extracts the model and recompiles the lexicon
every call. The client polls for 45 s with a queue worker in front, so
the CLI path was never going to fit. It lives in a separate process because
Kaldi reports fatal errors with `abort()`.

The pool queues its own warm-up alignment on every start, so the model loads
while the server boots and again while a 503/504 goes out after a restart;
`/health` reports `warming` until that finishes. The Praat pass runs
concurrently with alignment because it needs only the WAV.

**`mfa align_one` needs no database.** Its code path (mirrored in
`aligner.py`) never opens MFA's SQLAlchemy session. Do not add `mfa server`
or PostgreSQL setup to the image.

**No spaCy.** The `english_us_arpa` v3.0.0 model declares `language: unknown`,
so MFA uses `SimpleTokenizer`. If the model is ever swapped for one that
declares English, MFA will try to load `en_core_web_sm` at request time.

**Formant ceiling is 5000 Hz, not formantry's 5500.** `stats.json` was built
with the reference project's Praat settings (`0 5 5000 0.025 50`), and the
z-scores only mean something against readings taken the same way.

**The aligned phone label is the stats key.** The reference looked up the
cmudict pronunciation by index within the word, which drifts on pronunciation
variants. The aligner already chose a pronunciation; use it.

**The score scale is the reference project's.** 0.5 is the pooled population
mean, not the midpoint between two reference groups as in the client's old
service. The client's resonance target will need re-tuning against real data.
This is a known follow-up, not a bug.

**Reference data is AGPL-3.0.** `stats.json` and `weights.json` are copied
from Luna McNulty's reference project, which makes this repo a derivative
work. It is licensed AGPL-3.0 and its source must stay published. Do not
relicense, and keep `NOTICE` accurate. `NOTICE` credits the author without
naming or linking the project; leave it that way.

**Out-of-vocabulary words are reported, not guessed.** G2P would mutate the
shared lexicon per request and its thread-safety is unverified. `oovs` in
the response tells the client which words went unscored.

**Model files are curled from the GitHub release**, not fetched with
`mfa model download`, because that goes through the unauthenticated GitHub
API and rate-limits CI builds. The acoustic zip is pre-extracted so nothing
unpacks per request.

## Response compatibility

The client reads `pitch.{mean,median,stdev,min,max}`,
`resonance.{mean,median,stdev}`, `duration`, and `sample_count`. Keep those
names and shapes. Anything new is additive.

## Tests

```sh
.venv/bin/pytest            # unit tests, ~10 s; integration tests skip without MFA
docker buildx build --platform linux/amd64 --target test --load -t voice-metrics-api:test .
docker run --rm voice-metrics-api:test pytest -q -rs     # everything, including MFA
```

The `test` stage adds pytest and the test tree on top of the shipped image;
CI builds that stage and runs the suite in it.
The parity test in `tests/test_scoring.py` pins this port to the reference
implementation's numbers. Regenerate them with `spike/parity_expected.py` if
`tests/parity_data.py` changes, and only then.

Speech fixtures come from macOS `say`; see `tests/fixtures/README.md`. Synthetic
vowels come from formantry's source-filter model in `tests/conftest.py`.

## Building

```sh
docker buildx build --platform linux/amd64 --load -t voice-metrics-api .
```

amd64 only, for the reasons in the README. The image is about 0.85 GB.
