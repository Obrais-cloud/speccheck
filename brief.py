"""
Optional: compile a free-text delivery brief into a speccheck spec dict using a
local Ollama model. Kept out of the core so speccheck has zero cloud/API deps —
this is only imported when you pass --from-brief.

Config via env:
  SPECCHECK_OLLAMA   base URL   (default http://localhost:11434)
  SPECCHECK_MODEL    model tag  (default llama3.2)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SCHEMA_HINT = """
Return ONLY JSON: {"name": "...", "requirements": { "<id>": {"field","op","expected","severity"} , ... }}.
Valid `field` paths: container_all, duration, bitrate_kbps,
video.codec, video.width, video.height, video.fps, video.pix_fmt, video.field_order, video.dar,
audio.codec, audio.channels, audio.sample_rate, audio.bit_depth,
loudness.integrated_lufs, loudness.true_peak_dbtp, loudness.lra_lu.
Valid `op`: eq | in | range | min | max. `expected` is a value, a list (for `in`),
or a two-item list (for `range`). `severity`: fail | warn.
Codec names are ffmpeg names (h264, hevc, prores, aac, pcm_s16le). Do not invent fields.
"""


def compile_brief(brief: str) -> dict:
    base = os.environ.get("SPECCHECK_OLLAMA", "http://localhost:11434").rstrip("/")
    model = os.environ.get("SPECCHECK_MODEL", "llama3.2")
    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content":
             "You convert a video delivery brief into a strict JSON spec. " + SCHEMA_HINT},
            {"role": "user", "content": brief},
        ],
    }
    req = urllib.request.Request(
        f"{base}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(
            f"--from-brief needs a local Ollama model. Could not reach {base} "
            f"(model '{model}'): {e}. Set SPECCHECK_OLLAMA / SPECCHECK_MODEL, "
            f"or use a bundled --spec preset.") from e

    content = (body.get("message") or {}).get("content", "")
    try:
        spec = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"model did not return valid JSON: {content[:200]}") from e
    spec.setdefault("name", f"from brief: {brief[:60]}")
    if "requirements" not in spec:
        raise RuntimeError("compiled spec has no 'requirements'")
    return spec
