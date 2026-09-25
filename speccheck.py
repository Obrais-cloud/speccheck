#!/usr/bin/env python3
"""
speccheck — validate a rendered video against a delivery spec.

A deliverable that fails a festival, broadcaster or platform spec costs a
re-render, a re-upload and sometimes a slot. speccheck probes the actual file
with ffprobe/ffmpeg and checks it against a spec (a YAML preset or your own),
printing PASS / WARN / FAIL per requirement with actual-vs-expected, and exiting
non-zero on any FAIL so it drops into a render script or CI.

Deterministic core: no API keys, no cloud. Optional `--from-brief` turns a
free-text spec into requirements using a local Ollama model.

Usage:
  speccheck <video> --spec festival-prores
  speccheck <video> --spec ./my-spec.yaml --json
  speccheck --list-specs
  speccheck <video> --from-brief "1080p25 ProRes, stereo 48k, -23 LUFS"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    sys.exit("speccheck needs PyYAML:  pip install pyyaml")

SPECS_DIR = Path(__file__).resolve().parent / "specs"

# ── colours (skipped when not a TTY) ────────────────────────────────────────
_TTY = sys.stdout.isatty()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s
GREEN, YELLOW, RED, DIM, BOLD = "32", "33", "31", "2", "1"
MARK = {"PASS": _c(GREEN, "PASS"), "WARN": _c(YELLOW, "WARN"), "FAIL": _c(RED, "FAIL")}


# ── probing ────────────────────────────────────────────────────────────────
def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _fps(v: str | None) -> float | None:
    """avg_frame_rate like '25/1' or '30000/1001' → float."""
    if not v or v == "0/0":
        return None
    try:
        return round(float(Fraction(v)), 4)
    except (ZeroDivisionError, ValueError):
        return None


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def probe(path: str) -> dict:
    """Flatten the ffprobe JSON into the dotted fields specs address."""
    rc, out, err = _run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err.strip() or 'unknown error'}")
    meta = json.loads(out)
    fmt = meta.get("format", {})
    streams = meta.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})

    data: dict = {
        "container": (fmt.get("format_name") or "").split(",")[0] or None,
        "container_all": fmt.get("format_name"),
        "duration": _num(fmt.get("duration")),
        "bitrate_kbps": (_num(fmt.get("bit_rate")) or 0) // 1000 or None,
        "n_video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        "n_audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
    }
    ca = data["container_all"] or ""
    if "mp4" in ca or "mov" in ca:
        data["faststart"] = mp4_faststart(path)
    if v:
        data["video"] = {
            "codec": v.get("codec_name"),
            "profile": v.get("profile"),
            "width": _num(v.get("width")),
            "height": _num(v.get("height")),
            "fps": _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
            "pix_fmt": v.get("pix_fmt"),
            "field_order": v.get("field_order", "unknown"),
            "sar": v.get("sample_aspect_ratio"),
            "dar": v.get("display_aspect_ratio"),
            "color_primaries": v.get("color_primaries"),
            "color_transfer": v.get("color_transfer"),
            "color_space": v.get("color_space"),
            "bit_depth": _num(v.get("bits_per_raw_sample")),
        }
    if a:
        data["audio"] = {
            "codec": a.get("codec_name"),
            "channels": _num(a.get("channels")),
            "channel_layout": a.get("channel_layout"),
            "sample_rate": _num(a.get("sample_rate")),
            "bit_depth": _num(a.get("bits_per_raw_sample")) or _num(a.get("bits_per_sample")),
        }
    return data


def mp4_faststart(path: str) -> bool | None:
    """True if the MP4/MOV `moov` atom sits before `mdat` (streaming/progressive
    download friendly). None for non-MP4 files or if the box order can't be read.
    Reads only the top-level box headers, no ffmpeg needed."""
    order = []
    try:
        with open(path, "rb") as f:
            size_bytes = os.fstat(f.fileno()).st_size
            while f.tell() < size_bytes:
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                btype = header[4:8].decode("latin-1")
                if size == 1:                      # 64-bit largesize
                    size = int.from_bytes(f.read(8), "big")
                    body = size - 16
                elif size == 0:                    # extends to EOF
                    order.append(btype)
                    break
                else:
                    body = size - 8
                order.append(btype)
                if "moov" in order and "mdat" in order:
                    break
                if body < 0:
                    return None
                f.seek(body, os.SEEK_CUR)
    except OSError:
        return None
    if "ftyp" not in order and "moov" not in order:
        return None  # not an ISO-BMFF (mp4/mov) file
    if "moov" not in order or "mdat" not in order:
        return None
    return order.index("moov") < order.index("mdat")


_LOUD_RE = {
    "integrated_lufs": re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS"),
    "lra_lu": re.compile(r"\bLRA:\s*(-?\d+(?:\.\d+)?)\s*LU\b"),
    "true_peak_dbtp": re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS"),
}


def measure_loudness(path: str) -> dict:
    """EBU R128 loudness via ffmpeg. Reads the whole file, so it is opt-in for
    long masters. Returns {} if the file has no audio or ffmpeg fails."""
    # peak=true enables the true-peak meter (off by default in ebur128).
    rc, out, err = _run(["ffmpeg", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"])
    blob = err + out  # ffmpeg prints the summary to stderr
    result: dict = {}
    for key, rx in _LOUD_RE.items():
        found = rx.findall(blob)
        if found:
            result[key] = float(found[-1])  # last = the Summary block
    return result


# ── spec evaluation ─────────────────────────────────────────────────────────
def _get(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _passes(actual, op: str, expected, tol: float) -> bool:
    if actual is None:
        return False
    try:
        if op == "eq":
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                return abs(actual - expected) <= tol
            return str(actual).lower() == str(expected).lower()
        if op == "in":
            exp = [str(e).lower() for e in expected]
            return str(actual).lower() in exp
        if op == "range":
            lo, hi = expected
            return lo - tol <= actual <= hi + tol
        if op == "min":
            return actual >= expected - tol
        if op == "max":
            return actual <= expected + tol
    except TypeError:
        return False
    raise ValueError(f"unknown op: {op}")


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "/".join(str(x) for x in v)
    return str(v)


def check(data: dict, spec: dict) -> list[dict]:
    results = []
    for name, req in (spec.get("requirements") or {}).items():
        field = req["field"]
        op = req.get("op", "eq")
        expected = req.get("expected")
        tol = float(req.get("tolerance", 0))
        severity = req.get("severity", "fail").lower()
        actual = _get(data, field)

        if actual is None and req.get("optional"):
            continue
        ok = _passes(actual, op, expected, tol)
        if ok:
            status = "PASS"
        elif actual is None:
            status = "WARN"  # couldn't measure it — flag, don't hard-fail
        else:
            status = "FAIL" if severity == "fail" else "WARN"

        exp_str = _fmt(expected)
        if op == "range":
            exp_str = f"{_fmt(expected[0])}..{_fmt(expected[1])}"
        elif op == "min":
            exp_str = f"≥ {_fmt(expected)}"
        elif op == "max":
            exp_str = f"≤ {_fmt(expected)}"
        elif op == "in":
            exp_str = "one of " + exp_str

        results.append({
            "check": name, "field": field, "status": status,
            "actual": actual, "expected": exp_str, "severity": severity,
        })
    return results


# ── output ──────────────────────────────────────────────────────────────────
def report(path: str, spec: dict, results: list[dict]) -> int:
    fails = [r for r in results if r["status"] == "FAIL"]
    warns = [r for r in results if r["status"] == "WARN"]
    print(_c(BOLD, f"\nspeccheck  {os.path.basename(path)}"))
    print(_c(DIM, f"spec: {spec.get('name', '(unnamed)')}\n"))

    w = max((len(r["check"]) for r in results), default=8)
    for r in results:
        actual = _fmt(r["actual"])
        line = f"  {MARK[r['status']]}  {r['check']:<{w}}  {_c(DIM, 'is')} {actual:<16} {_c(DIM, 'want')} {r['expected']}"
        print(line)

    verdict = _c(RED, "FAIL") if fails else (_c(YELLOW, "PASS (with warnings)") if warns else _c(GREEN, "PASS"))
    print(f"\n  {_c(BOLD, 'Result:')} {verdict}  "
          f"({len(results) - len(fails) - len(warns)} pass, {len(warns)} warn, {len(fails)} fail)\n")
    return 1 if fails else 0


def load_spec(ref: str) -> dict:
    p = Path(ref)
    if not p.exists():
        p = SPECS_DIR / (ref if ref.endswith((".yaml", ".yml")) else f"{ref}.yaml")
    if not p.exists():
        sys.exit(f"spec not found: {ref}  (try --list-specs)")
    spec = yaml.safe_load(p.read_text())
    if not isinstance(spec, dict) or "requirements" not in spec:
        sys.exit(f"invalid spec: {p} (needs a 'requirements' mapping)")
    return spec


def list_specs() -> None:
    print(_c(BOLD, "Available specs:"))
    for p in sorted(SPECS_DIR.glob("*.yaml")):
        spec = yaml.safe_load(p.read_text()) or {}
        print(f"  {_c(GREEN, p.stem):<28} {spec.get('name', '')}")


def spec_from_brief(brief: str) -> dict:
    """Best-effort: compile a free-text delivery brief into a spec via a local
    Ollama model. Falls back to a clear error if the fleet is unreachable."""
    from brief import compile_brief  # local, optional
    return compile_brief(brief)


def main() -> int:
    ap = argparse.ArgumentParser(prog="speccheck", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", help="video file to check")
    ap.add_argument("--spec", help="preset name (see --list-specs) or path to a .yaml")
    ap.add_argument("--from-brief", metavar="TEXT",
                    help="compile a free-text spec via a local Ollama model")
    ap.add_argument("--no-loudness", action="store_true",
                    help="skip the EBU R128 loudness pass (faster on long files)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--list-specs", action="store_true", help="list bundled specs and exit")
    args = ap.parse_args()

    if args.list_specs:
        list_specs()
        return 0
    if not args.video:
        ap.error("a video file is required (or use --list-specs)")
    if not Path(args.video).exists():
        sys.exit(f"no such file: {args.video}")
    if not args.spec and not args.from_brief:
        ap.error("give a spec with --spec or --from-brief")

    spec = spec_from_brief(args.from_brief) if args.from_brief else load_spec(args.spec)

    if args.no_loudness:
        spec = {**spec, "requirements": {
            k: v for k, v in (spec.get("requirements") or {}).items()
            if not str(v.get("field", "")).startswith("loudness.")}}

    data = probe(args.video)
    needs_loudness = any(str(r.get("field", "")).startswith("loudness.")
                         for r in (spec.get("requirements") or {}).values())
    if needs_loudness:
        data["loudness"] = measure_loudness(args.video)

    results = check(data, spec)

    if args.json:
        print(json.dumps({
            "file": args.video, "spec": spec.get("name"),
            "probe": data, "results": results,
            "ok": not any(r["status"] == "FAIL" for r in results),
        }, indent=2, default=str))
        return 1 if any(r["status"] == "FAIL" for r in results) else 0

    return report(args.video, spec, results)


if __name__ == "__main__":
    sys.exit(main())
