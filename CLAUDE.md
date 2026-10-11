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
| `dictionary/cmudict.dict` | CMUdict entries `english_us_arpa` lacks, alphabetic headwords only; generated, appended to the release dictionary at build |
| `dictionary/LICENSE.cmudict` | CMUdict's BSD 2-clause license, verbatim; stays beside the entries |
| `dictionary/custom.dict` | Hand-written words neither of those has; appended last |
| `tests/textgrids.py` | praatio TextGrid parser, used only to turn checked-in alignments into fixtures |
| `spike/` | Timing script, the parity-number generator, and `cmudict_gap.py`, which regenerates `cmudict.dict`; not shipped |

## Decisions that were measured or negotiated, not defaulted

**The aligner is loaded once and kept in a worker process.** Measured in the
image under emulation: model plus lexicon load about 10 s once (about 8 s
before `cmudict.dict` was merged in; peak RSS went from 1,255 MB to about
1,450 MB), then 0.02–0.7 s per clip. The CLI `mfa align_one` took 9.4 s cold
and 3.1 s with `--no_clean` for the same clip because it re-extracts the
model and recompiles the lexicon every call. The client polls for 45 s with a queue worker in front, so
the CLI path was never going to fit. It lives in a separate process because
Kaldi reports fatal errors with `abort()`.

The pool queues its own warm-up alignment on every start, so the model loads
while the server boots and again while a 503/504 goes out after a restart;
`/health` reports `warming` until that finishes. The Praat pass runs
concurrently with alignment because it needs only the WAV.

A request that arrives during a restart has the reload counted against
`ALIGN_TIMEOUT` (10 s). Under emulation the reload is now about that long, so
such a request can time out and trigger another restart. The load has not
been measured on native amd64.

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
the response tells the client which words went unscored. A word worth
supporting gets an entry in `dictionary/custom.dict`. The only
`english_us_arpa` G2P model in the `mfa-models` releases (v2.0.0a) was
measured in #9: none of its three guesses for `niamh` or `siobhan` matched a
CMUdict pronunciation, and 44 words took 67 s under emulation.

**CMUdict's missing words are a committed, generated file.** For the 66,015
headwords the two share, the release dictionary carries a CMUdict
pronunciation verbatim 96.9% of the time, and it lacks another 60,037 CMUdict
headwords outright. `dictionary/cmudict.dict` is the 54,684 of those that are
purely alphabetic (57,253 lines), taken from CMUdict at the commit pinned in
`spike/cmudict_gap.py`. It is committed instead of fetched at build so the
format checks cover every line without Docker and the build gains no network
dependency. Alphabetic only because the tokenizer already splits clitics and
compounds: of the 5,353 missing headwords with an apostrophe, hyphen, or
period, 89 stay OOV once the alphabetic entries are in. Do not edit the file
by hand. Rerun the script when the pinned commit or the release dictionary
changes, and only then. CMUdict is BSD 2-clause and its first clause deems the
file's contents source code, so `dictionary/LICENSE.cmudict` and the `NOTICE`
entry stay with the entries. CMUdict is ASCII-only, so accented spellings (#10)
and names it lacks are still OOV.

**Extra words are merged into the dictionary at build time.** The release
asset is saved as `english_us_arpa.base.dict` and the Dockerfile appends
`dictionary/cmudict.dict` and then `dictionary/custom.dict` to it to produce
`english_us_arpa.dict`, the one path `worker.py` loads. kalpy's
`load_pronunciations` takes a single file and is not written to be called
twice. The merge sits after the dependency layers so a dictionary edit does
not reinstall the conda environment; do not move that `COPY` up into the model
layer. The `COPY` takes the whole directory so `LICENSE.cmudict` reaches the
image with the entries.

**`tests/test_dictionary.py` is the only thing between a mistyped entry and a
crash.** kalpy accepts any token as a phone. Measured: an entry with a phone
the acoustic model lacks (`ER3`, a vowel with no stress digit, lowercase)
loads cleanly, aligns every other transcript, and segfaults the worker when
its word is aligned, so warm-up never sees it. The format has no comment
syntax either: `# note` parses as the word `#` with the phone `note`. A line
with no phones raises in `load_pronunciations` and the aligner never becomes
ready. The format checks run without MFA so a typo is caught before a build.
They cover `cmudict.dict` and `custom.dict` alike, and one of them fails when
the two files share a headword. In the image the same file checks that its
hardcoded phone inventory equals the model's, that every word in either file
tokenizes, and that the loaded dictionary's lines for each file's words are
exactly that file's. That last one fails on a lost or stale merge and on a
word the base dictionary already has, which kalpy would not report: a
repeated headword becomes a second pronunciation. Several lines for one word
inside a file are deliberate variants. Relax that check if variants of base
words are ever wanted. `custom.dict` has been empty since #9 because CMUdict
has all four of the words it held, so its checks pass on nothing until an
entry is added. Phones without an entry in
`stats.json` (any `AW` or `UH`, `JH`, `Y`, `ZH`, most vowels outside primary
stress) still count toward `speech_coverage` and pitch; they get no resonance
score.

**A mismatched take is reported as `speech_coverage`, not judged.** When the
audio does not match the transcript, MFA puts the slack into `sil`/`sp`
instead of stretching a speech label, so `suspicious` rarely fires, and
`word_count` is only the transcript's word count minus `len(oovs)`. The share
of the clip under speech phones does separate them: 0.78–0.82 for real reads,
slow ones included, against 0.02–0.22 for tones and looped clips in issue #3's
measurements. The service returns the number and the caller owns the floor,
so it can be re-tuned without a redeploy. `spn` spans are excluded, so OOV
words lower it. Speech-dense audio with the wrong words still reads high
(0.73); catching that needs recognition, not alignment.

**Model files are curled from the GitHub release**, not fetched with
`mfa model download`, because that goes through the unauthenticated GitHub
API and rate-limits CI builds. The acoustic zip is pre-extracted so nothing
unpacks per request. The dictionary asset lands as
`english_us_arpa.base.dict`; the file the aligner loads is built from it.
That layer is first in the Dockerfile because it depends on nothing else, so
it survives every dependency change.

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
