# Voice Metrics API

Pitch and resonance scoring for voice training. Upload a recording and the
text that was read; get back pitch statistics, a 0–1 resonance score, how much
of the text a speech recognizer heard, and, if you ask for it, a reading for
every phone the aligner found.

It force-aligns the transcript to the audio with [Montreal Forced Aligner](https://montreal-forced-aligner.readthedocs.io),
measures F0 and F1–F3 with Praat at the midpoint of each phone, and scores each
phone against per-phoneme population statistics. Whisper transcribes the
same audio so the response can say whether the text was actually read.
Licensed AGPL-3.0; see
`NOTICE` for what is derived and from whom. `GET /` links to this repository.
If you run a modified version, point `SOURCE_URL` in `app/main.py` at your own
source.

## API

### `POST /analyze`

Multipart form.

| Field | Type | Notes |
| --- | --- | --- |
| `audio` | file | Anything ffmpeg decodes; browsers send WebM or MP4 |
| `transcript` | string | The English text that was read aloud, up to 4,000 characters |

Query parameter `include_phones=1` adds `words` and `phones` to the response. Audio must be 1–60 seconds, and the whole request can be up to 16 MB. Requests need `Authorization: Bearer <token>` with the server's `API_TOKEN` as the token. A server started with `ALLOW_UNAUTHENTICATED=1` takes them without one (see [Running it](#running-it)).

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
  "speech_span": { "start": 0.01, "end": 4.62 },
  "recognition": { "text": "Hello there, how have you been this past week?", "word_coverage": 1.0, "compared_words": 9 },
  "pitch":     { "mean": 181.2, "median": 178.9, "stdev": 24.1, "min": 121.0, "max": 240.3, "unit": "Hz" },
  "resonance": { "mean": 0.58, "median": 0.57, "stdev": 0.11, "balanced": 0.61 },
  "formants":  { "f1": { "mean": 512.0, "...": "..." }, "f2": { "...": "..." }, "f3": { "...": "..." }, "unit": "Hz" },
  "words":  [ { "word": "hello", "start": 0.31, "end": 0.62, "heard": true } ],
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
- `speech_coverage` is the share of the clip that sits under aligned speech phones, from 0 to 1. Silence and out-of-vocabulary words don't count toward it, so a transcript with entries in `oovs` reads lower. Check it for audio that didn't match the transcript. `word_count` can't tell you that because the aligner places every in-vocabulary word no matter what the audio holds. In the seven takes measured so far, a real read came back at 0.78 to 0.82 (slow reads included) and tones or looped clips at 0.02 to 0.22. The service reports the number and leaves the floor to you. It can't catch a clip that is dense with speech but has the wrong words (0.73 in the one case measured) because forced alignment has no way to tell which words were said. `recognition` covers that case.
- `speech_span` is the stretch of the clip the aligner worked on, in seconds. It starts 0.3 seconds before the first speech a voice activity detector hears and ends 0.3 seconds after the last (never past either end of the clip). Words and phones land only inside it so low-level noise before or after the read can't be labeled as speech. It's `null` when the detector heard no speech at all, like silence, a tone, or a near-silent take. The whole clip is aligned in that case. `duration` and `speech_coverage` always cover the whole clip.
- `recognition` is what a speech recognizer heard in the clip and how much of the transcript that covers. `text` is the recognizer's own transcription. `word_coverage` is the share of the transcript's words (from 0 to 1) that show up in that transcription in the same order. Case, punctuation, contractions, and spelled-out numbers don't count as differences. Words in `oovs` are left out because the recognizer has no reason to spell a name the way you did. Extra words cost nothing. A stumble or a repeated phrase doesn't lower it. `compared_words` is how many transcript words it was measured over after normalization, where a contraction counts as two.
- Use `word_coverage` and `speech_coverage` together. Missing or wrong words lower `word_coverage` and extra speech the transcript doesn't cover lowers `speech_coverage`. In the takes measured so far, the lowest genuine read was 0.667 (a tongue twister the recognizer "corrected") and the next 0.8. A read of a different prompt topped out at 0.5. That was two greetings that share half their words. As with `speech_coverage`, the floor is yours.
- `word_coverage` is `null` when every transcript word is out of vocabulary. `recognition` itself is `null` when the recognizer couldn't answer in time or is restarting. The rest of the response is still scored. Treat a `null` as unmeasured instead of as a mismatch.
- `words` lists the aligned words. `heard` says whether the recognizer heard each one. It's `null` when `recognition` is `null` or when the word normalizes to nothing, like `um`. A phone's `word_index` is its word's position in `words`.
- The scores count every aligned word whether or not it was heard. A take that leaves part of the prompt out is still scored if it aligns, so check `word_coverage` to turn those away. In the measurements so far, partial reads that aligned read 0.31 to 0.72.
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

It can answer with `unauthorized`, `invalid_request`, `upload_too_large`, `aligner_unavailable`, `aligner_busy`, or `alignment_timeout` from [Errors](#errors).

### `GET /health` and `GET /ready`

These are for a platform's health checks. Both return the same body at 200 and at 503:

```json
{ "version": "0.1.0", "status": "healthy", "aligner": "ready", "analyzer": "ready", "recognizer": "ready" }
```

`aligner`, `analyzer`, and `recognizer` each report one of four states:

| State | Meaning |
| --- | --- |
| `ready` | Loaded and taking requests |
| `warming` | Loading after a restart, for up to `WARMUP_TIMEOUT` |
| `stalled` | Still loading after `WARMUP_TIMEOUT` |
| `down` | The load failed or a worker process died |

- `/health` is the liveness check. It answers 503 (`"status": "unhealthy"`) only while the aligner or the analyzer is `stalled` or `down`. A pool leaves those states only when a request arrives and restarts it, so an instance that a router has stopped sending traffic to would stay stuck. A normal reload stays 200 so the check doesn't get an instance replaced in the middle of one.
- `/ready` is the readiness check. It answers 200 (`"status": "ready"`) only when the aligner and the analyzer are `ready` and 503 (`"not_ready"`) otherwise.
- The recognizer's state is in both bodies but never changes either answer. While it's `down` or reloading, `/analyze` still scores takes and returns `recognition: null`. The next request restarts it.

The server doesn't accept connections until all three pools have loaded at startup, about 13 seconds under emulation. If the load is still running after `WARMUP_TIMEOUT`, the server logs which pool didn't load and exits with code 3. It exits the same way before loading anything when it can't tell whether auth is meant to be on (see [Running it](#running-it)). Give the platform's startup check at least `WARMUP_TIMEOUT` before it gives up.

On a single instance, point only the liveness check at the service. A router that follows `/ready` sends nothing during a reload, so requests that would have waited it out fail at the router instead.

### Versioning

`/analyze`, `/lexicon`, `/health`, `/ready`, and `/` all return a top-level `version`. The OpenAPI schema at `/openapi.json` has the same one. It's a `MAJOR.MINOR.PATCH` number set by hand in `app/__init__.py` and doesn't change with every commit.

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
| 400 | `upload_too_large` | false | The request body is larger than 16 MB |
| 400 | `transcript_empty` | false | Nothing in the transcript is left to align after normalization |
| 422 | `alignment_failed` | false | The aligner couldn't align the transcript to the audio, even with a wide beam |
| 422 | `too_little_speech` | false | Fewer than three vowels and sonorants could be scored |
| 422 | `too_little_voicing` | false | Fewer than three phones had a measurable pitch, as in a near-silent take |
| 401 | `unauthorized` | true | The bearer token is missing or wrong |
| 400, 422 | `invalid_request` | true | A field is missing or invalid (422), or the body couldn't be parsed (400) |
| 504 | `decode_timeout` | true | ffmpeg ran longer than 5 seconds on the upload and was stopped |
| 503 | `aligner_unavailable` | true | The aligner process died or didn't load within `WARMUP_TIMEOUT`, and a new one is starting |
| 503 | `aligner_busy` | true | The aligner didn't reach the request within `ALIGN_TIMEOUT`, as during a restart. Nothing was restarted |
| 504 | `alignment_timeout` | true | The request ran longer than `ALIGN_TIMEOUT` once the aligner reached it. The aligner stops a slow alignment itself and is restarted only when it doesn't answer |
| 503 | `analyzer_unavailable` | true | A Praat worker process died and new ones are starting |
| 404 | `not_found` | true | No route at that path |
| 405 | `method_not_allowed` | true | The path exists but not for that method |

## How it works

1. `ffmpeg` transcodes the upload to 16 kHz mono 16-bit PCM WAV. It stops a second past 60 seconds, so a longer upload is refused without being decoded in full.
2. Silero VAD (the copy that ships with faster-whisper) finds where the speech starts and ends. MFA's `english_us_arpa` model then aligns the transcript over that span plus 0.3 seconds on each side, giving a start and end for every word and ARPAbet phone. MFA labels whatever audio it's handed. Without the span, low-level noise before the read gets the first word stretched over it. A loudness threshold can't find the span because a lead-in like that can sit within 25 dB of the speech. When Silero hears no speech, the whole clip is aligned. Both models are loaded once in a dedicated worker process and reused.
3. After alignment, Praat (via parselmouth) computes a Pitch object (75 to 600 Hz) and a Burg Formant object (5 formants, 5000 Hz ceiling) and reads both at each phone's midpoint. It runs in its own pool of worker processes because parselmouth holds Python's GIL for a whole analysis and would stall every other request until it finished.
4. Readings more than two standard deviations from the clip mean are dropped
   per formant. Each remaining phone's F1–F3 are z-scored against the
   population statistics for that phoneme, combined with fitted weights, and
   mapped to 0–1 with 0.5 at the population mean. Pitch and resonance are
   then summarized across phones. `resonance.balanced` first averages each
   vowel and sonorant label's scores, then averages those.
5. While steps 2 to 4 run, Whisper tiny.en (through faster-whisper, in its own worker process) transcribes the clip. It never sees the transcript because prompting it with the expected text would bias it toward hearing that text. Its words and the aligned words both go through Whisper's English text normalizer. `word_coverage` is the share of the aligned words (out-of-vocabulary ones aside) that appear in order in what it heard.

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
docker run --rm --platform linux/amd64 -p 8080:8080 -e ALLOW_UNAUTHENTICATED=1 voice-metrics-api
curl -F audio=@clip.webm -F transcript="Hello there, world." "localhost:8080/analyze?include_phones=1"
curl --json '{"words": ["Hello", "Siobhán"]}' localhost:8080/lexicon
```

`--target base` matters: `test` is the Dockerfile's last stage, so a build
without it produces the test image, with pytest and the suite inside.

`ALLOW_UNAUTHENTICATED=1` turns the bearer token off, which is fine on your own machine and nowhere else. A deployed server gets `API_TOKEN` instead.

The server refuses to start without one or the other. A secret that never made it into the environment stops the deploy instead of opening `/analyze` and `/lexicon` to anyone. It also refuses both at once and any `ALLOW_UNAUTHENTICATED` value but `1`. An empty `API_TOKEN` counts as missing. Each refusal logs the reason and exits with code 3 before anything loads. A server that does start logs `auth=bearer`, or `auth=off` as a warning.

To keep a container running in the background and rebuild it after a change, give it a name and replace it each time:

```sh
docker rm -f voice-metrics-api
docker build --platform linux/amd64 --target base -t voice-metrics-api .
docker run -d --name voice-metrics-api --platform linux/amd64 -p 8080:8080 -e ALLOW_UNAUTHENTICATED=1 voice-metrics-api
```

The first line stops and removes the old container so the new one can take its name and port 8080. It does nothing on the first run, when there's no container to remove.

The image is amd64 only: `praat-parselmouth` has no arm64 wheels and MFA's
Kaldi build is linux-64. On Apple Silicon the build and the container run
under emulation.

Environment variables: `PORT` (8080), `API_TOKEN` (required unless `ALLOW_UNAUTHENTICATED=1`), `ALLOW_UNAUTHENTICATED` (`1` turns auth off), `ALIGN_TIMEOUT` seconds (10), `RECOGNIZE_TIMEOUT` seconds (10), `WARMUP_TIMEOUT` seconds (60), `ANALYZER_WORKERS` Praat processes (2), `RECOGNIZER_THREADS` (2), `MODEL_DIR` (/opt/mfa-models), `WHISPER_MODEL_DIR` (/opt/whisper/tiny.en).

Each Praat process holds about 150 MB. Requests queue for a free one under a burst of long clips, and a host with CPUs to spare can raise `ANALYZER_WORKERS` to get through a burst faster. The recognizer is one process of 210 to 330 MB that uses `RECOGNIZER_THREADS` CPU threads.

Under emulation the recognizer runs about 15 times slower than it does natively because Rosetta can't run the AVX instructions it relies on. A clip longer than about 35 seconds runs past `RECOGNIZE_TIMEOUT` and comes back with `recognition: null`. Add `-e RECOGNIZE_TIMEOUT=30` to the `docker run` when you need the figure for long clips locally. Leave it at 10 wherever a client is waiting. Recognition gets that timeout twice, once to reach its turn and once to run.

## Deploying to Cloud Run

`cloudbuild.yaml` builds the `base` stage on amd64 in Cloud Build and pushes it to Artifact Registry. That skips the emulated build on Apple Silicon and the upload of a 0.85 GB image from your own machine. The steps assume a Google Cloud project with billing on and `gcloud` signed in to it, and they use `us-east4`. Swap in your own region and `PROJECT_ID`.

1. Enable the APIs and create the image repository:

   ```sh
   gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
   gcloud artifacts repositories create voice-metrics-api --repository-format=docker --location=us-east4
   ```

2. Build and push the image, tagged with the commit it came from:

   ```sh
   gcloud builds submit --region us-east4 --config cloudbuild.yaml --substitutions=_TAG=$(git rev-parse --short HEAD) .
   ```

   `_REGION` and `_REPOSITORY` default to `us-east4` and `voice-metrics-api`, so pass them in `--substitutions` too if yours differ. The build takes about four minutes. `.gcloudignore` reuses `.dockerignore`, so the upload leaves out `.git` and `spike/`.

3. Generate the token and store it. Piping it straight into Secret Manager keeps it off your screen and out of your shell history:

   ```sh
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets create voice-metrics-api-token --replication-policy=user-managed --locations=us-east4 --data-file=-
   ```

   A client reads it back with `gcloud secrets versions access latest --secret voice-metrics-api-token`.

4. Create the account the service runs as, with access to that one secret and nothing else:

   ```sh
   gcloud iam service-accounts create voice-metrics-api-run
   gcloud secrets add-iam-policy-binding voice-metrics-api-token \
     --member="serviceAccount:voice-metrics-api-run@PROJECT_ID.iam.gserviceaccount.com" \
     --role=roles/secretmanager.secretAccessor
   ```

5. Deploy:

   ```sh
   gcloud run deploy voice-metrics-api --region us-east4 \
     --image us-east4-docker.pkg.dev/PROJECT_ID/voice-metrics-api/voice-metrics-api:TAG \
     --service-account voice-metrics-api-run@PROJECT_ID.iam.gserviceaccount.com \
     --set-secrets API_TOKEN=voice-metrics-api-token:latest \
     --allow-unauthenticated \
     --cpu 2 --memory 4Gi --execution-environment gen2 --cpu-boost \
     --timeout 60 --concurrency 8 --min-instances 0 --max-instances 2
   ```

`--allow-unauthenticated` lets requests through without a Google sign-in, so the bearer token is the only gate. That holds because the server won't start without `API_TOKEN`: a secret that didn't attach stops the revision from starting instead of opening `/analyze` to anyone.

The rest of the flags come from these measurements on a 2-CPU instance in `us-east4`, with times taken end to end at the client:

- Startup takes about 30 seconds, and 25.6 of them are the aligner loading. The server opens its port only once both pools are ready, so Cloud Run's default TCP startup probe already waits for the right thing.
- Warm, a 5-second clip comes back in about half a second and a 24-second clip in about 1.2 seconds.
- With eight requests in flight on one instance, every 5-second clip came back within 2.2 seconds and every 24-second clip within 4.5. The aligner takes one call at a time, but each call is short, so a queue on one instance costs far less than a second instance's startup. That's why `--concurrency` is 8.
- Memory peaked around 1.5 GB of the 4 GiB with eight long clips in flight. The aligner holds over 1 GB and each Praat worker about 150 MB, and a worker the out-of-memory killer takes shows up as `exitcode=-9` in the logs.
- After scaling to zero, the first request waits out a whole startup: 30.5 seconds end to end, 27.9 of them before the instance was ready. `--min-instances 1` removes that wait for the price of one instance running all the time, and with `--min-instances 0` an idle service costs nothing.

`--timeout 60` leaves room for a startup plus the longest clip the service accepts.

## Logs

The service writes to stderr: a line at startup with the auth mode, one line per request, and a line whenever the aligner, the Praat workers, or the recognizer restart, finish loading, or fail to load. These came from a container whose aligner was killed while idle:

```text
2026-11-07 18:26:17,099 INFO request=job-41 POST /analyze 200 total_ms=199 decode_ms=101 audio_s=1.3 aligner_wait_ms=0 aligner_ms=5 words=2 oovs=1 praat_wait_ms=1 praat_ms=69
2026-11-07 18:26:31,781 WARNING request=f7b8268c8a0bb70d GET /health 503 total_ms=1 aligner=down analyzer=ready
2026-11-07 18:26:31,966 WARNING request=289cb16d8cdb9ea6 aligner restart reason=worker_lost exitcode=-9 uptime_s=40.5
2026-11-07 18:26:31,970 WARNING request=289cb16d8cdb9ea6 POST /analyze 503 code=aligner_unavailable total_ms=161 decode_ms=154 audio_s=1.3 aligner_wait_ms=0
2026-11-07 18:26:41,873 INFO request=- aligner ready load_s=9.9
```

Every line carries `request=` with the ID of the request it came from, or `-` when it didn't come from one. Send an `X-Request-ID` header of 1 to 64 letters, digits, dots, underscores, or hyphens and the service uses yours. Otherwise it generates one. Either way it comes back in the response's `X-Request-ID` header.

A request line has the status, the error `code` if there was one, and the time each stage took in milliseconds:

- `decode_ms`: saving the upload and running ffmpeg
- `aligner_wait_ms`: waiting out a reload and the calls ahead
- `aligner_ms`: the alignment, or the `/lexicon` check
- `praat_wait_ms`: waiting for a free Praat worker
- `praat_ms`: the Praat pass
- `recognizer_wait_ms`: waiting out a recognizer reload and the recognition ahead
- `recognizer_ms`: the recognition

Once the alignment is done, an `/analyze` line also carries `speech_start_s` and `speech_end_s` (the `speech_span` rounded to hundredths) when the detector heard speech.

A scored `/analyze` line also carries `recognition=`. It's `ok` followed by `word_coverage` and `compared_words`, or the reason the figure is null: `recognizer_busy`, `recognition_timeout`, or `recognizer_unavailable`. Those three are reasons on a 200 and never error codes.

A `praat_wait_ms` that keeps growing under load means `ANALYZER_WORKERS` is too low. An unhandled exception adds `error=` with the exception's class. `/health` and `/ready` stay out of the log unless they answer 503.

A restart line names its `reason` (`alignment_timeout`, `recognition_timeout`, `worker_lost`, or `warmup_timeout`) and the `exitcode` of any worker that died:

- `-6`: the process aborted, which is how Kaldi reports a fatal error
- `-9`: the process was killed, as by the out-of-memory killer
- `-11`: a segfault, as from a dictionary phone the acoustic model lacks (see [Adding a word](#adding-a-word))
- `-15`: the pool stopped the process itself after another Praat worker died

No line includes the transcript or its words, what the recognizer heard, out-of-vocabulary words (only their count), the upload's filename, an error's `detail`, or the client's IP, because a transcript can include someone's name. Uvicorn's access log is off since the request line replaces it.

## Tests

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt praat-parselmouth praatio numpy
.venv/bin/pytest
```

That runs everything except the aligner integration tests, which need MFA and
run inside the image's `test` stage:

```sh
docker build --platform linux/amd64 --target test -t voice-metrics-api:test .
docker run --rm voice-metrics-api:test pytest
```

## Updating dependencies

The image installs `env.lock` and `requirements.lock`, not `env.yaml` and `requirements.txt`. You edit the second pair and the locks are generated from them, so a rebuild gets the same conda and pip packages until someone re-solves on purpose.

After editing either input, rewrite the locks:

```sh
python3 spike/lock.py
```

It needs Docker and takes about 30 seconds. It solves the inputs in the Dockerfile's base image. It re-solves `env.lock` only when `env.yaml` has changed, which keeps a `requirements.txt` edit from moving any conda package. To pick up newer conda packages without changing `env.yaml`, delete `env.lock` first.

`.venv/bin/pytest` fails when an input has changed and its lock hasn't. The `.venv` itself installs `requirements.txt` without the lock because the lock is for the image's Linux build.

Then build the `test` stage and run the suite in it, as in [Tests](#tests). Read the `env.lock` and `requirements.lock` diffs before merging because a new version of any of these can change the figures for the same recording:

- `ffmpeg`, which decodes the upload
- `praat-parselmouth`, which takes the pitch and formant readings
- `kaldi`, `kalpy`, and `montreal-forced-aligner`, which do the alignment
- `libblas`, `liblapack`, and `libopenblas`, which Kaldi's matrix code calls
- `faster-whisper`, `ctranslate2`, and `onnxruntime` (in `requirements.lock`), which do the recognition and find the speech span the aligner works on
