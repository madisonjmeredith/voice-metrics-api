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

Source is in `app/`, tests in `tests/`. Non-obvious parts:

- `app/reference/` is copied verbatim from the reference project; don't edit it.
- `app/warmup.wav` is aligned at startup so the model loads before traffic.
- `dictionary/cmudict.dict` is generated (see below); `dictionary/LICENSE.cmudict` stays beside it; `dictionary/custom.dict` is hand-written and appended last.
- `tests/textgrids.py` is used only to turn checked-in alignments into fixtures.
- `tests/test_aligner.py` and `tests/test_worker.py` run without MFA, over stand-ins.
- `tests/test_analyzer_pool.py` runs without MFA too, over real Praat and a stand-in.
- `spike/` holds the timing script, the parity-number generator, and `cmudict_gap.py`; not shipped.

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
`/health` reports `warming` until that finishes.

Each call gets `ALIGN_TIMEOUT` (10 s) twice (#18): once to reach its turn,
which covers the warm-up and the calls ahead, and once to run. Running out of
the first answers `aligner_busy` and restarts nothing. Running out of the
second restarts the worker and answers `alignment_timeout`, for `/analyze` and
`/lexicon` alike. Before #18 one clock started at queueing, a reload under
emulation (about 11.5 s) outlasted it, and each request that arrived after a
restart killed the reload and started another: after one worker kill, 1
request per second failed 54 of 54 and a serial client 8 of 8 until traffic
stopped. A call can now take up to 20 s, inside the client's 30 s HTTP
timeout. A reload still running after `WARMUP_TIMEOUT` (60 s, about five
times the emulated reload) is restarted by the next call that gives up on it,
which answers `aligner_unavailable`; that keeps the self-healing a hung
reload got from any timeout before. The load has not been measured on native
amd64.

**The lexicon check runs in the aligner process.**
`POST /lexicon` calls `LoadedAligner.tokenize` through the pool, so its answer
is what an alignment would do by construction, and a change to `tokenize`
shows up there with no further work, as #10's accent fold did. The
alternative was measured in #7: MFA's `SimpleTokenizer` over a word set read
from the merged dictionary, in the API process. It matched the worker on 61 of
61 inputs at 3.7 µs per entry, but importing MFA took the API process from 144
MB to 341 MB RSS, 142 MB of that private, plus 25 MB for the words and 1.3 s at
startup. The queue wait it would have saved is usually small. Measured on
the built image under emulation, a three-word check takes 1.8 ms end to end
and 1,000 entries take 11 to 13 ms. With four clients looping `/analyze` the
median stayed near 3 ms, the 90th percentile was 254 to 317 ms, and the
slowest of 200 took 771 ms. The response shape is the same either way, so the
check can move in-process later without a caller noticing.
`AlignerPool.tokenize` shares `align`'s two clocks. Waiting never restarts
anything, so a check can't kill a reload or an alignment it was only waiting
behind. A check that itself runs past `ALIGN_TIMEOUT` restarts the worker and
answers `alignment_timeout`, because nothing else would restart a worker it
hung. A check that arrives as a reload begins waits out `ALIGN_TIMEOUT` and
gets `aligner_busy`, because under emulation the reload is about as long.
`Tokenized` in
`aligner.py` owns `in_vocabulary`: something left to align, by the test
`align` uses for `EmptyTranscriptError`, and nothing in `oovs`. `<unk>` and
`<eps>` read as in vocabulary because they are symbols in MFA's word table;
`/analyze`'s `oovs` has the same blind spot. The caps (1,000 entries of 200
characters) bound how long one check holds the worker. The slowest batch at
the caps measured so far is 1,000 entries of 100 one-letter accented chunks:
about 617 ms of tokenizer time under emulation, against about 220 ms before
the accent fold.

