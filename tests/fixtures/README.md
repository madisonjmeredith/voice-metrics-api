# Fixtures

- `hello.wav`: "Hello there, world." from macOS `say -v Samantha`, converted to
  16 kHz mono PCM. Also shipped as `app/warmup.wav`.
- `hello.TextGrid`: the `mfa align_one` output for it (english_us_arpa v3.0.0).
- `playlist.wav`: "Update the playlist with a decaf vibe." from the same voice
  and conversion. Four of its words are in `dictionary/custom.dict` and not in
  the release dictionary.
- `squirrel.TextGrid`: alignment of a 16 s passage from the same voice. Only
  the TextGrid is kept, as a parser fixture with silences (`<eps>`, `sil`)
  between words.

Regenerate with the Docker image:

```sh
docker run --rm --platform linux/amd64 -v "$PWD/tests/fixtures:/f" voice-metrics-api \
  mfa align_one /f/hello.wav /f/hello.txt \
  /opt/mfa-models/dictionary/english_us_arpa.dict \
  /opt/mfa-models/acoustic/english_us_arpa /f/hello.TextGrid -q
```
