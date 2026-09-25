# speccheck

**Catch a bad deliverable before the festival does.** `speccheck` probes a
rendered video with `ffprobe`/`ffmpeg` and validates it against a delivery spec —
codec, resolution, frame rate, scan, pixel format, audio layout, sample rate,
and EBU R128 loudness / true peak — printing `PASS` / `WARN` / `FAIL` per
requirement and exiting non-zero on any failure. Drop it at the end of a render
script or in CI.

Deterministic core, **no API keys, no cloud**. (Optional `--from-brief` compiles
a free-text spec with a local Ollama model — that's the only part that touches a
model, and it's off by default.)

## Why

A master that's 29.97 instead of 25, stereo at 44.1 kHz instead of 48, or +2 LU
too hot gets bounced — after you've uploaded 40 GB and gone to bed. `ffprobe`
already knows all of this; `speccheck` just turns "read the JSON and remember the
rules" into one command with a clear verdict.

## Install

Needs `ffmpeg`/`ffprobe` on PATH and Python 3.10+ with `pyyaml`.

```bash
git clone <this repo> && cd speccheck
pip install pyyaml            # the only dependency
./speccheck.py --list-specs
```

Optionally symlink it onto your PATH: `ln -s "$PWD/speccheck.py" ~/bin/speccheck`.

## Use

```bash
# Check a master against a bundled preset
speccheck master.mov --spec festival-prores

# Your own spec, machine-readable, and skip the (slow) loudness pass
speccheck master.mov --spec ./cinexin-delivery.yaml --json --no-loudness

# See what ships
speccheck --list-specs
```

Exit code is `0` on pass (warnings allowed) and `1` if any `FAIL` requirement is
violated, so:

```bash
speccheck out.mov --spec broadcast-h264-1080i25 && aws s3 cp out.mov s3://...
```

### Example

```
speccheck  master.mov
spec: Festival — ProRes 422 HQ, 1080p25, stereo, ~-23 LUFS

  PASS  video_codec     is prores           want one of prores
  FAIL  fps             is 29.97            want 25
  PASS  audio_channels  is 2                want 2
  FAIL  sample_rate     is 44100            want 48000
  PASS  loudness        is -23              want -24..-22

  Result: FAIL  (3 pass, 0 warn, 2 fail)
```

## Bundled specs

Grounded in the real delivery paths documentary/festival work actually uses —
sources below.

| preset | for |
|---|---|
| `fillos-do-vento-dcp-source` | **Project-specific** — the ProRes DCP-source master for the *Fillos do Vento: A Rapa* feature, derived from the film's actual finishing files (1920×1080, 23.976, 2.39 scope, 10-bit Rec.709, PCM 48k). Validated against the real master. |
| `fillos-do-vento-tv` | **Project-specific** — the 52-min TV broadcast master (1080/**25**, ProRes 422 HQ, EBU **R128 -23 LUFS** and TP ≤ -1 as hard fails). The feature's 23.976 master correctly fails frame rate here — it's not TV-ready. |
| `fillos-do-vento-installation` | **Project-specific** — the gallery installation front-wall master (ProRes 422 LT, 10200×1200 17:2, 10-bit Rec.709, 23.976; video-only, sound is separate). Validated against the real export. |
| `fillos-do-vento-vr` | **Project-specific** — the 31-min VR/immersive panorama master (HEVC Main 10, 7344×864 17:2, 10-bit Rec.709, 23.976; video-only, spatial audio separate). Validated against the real export. |
| `filmfreeway-screener` | **Submission upload** — the online screener you send through FilmFreeway (H.264 MP4, ≤1080p, AAC stereo). The path for IDFA, Sundance, SXSW, Thessaloniki Doc, DocsBarcelona, San Sebastián, Visions du Réel… |
| `festival-screening-h264` | Digital screening copy for festivals/cinemas that take H.264 (1080p, ≤20 Mbps, 48k, faststart) |
| `festival-prores` | Exhibition / DCP-source master — ProRes 422 HQ, 1080p**25** (European), Rec709, PCM 48k |
| `festival-prores-24p` | Same master at **24 fps** (DCP standard / international) |
| `broadcast-h264-1080i25` | European broadcast H.264 1080i25, -23 LUFS ±0.5, TP ≤ -1 |
| `web-h264-1080p` | Web/social H.264 MP4, yuv420p, AAC stereo, ~-14 LUFS, faststart |
| `youtube-4k` | YouTube 2160p H.264/HEVC, AAC stereo, ~-14 LUFS |

**Two honest caveats built into these presets:**

- **DCP is a package, not a file.** A Digital Cinema Package is a folder of MXF
  (JPEG 2000) + XML, so `speccheck` can't validate it directly — it validates the
  **ProRes master you make the DCP from** (`festival-prores` / `-24p`).
- **There is no single festival loudness standard.** FilmFreeway doesn't normalise,
  and cinema uses reference level, not EBU R128 — so loudness is a **warning**, not
  a hard fail. Confirm each festival's own tech sheet.

For a **specific festival's sheet**, copy the closest preset and change the
numbers, or paste the sheet into `--from-brief`. Presets are just YAML.

## Writing a spec

```yaml
name: "My client — 1080p50 H.264"
requirements:
  video_codec: { field: video.codec,   op: in,  expected: [h264],  severity: fail }
  width:       { field: video.width,    op: eq,  expected: 1920,    severity: fail }
  fps:         { field: video.fps,      op: eq,  expected: 50, tolerance: 0.01, severity: fail }
  loudness:    { field: loudness.integrated_lufs, op: range, expected: [-24, -22], severity: warn }
```

- **fields** (dotted): `container_all`, `duration`, `bitrate_kbps`, `faststart`
  (mp4/mov moov-atom before mdat — streaming-friendly),
  `video.{codec,profile,width,height,fps,pix_fmt,field_order,dar,sar,bit_depth,color_*}`,
  `audio.{codec,channels,sample_rate,bit_depth,channel_layout}`,
  `loudness.{integrated_lufs,true_peak_dbtp,lra_lu}`.
- **op**: `eq`, `in` (list), `range` (`[lo, hi]`), `min`, `max`.
- **severity**: `fail` (breaks the build) or `warn` (flagged, still passes).
- `tolerance:` adds float wiggle. `optional: true` skips a field that isn't present.

A field the file doesn't expose is reported `WARN` ("couldn't measure"), never a
silent pass.

## Optional: `--from-brief`

```bash
export SPECCHECK_OLLAMA=http://localhost:11434   # or a fleet node
export SPECCHECK_MODEL=qwen3.8:27b
speccheck master.mov --from-brief "1080p25 ProRes, stereo 48k, -23 LUFS, TP under -1"
```

Compiles the sentence into requirements with a local model. If the model isn't
reachable it tells you and you fall back to a preset — the core never needs it.

## Notes

- Loudness reads the whole file (EBU R128), so it's the slow part; `--no-loudness`
  skips it and drops loudness requirements from the report.
- `field_order` from ffprobe is often `unknown` for progressive files; the presets
  treat `unknown` as acceptable for a progressive requirement.
- `samples/` holds ffmpeg-generated test clips (gitignored); regenerate with the
  commands in the tests / this README.

## Sources

The bundled festival/screener presets are grounded in these (checked Sep 2026;
festival tech sheets change per edition — always confirm the current one):

- FilmFreeway — recommended video upload format (H.264 MP4, ≤1080p, AAC stereo):
  <https://filmfreeway.com/help/article/16013/what-format-do-you-recommend-for-video-uploads>
- shortfilm.de — best practice for digital screening copies for festivals/cinemas:
  <https://www.shortfilm.de/en/best-practice-digitale-vorfuehrkopien-fuer-festivals-und-kinos/>
- Filmmaker Magazine — how to deliver your film to a festival (DCP / ProRes 422 HQ):
  <https://filmmakermagazine.com/96184-how-to-deliver-your-film-to-a-festival/>
- Berlinale — technical specifications for festival media (reference for tier festivals):
  <https://www.berlinale.de/en/film-entry/technical-specifications/festival-media.html>

## Test

```bash
python3 -m unittest -q test_speccheck
```
