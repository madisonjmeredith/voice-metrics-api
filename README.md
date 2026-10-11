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
  "duration": 4.82,
  "sample_count": 41,
  "phone_count": 41,
  "scored_phone_count": 37,
  "word_count": 9,
  "oovs": [],
  "suspicious": false,
  "speech_coverage": 0.78,
  "pitch":     { "mean": 181.2, "median": 178.9, "stdev": 24.1, "min": 121.0, "max": 240.3, "unit": "Hz" },
  "resonance": { "mean": 0.58, "median": 0.57, "stdev": 0.11 },
  "formants":  { "f1": { "mean": 512.0, "...": "..." }, "f2": { "...": "..." }, "f3": { "...": "..." }, "unit": "Hz" },
  "words":  [ { "word": "hello", "start": 0.31, "end": 0.62 } ],
  "phones": [ { "phone": "HH", "word_index": 0, "start": 0.31, "end": 0.38, "midpoint": 0.345,
                "f0": 176.1, "f1": 512.0, "f2": 1622.4, "f3": 2701.9,
                "z": [-0.3, 0.2, 0.1, 0.0], "resonance": 0.49, "outlier": false } ]
}
```

- `sample_count` and `phone_count` are the same number, the phones measured.
  `scored_phone_count` is how many had reference statistics.
- `oovs` lists transcript words missing from the pronunciation dictionary. They are aligned as unknown and never scored. [Adding a word](#adding-a-word) covers adding them.
- `suspicious` is set when an aligned phone runs longer than 0.8 s or a word longer than 2 s. Slow, deliberate reading can trip it and mismatched audio usually won't. The aligner puts audio the transcript doesn't cover into silence instead of stretching a speech label, so `speech_coverage` is the field for that.
- `speech_coverage` is the share of the clip that sits under aligned speech phones, from 0 to 1. Silence and out-of-vocabulary words don't count toward it, so a transcript with entries in `oovs` reads lower. Check it for audio that didn't match the transcript. `word_count` can't tell you that because the aligner places every in-vocabulary word no matter what the audio holds. In the seven takes measured so far, a real read came back at 0.78 to 0.82 (slow reads included) and tones or looped clips at 0.02 to 0.22. The service reports the number and leaves the floor to you. It can't catch a clip that is dense with speech but has the wrong words (0.73 in the one case measured) because forced alignment has no way to tell which words were said.
- `z` is the phone's F0–F3 as standard deviations from the population mean
  for that phoneme.

Errors: `400` for undecodable audio, audio outside 1–60 s, or a transcript
with no alignable words; `401` for a bad token; `422` when alignment fails or
too few phones could be scored; `503` while the aligner restarts; `504` if
alignment times out.

### `GET /health`

`{"status": "healthy", "aligner": "ready"}`. The aligner reports `warming`
while its model loads after startup or a restart.

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
   then summarized across phones.

## Dictionary

The aligner loads one pronunciation dictionary. The image build makes it by appending three files in this order:

1. The `english_us_arpa` release dictionary, downloaded during the build.
2. `dictionary/cmudict.dict`, the 54,684 words that [CMUdict](https://github.com/cmusphinx/cmudict) has and the release dictionary lacks. `update`, `playlist`, `checkout`, and `liam` are among them.
3. `dictionary/custom.dict`, for words neither of those has.

`oovs` in a response names the words a transcript needed that none of the three had.

`cmudict.dict` is generated, so don't edit it by hand. This rebuilds it from CMUdict at the commit pinned in the script:

```sh
python spike/cmudict_gap.py
```

It keeps the entries whose word is all letters and isn't in the release dictionary. It also rewrites `dictionary/LICENSE.cmudict`, CMUdict's license, which has to stay beside the entries.

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
- Add only words the other two files lack. The checks that run without Docker reject a word `cmudict.dict` has. The suite in the image rejects a word the release dictionary has.
- Resonance is scored only for phones that have reference statistics. `AE2` and every `AW` have none. Those phones still count toward `speech_coverage` and the pitch figures.

Run `.venv/bin/pytest` after editing the file. `tests/test_dictionary.py` checks every entry in both files and its format checks run without Docker. They matter because a phone the acoustic model doesn't have loads without an error and then crashes the aligner the first time a transcript uses that word.

## Running it

```sh
docker build --platform linux/amd64 --target base -t voice-metrics-api .
docker run --rm --platform linux/amd64 -p 8080:8080 voice-metrics-api
curl -F audio=@clip.webm -F transcript="Hello there, world." "localhost:8080/analyze?include_phones=1"
```

`--target base` matters: `test` is the Dockerfile's last stage, so a build
without it produces the test image, with pytest and the suite inside.

The image is amd64 only: `praat-parselmouth` has no arm64 wheels and MFA's
Kaldi build is linux-64. On Apple Silicon the build and the container run
under emulation.

Environment variables: `PORT` (8080), `API_TOKEN` (unset means no auth),
`ALIGN_TIMEOUT` seconds (10), `MODEL_DIR` (/opt/mfa-models).

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
