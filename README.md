# voice-metrics-api

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
- `oovs` lists transcript words missing from the pronunciation dictionary.
  They are aligned as unknown and never scored.
- `suspicious` is set when a phone or word came out implausibly long, which
  usually means the audio runs well past the transcript.
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

## Running it

```sh
docker build --platform linux/amd64 -t voice-metrics-api .
docker run --rm -p 8080:8080 voice-metrics-api
curl -F audio=@clip.webm -F transcript="Hello there, world." "localhost:8080/analyze?include_phones=1"
```

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
