# Voice Metrics API

Pitch and resonance scoring for voice training. Upload a recording and the
text that was read; get back pitch statistics, a 0–1 resonance score, and,
if you ask for it, a reading for every phone the aligner found.

It force-aligns the transcript to the audio with [Montreal Forced Aligner](https://montreal-forced-aligner.readthedocs.io),
measures F0 and F1–F3 with Praat at the midpoint of each phone, and scores each
phone against per-phoneme population statistics. Licensed AGPL-3.0; see
`NOTICE` for what is derived and from whom.

## API

### `POST /analyze`

Multipart form.

| Field | Type | Notes |
| --- | --- | --- |
| `audio` | file | Anything ffmpeg decodes; browsers send WebM or MP4 |
| `transcript` | string | The English text that was read aloud |

Query parameter `include_phones=1` adds `words` and `phones` to the response.
Audio must be 1–60 seconds. With `API_TOKEN` set on the server, requests need
`Authorization: Bearer <token>`.

```json
{
  "version": "0.1.0",
  "duration": 4.82,
  "sample_count": 41,
  "phone_count": 41,
  "scored_phone_count": 37,
  "word_count": 9,
  "oovs": [],
  "speech_coverage": 0.78,
  "pitch":     { "mean": 181.2, "median": 178.9, "stdev": 24.1, "min": 121.0, "max": 240.3, "unit": "Hz" },
  "resonance": { "mean": 0.58, "median": 0.57, "stdev": 0.11, "balanced": 0.61 },
  "formants":  { "f1": { "mean": 512.0, "...": "..." }, "f2": { "...": "..." }, "f3": { "...": "..." }, "unit": "Hz" },
  "words":  [ { "word": "hello", "start": 0.31, "end": 0.62 } ],
  "phones": [ { "phone": "HH", "word_index": 0, "start": 0.31, "end": 0.38, "midpoint": 0.345,
                "f0": 176.1, "f1": 512.0, "f2": 1622.4, "f3": 2701.9,
                "z": [-0.3, 0.2, 0.1, 0.0], "resonance": 0.49, "outlier": false } ]
}
```

- `version` is the version of the service that answered. See [Versioning](#versioning) for when it changes.
- `sample_count` and `phone_count` are the same number, the phones measured.
  `scored_phone_count` is how many had reference statistics.
- Every figure in `pitch`, `resonance`, and `formants` is a number. A take with fewer than three phones behind its pitch is refused with `too_little_voicing` instead. One with fewer than three vowels and sonorants behind its resonance is refused with `too_little_speech`. A single phone's `f0` and the formants under `phones` can still be `null`.
- `resonance.balanced` is the figure to use when comparing takes of different sentences or checking one against a fixed target. `mean` and `median` count every scored phone so they follow the sentence's mix of sounds as well as the voice. `balanced` averages each vowel and sonorant's mean score (`L`, `M`, `N`, `NG`, `R`, `W`, `Y`, and the vowels) and leaves out stops, fricatives, and affricates, whose midpoint formants aren't resonance. Across 23 voices with only the text varying, a voice's `median` moved by 0.31 of the spread between voices and its `balanced` by 0.18. Its values spread wider across voices than `median` does so a threshold tuned on `median` won't carry over.
- `oovs` lists transcript words missing from the pronunciation dictionary. They are aligned as unknown and never scored. An accented word is missing only if its unaccented spelling is missing too (see [Accented words](#accented-words)). [`POST /lexicon`](#post-lexicon) checks a word before anything is recorded and [Adding a word](#adding-a-word) covers adding one.
- `speech_coverage` is the share of the clip that sits under aligned speech phones, from 0 to 1. Silence and out-of-vocabulary words don't count toward it, so a transcript with entries in `oovs` reads lower. Check it for audio that didn't match the transcript. `word_count` can't tell you that because the aligner places every in-vocabulary word no matter what the audio holds. In the seven takes measured so far, a real read came back at 0.78 to 0.82 (slow reads included) and tones or looped clips at 0.02 to 0.22. The service reports the number and leaves the floor to you. It can't catch a clip that is dense with speech but has the wrong words (0.73 in the one case measured) because forced alignment has no way to tell which words were said.
- `z` is the phone's F0–F3 as standard deviations from the population mean
  for that phoneme.

It can answer with any of the [errors](#errors).

### `POST /lexicon`

Checks words against the pronunciation dictionary without any audio. Send a JSON body with a `words` list of 1 to 1,000 entries, each up to 200 characters. It takes the same bearer token as `/analyze`.

```json
{ "words": ["Hello", "Siobhán", "Niamh's", "L'Oréal", "..."] }
```

You get one result per entry in the order you sent them:

```json
{
  "version": "0.1.0",
  "words": [
    { "word": "Hello",   "normalized": "hello",    "oovs": [],          "in_vocabulary": true },
    { "word": "Siobhán", "normalized": "siobhan",  "oovs": [],          "in_vocabulary": true },
    { "word": "Niamh's", "normalized": "niamh 's", "oovs": [],          "in_vocabulary": true },
    { "word": "L'Oréal", "normalized": "l' oréal", "oovs": ["oréal"],   "in_vocabulary": false },
    { "word": "...",     "normalized": "",         "oovs": [],          "in_vocabulary": false }
  ]
}
```

- `version` is the service's version, the same as in `/analyze`.
- `word` is the entry as sent.
- `normalized` is the text the aligner would align. It's lowercased and its punctuation is dropped. A word with an apostrophe or a hyphen that the dictionary doesn't have whole is usually split into its parts, so one entry can come back as several words. An accented word the dictionary lacks comes back without its accents when that spelling is known. That's why `Siobhán` is `siobhan` here while `oréal` kept its accent.
- `oovs` lists the parts of `normalized` that the dictionary lacks. They're the strings `/analyze` would report in its own `oovs` and the headwords to add under [Adding a word](#adding-a-word).
- `in_vocabulary` is true when the entry has something to align and nothing in `oovs`. An entry that normalizes to nothing, like `"..."`, is false.

Each entry goes through the tokenizer `/analyze` uses, by itself. An entry can be one word or a whole sentence. A miss isn't an error. The response says whether the dictionary has the word and leaves what to do about it to you.

The check runs in the aligner's worker process. It waits behind an alignment that's in progress or a restart for up to `ALIGN_TIMEOUT`.

It can answer with `unauthorized`, `invalid_request`, `aligner_unavailable`, `aligner_busy`, or `alignment_timeout` from [Errors](#errors).

### `GET /health`

`{"version": "0.1.0", "status": "healthy", "aligner": "ready"}`. The aligner reports `warming`
while its model loads after startup or a restart.

### Versioning

`/analyze`, `/lexicon`, `/health`, and `/` all return a top-level `version`. The OpenAPI schema at `/openapi.json` has the same one. It's a `MAJOR.MINOR.PATCH` number set by hand in `app/__init__.py` and doesn't change with every commit.

The service stays in 0.x until it goes into production. Until then the version makes no promises and changes only when someone bumps it on purpose. Two results with the same 0.x version can still have been scored differently.

From 1.0.0 on, a bump follows these rules:

- Major: a change that breaks the response or the errors, like a field removed or renamed, a code renamed, or a status moved.
- Minor: a new field, endpoint, or error code, or anything that can change a figure or a refusal for the same recording and transcript. A dictionary change counts even when no code changes.
- Patch: anything a caller can't see, like docs, tests, or logging.

Two results with the same major and minor version were scored the same way. Keep `version` with any result you store so you can tell which ones you can compare.

### Errors

Every error is JSON with the same three fields:

```json
{ "detail": "Audio too short. Record at least 1 second.", "code": "audio_too_short", "retryable": false }
```

- `detail` is a message for a person. It's a string, except on a validation `invalid_request`, where it's FastAPI's list of field errors.
- `code` is the key to branch on or to map to your own copy. Codes are never renamed or reused.
- `retryable` is `false` only when the service judged the recording or transcript itself, so the same request will always get the same answer. Anything else can work on another try, once the aligner recovers or once the request is fixed.

An unhandled exception comes back as a plain-text 500 with no `retryable` at all. Treat it as retryable.

| Status | `code` | `retryable` | When |
| --- | --- | --- | --- |
| 400 | `audio_undecodable` | false | ffmpeg can't decode the upload |
| 400 | `audio_too_short` | false | The audio is shorter than 1 second |
| 400 | `audio_too_long` | false | The audio is longer than 60 seconds |
| 400 | `transcript_empty` | false | Nothing in the transcript is left to align after normalization |
| 422 | `alignment_failed` | false | The aligner couldn't align the transcript to the audio, even with a wide beam |
| 422 | `too_little_speech` | false | Fewer than three vowels and sonorants could be scored |
| 422 | `too_little_voicing` | false | Fewer than three phones had a measurable pitch, as in a near-silent take |
| 401 | `unauthorized` | true | The bearer token is missing or wrong |
| 400, 422 | `invalid_request` | true | A field is missing or invalid (422), or the body couldn't be parsed (400) |
| 503 | `aligner_unavailable` | true | The aligner process died or didn't load within `WARMUP_TIMEOUT`, and a new one is starting |
| 503 | `aligner_busy` | true | The aligner didn't reach the request within `ALIGN_TIMEOUT`, as during a restart. Nothing was restarted |
| 504 | `alignment_timeout` | true | The request ran longer than `ALIGN_TIMEOUT` once the aligner reached it, so the aligner was restarted |
| 404 | `not_found` | true | No route at that path |
| 405 | `method_not_allowed` | true | The path exists but not for that method |

## How it works

1. `ffmpeg` transcodes the upload to 16 kHz mono 16-bit PCM WAV.
2. MFA's `english_us_arpa` model aligns the transcript, giving a start and end
   for every word and ARPAbet phone. The model is loaded once, in a dedicated
   worker process, and reused.
3. Praat (via parselmouth) computes a Pitch object (75–600 Hz) and a Burg
   Formant object (5 formants, 5000 Hz ceiling) and is read at each phone's
   midpoint.
4. Readings more than two standard deviations from the clip mean are dropped
   per formant. Each remaining phone's F1–F3 are z-scored against the
   population statistics for that phoneme, combined with fitted weights, and
   mapped to 0–1 with 0.5 at the population mean. Pitch and resonance are
   then summarized across phones. `resonance.balanced` first averages each
   vowel and sonorant label's scores, then averages those.

## Dictionary

The aligner loads one pronunciation dictionary. The image build makes it by appending three files in this order:

1. The `english_us_arpa` release dictionary, downloaded during the build.
2. `dictionary/cmudict.dict`, the words that [CMUdict](https://github.com/cmusphinx/cmudict) has and the release dictionary lacks. `update`, `playlist`, `checkout`, and `liam` are among them.
3. `dictionary/custom.dict`, for words neither of those has.

`oovs` in a response names the words a transcript needed that none of the three had.

`cmudict.dict` is generated, so don't edit it by hand. This rebuilds it from CMUdict at the commit pinned in the script:

```sh
python spike/cmudict_gap.py
```

It keeps the entries whose word is all letters and isn't in the release dictionary. It also rewrites `dictionary/LICENSE.cmudict`, CMUdict's license, which has to stay beside the entries.

### Accented words

None of the three files has an accented headword. When a transcript word is out of vocabulary as typed, the aligner looks it up again without its accents and uses that spelling if the dictionary has it. `José` aligns as `jose`, `Zoë's` as `zoe's`, and `café-crème` as `cafe creme`. It doesn't matter whether an accent arrives as one composed character or as a letter followed by a combining mark.

The unaccented spelling is what you'll see in `words` from `/analyze` and in `normalized` from `/lexicon`. A word that's still unknown keeps its accents in `oovs`, so `L'Oréal` reports `oréal`.

The second lookup has three limits:

1. Only a letter with a separable mark loses anything. `ł`, `ø`, `đ`, `ß`, and `æ` stay as they are. `Łukasz` is out of vocabulary until it has its own entry.
2. Everything between two spaces is looked up together. `Zoë-Zorblax` stays out of vocabulary whole because `zorblax` is unknown.
3. The unaccented spelling can be a different word. `rosé` aligns as `rose` and gets that word's pronunciation, R OW1 Z. Of 76 loanwords and names ending in é or ée, 72 had a known unaccented spelling and 42 of those had no pronunciation ending in EY. `résumé` is fine because `resume` already has one (R EH1 Z AH0 M EY2).

An entry for the accented word in `dictionary/custom.dict` fixes the first and the third. A word the dictionary has as typed is never looked up a second time.

### Adding a word

Add it to `dictionary/custom.dict`. It takes effect on the next image build.

An entry is one line: the word in lowercase, a tab, and its ARPAbet phones separated by single spaces. Every vowel carries a stress digit (0, 1, or 2).

```text
journaling	JH ER1 N AH0 L IH0 NG
journaling	JH ER1 N L IH0 NG
```

- Keep the lines sorted and unique.
- The format has no comments. A line that starts with `#` is read as a word.
- A word can have more than one line, one per pronunciation. The aligner uses whichever fits the audio.
- A word can have accents. Write it with composed characters (NFC), which the checks require. The aligner uses an accented entry before it tries the unaccented spelling, so this is how you fix a word like `rosé` or add one like `łukasz`.
- Add only words the other two files lack. The checks that run without Docker reject a word `cmudict.dict` has. The suite in the image rejects a word the release dictionary has.
- Resonance is scored only for phones that have reference statistics. `AE2` and every `AW` have none. Those phones still count toward `speech_coverage` and the pitch figures.

Run `.venv/bin/pytest` after editing the file. `tests/test_dictionary.py` checks every entry in both files and its format checks run without Docker. They matter because a phone the acoustic model doesn't have loads without an error and then crashes the aligner the first time a transcript uses that word.

## Running it

```sh
docker build --platform linux/amd64 --target base -t voice-metrics-api .
docker run --rm --platform linux/amd64 -p 8080:8080 voice-metrics-api
curl -F audio=@clip.webm -F transcript="Hello there, world." "localhost:8080/analyze?include_phones=1"
curl --json '{"words": ["Hello", "Siobhán"]}' localhost:8080/lexicon
```

`--target base` matters: `test` is the Dockerfile's last stage, so a build
without it produces the test image, with pytest and the suite inside.

To keep a container running in the background and rebuild it after a change, give it a name and replace it each time:

```sh
docker rm -f voice-metrics-api
docker build --platform linux/amd64 --target base -t voice-metrics-api .
docker run -d --name voice-metrics-api --platform linux/amd64 -p 8080:8080 voice-metrics-api
```

The first line stops and removes the old container so the new one can take its name and port 8080. It does nothing on the first run, when there's no container to remove.

The image is amd64 only: `praat-parselmouth` has no arm64 wheels and MFA's
Kaldi build is linux-64. On Apple Silicon the build and the container run
under emulation.

Environment variables: `PORT` (8080), `API_TOKEN` (unset means no auth),
`ALIGN_TIMEOUT` seconds (10), `WARMUP_TIMEOUT` seconds (60), `MODEL_DIR`
(/opt/mfa-models).

## Tests

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt praat-parselmouth praatio numpy
.venv/bin/pytest
```

That runs everything except the aligner integration tests, which need MFA and
run inside the image's `test` stage:

```sh
docker build --platform linux/amd64 --target test -t voice-metrics-api:test .
docker run --rm voice-metrics-api:test pytest -q
```