**Calls queue in the API process, a call restarts only the pool it arrived
on, and `shutdown()` does not cancel queued futures.** A call waits for its
pool's warm-up, then for `_turn` (an `asyncio.Lock`), and only then submits,
so the executor holds the warm-up and at most one call. Don't move the queue
back into the executor (#18): `Future.cancel()` can't take back the calls it
has already moved into its two-slot call queue, so a call whose caller gave
up would still run with nothing timing it, and if it hung nothing would
restart the worker. The warm-up wait is shielded because a timeout would
otherwise cancel the shared warm-up future through `wrap_future`. A call whose
turn comes after its pool was replaced raises `AlignerUnavailable`, so every
call waiting on a lost worker fails together with one restart. Each handler
used to call `restart()`, so the second killed the worker the first had just
started; `_restart_if_current` compares the pool a call arrived on with the
current one. `cancel_futures=True` raced the kill. Measured on Python 3.12
with six calls queued in the executor, over 10 trials, 39 of 60 calls
surfaced as `asyncio.CancelledError`, which no handler maps to a status;
without the flag all 60 got `BrokenProcessPool`. Do not add the flag back.
`tests/test_worker.py` covers these rules without MFA by patching `_init`,
`_align`, and `_tokenize` to stand-ins that are importable from the spawned
worker, and by pointing `WARMUP_TRANSCRIPT` at a slow or hanging stand-in.
`_turn` binds to the first event loop that waits on it, so a pool can't be contended
across two `asyncio.run` calls; start a fresh pool per test.

**Praat runs in its own process pool, after alignment (#19).** parselmouth
holds the GIL for a whole analysis, so in a thread it stalled the event loop,
finished alignments went uncollected until `ALIGN_TIMEOUT` ran out, and each
false timeout restarted a healthy aligner. Measured under emulation on
November 5, 2026, with a 58.8 s clip: in a thread, `/health` took up to 3.5 s
during one request, and bursts of 3, 4, 6, 12, and 24 such clips each failed
all or most requests and restarted the aligner. In the pool `/health` peaked
at 6.5 ms and every request in those bursts succeeded with no restart. Pitch
and Formant objects can't be pickled (parselmouth 0.4.7), so the alignment
goes to the worker and the measured phones come back. Praat's binary files
round-trip the tracks exactly and would have kept Praat overlapping alignment,
but reading them back would put the API process on the GIL again, and the
overlap saved only the alignment time: one long clip took 3.6 to 3.7 s either
way. `tests/test_analyzer_pool.py` pins the property: the event loop's longest
gap during a 58.8 s pass stays under a quarter of the pass's in-process time
(natively 6 ms in the pool, 222 ms in a thread). A worker that dies breaks the
whole executor, so every call on it answers `analyzer_unavailable` and the
first restarts the pool, the aligner's rule; the aligner is untouched. There
is no timeout, because Praat's work is bounded by `MAX_DURATION` and hasn't
been seen to hang. A hung worker would keep its request and its slot until the
container restarts. `ANALYZER_WORKERS` sets burst throughput: 24 concurrent
long clips took up to 46.2 s with the default 2 and 28.9 s with 4, under
emulation with 10 CPUs. It defaults to 2, not the CPU count, because a
container reports the host's CPUs and each worker holds 125 to 165 MB. Each
worker runs a Praat pass in its initializer, and one warm-up task per worker
is what spawns them all at boot; a task alone could land on a worker that was
already warm and leave another cold (about 0.35 s on its first request).

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

**`resonance.balanced` is the figure that holds across transcripts (#6).**
`mean` and `median` weight every scored phone once, so they follow the
transcript's phone mix as well as the voice. `balanced` averages per-label
means over vowels and sonorants only. Measured over 23 voices (the reference
project's 21 sample recordings and two of the client's speakers), each voice's
per-label profile swapped into 55 transcripts, within-voice SD over
between-voice SD was 0.310 for `median`, 0.311 duration-weighted, 0.252 for
`mean`, 0.227 counting each label once, and 0.180 for `balanced`. Obstruents
carry the largest shared per-label offsets (SH −0.30, HH +0.18) because their
midpoint formants aren't resonance. Comparing against a speaker at 0.5 on
every phone is a no-op, since that speaker pools to 0.5 on any transcript. The
working version is a per-label offset table fitted to real voices; it took
`balanced` to 0.169 and was left out because it would be a committed fit to
23 voices that needs refitting whenever alignment or measurement shifts.
`too_little_speech` counts `readings["balanced"]`, a subset of the resonance
readings; the fewest any of the client's 40 takes had was 5. Its values spread
wider across voices than `median`'s, so a target tuned on `median` does not
carry over.

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
headwords outright. `dictionary/cmudict.dict` is the ones that are
purely alphabetic, taken from CMUdict at the commit pinned in
`spike/cmudict_gap.py`. It is committed instead of fetched at build so the
format checks cover every line without Docker and the build gains no network
dependency. Alphabetic only because the tokenizer already splits clitics and
compounds: of the 5,353 missing headwords with an apostrophe, hyphen, or
period, 89 stay OOV once the alphabetic entries are in. Do not edit the file
by hand. Rerun the script when the pinned commit or the release dictionary
changes, and only then. CMUdict is BSD 2-clause and its first clause deems the
file's contents source code, so `dictionary/LICENSE.cmudict` and the `NOTICE`
entry stay with the entries. CMUdict is ASCII-only, so an accented spelling is
known only through the fold below, and names CMUdict lacks are still OOV.

**An accented word that is OOV aligns as its unaccented spelling.** None of
the merged dictionary's headwords has a non-ASCII character (#10).
`fold_accents` in `aligner.py` NFC-normalizes the transcript, then takes each
whitespace-delimited chunk that is not ASCII and has an OOV in it, strips its
combining marks, and keeps the stripped chunk only if nothing in it is OOV.
MFA's tokenizer then normalizes the rewritten transcript as before, so a
folded word gets exactly what its unaccented spelling gets: `coup d'état`
reaches the `d'etat` headword, where a fold on the already-split parts would
give `d' etat`. Do not move the fold after the split or into a copy of the
tokenizer's loop. An ASCII transcript returns before the tokenizer is called:
10.3 µs before and 10.5 µs after for a sentence under emulation, against 20.5
to 45.5 µs for one with five accented words. All-or-nothing per chunk keeps
`oovs` in the caller's spelling (`Łódź` would half-fold to `łodz`), at the
cost that `Zoë-Zorblax`, or two words run together around an em dash, stays
OOV whole when the other half is unknown. NFC comes first so a decomposed
spelling reaches an accented headword before any fold is tried. That makes an
accented entry in `custom.dict` the override for a wrong fold in either
Unicode form; checked on a copy of the dictionary with `rosé` appended, where
both forms tokenized as `rosé` and `José` still folded. Wrong folds are real:
of 76 loanwords and names ending in é or ée, 72 fold into vocabulary and 42
of those land on a headword with no pronunciation ending in EY (`rosé` on
`rose`, `exposé` on `expose`, `pâté` on `pate`); `résumé` is fine because
`resume` carries R EH1 Z AH0 M EY2. Decided in #10: fold them all and add an
entry only when a word matters, since the unaccented spelling typed today gets
the same pronunciation, and add no response field. `oovs` keeps the spelling
of anything still unknown, and `words` and `/lexicon`'s `normalized` show the
unaccented spelling of anything folded. A letter that is not a base plus a
mark (`ł`, `ø`, `đ`, `ß`, `æ`) does not fold and needs its own headword. A
lone combining mark normalizes to nothing instead of an OOV, and a fold inside
a bracketed annotation changes only the OOV string. Across the first-name
lists of the 60 faker 40.43.0 locales whose names are all Latin script, 13,308
of 21,374 distinct names were OOV before the fold and 12,036 after.
`tests/test_aligner.py` covers the rules over a stand-in tokenizer and runs
without MFA; its decomposed inputs are written as escapes because an editor
can normalize a literal combining character without a test noticing.

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
the two files share a headword. Every headword must be NFC, because
`fold_accents` NFC-normalizes the transcript and a decomposed headword could
never match. In the image the same file checks that its
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
instead of stretching a speech label, and `word_count` is only the
transcript's word count minus `len(oovs)`. The share of the clip under speech
phones does separate them: 0.78–0.82 for real reads, slow ones included,
against 0.02–0.22 for tones and looped clips in issue #3's measurements. The
service returns the number and the caller owns the floor, so it can be
re-tuned without a redeploy. `spn` spans are excluded, so OOV words lower it.
Speech-dense audio with the wrong words still reads high (0.73); catching that
needs recognition, not alignment. `suspicious`, a flag for a phone over 0.8 s
or a word over 2 s, was removed in #17 because it caught none of #3's
mismatched takes. Of 39 later takes it flagged one that `speech_coverage`
already separates and one bad alignment, by 30 ms. Nothing detects a bad
alignment today.

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
names and shapes. Anything new is additive. A 200 never carries a null in
`pitch`, `resonance`, or `formants` (#2): `/analyze` refuses a take with fewer
than `MIN_SCORED_PHONES` readings behind pitch or `resonance.balanced`, counted
from `score()`'s `readings`, which leave out the outliers `scored_phone_count`
keeps. The balanced readings are a subset of the other resonance figures'.

`/lexicon` returns `words[].{word,normalized,oovs,in_vocabulary}`. Nothing
reads it yet; treat it the same way once something does.

`/analyze`, `/lexicon`, `/health`, and `/` carry a top-level `version`, read
from `app.__version__` like the OpenAPI schema's (#16). Error bodies don't
carry it. It is bumped by hand, not stamped from the commit at build time,
because neither this service nor the client is live and a version that moves
with every commit is noise during development. It stays in 0.x until launch;
don't bump it before then unless asked. At launch it becomes 1.0.0, and from
then on the PR that makes a change also bumps it: major for a breaking change
to the response or error contract, minor for a new field, endpoint, or code or
for anything that can move a figure or a refusal for the same recording and
transcript (a dictionary-only change included), patch for anything a caller
can't observe. From 1.0.0, tag each bump `vX.Y.Z` on the merged commit on
main. Don't tag a 0.x version: every commit until the next bump reports it,
so the tag would mark only the first of them. Nothing bumps or checks the
version automatically: whether a change can move a score is a judgment call,
and the suite runs in an image without `.git`.

Every error body is `{detail, code, retryable}`, built from `ERRORS` and
`FRAMEWORK_ERROR_CODES` in `app/main.py` (#1). Callers branch on `code` and
`retryable`, so never rename or reuse a code, and keep `retryable: false` for
a verdict on the audio or transcript and nothing else. A new refusal gets its
own exception type and a row in `ERRORS`, not an `HTTPException`; the
contract test in `tests/test_api.py` pins both tables so a change there is
deliberate. Keep the validation 422's `detail` a list and don't move a
status: a client that predates `retryable` reads a 400 or 422 with a string
`detail` as a verdict on the recording and anything else as retryable.
`FRAMEWORK_ERROR_CODES` is a literal map, not a lookup on `HTTPStatus`,
because Python 3.13 renamed some phrases (422 reads "Unprocessable Content")
and the image runs 3.12, while the local `.venv` may be newer. Don't assert
on standard-library text in unit tests; they can pass locally and fail in the
image.

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

Identical `/analyze` requests to the same container can return different
pitch, formant, and resonance figures (#28), so comparing a new image's output
byte for byte against an older container can't show that an aggregation change
left existing figures alone. Recompute `mean`, `median`, and `stdev` from the
same response's `include_phones=1` phones (those with a resonance that aren't
outliers) and check they match it.

To reproduce a worker loss in a running container (#18), kill the aligner process
with `docker exec <container> sh -c 'kill -9 <pid>'`: the image has no `kill` or
`pkill` binary. Find the pid by grepping each `/proc/<pid>/cmdline` for `spawn_main`,
skipping the scanning shell's own `$$`, whose command line contains that string too.
The Praat workers match as well. The aligner is the one with over 1 GB of `VmRSS` in
`/proc/<pid>/status`; a Praat worker holds about 150 MB.

Speech fixtures come from macOS `say`; see `tests/fixtures/README.md`. Synthetic
vowels come from formantry's source-filter model in `tests/conftest.py`.

## Building

```sh
docker buildx build --platform linux/amd64 --load -t voice-metrics-api .
```

amd64 only, for the reasons in the README. The image is about 0.85 GB.
