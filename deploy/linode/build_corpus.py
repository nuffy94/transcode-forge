#!/usr/bin/env python3
"""Build the versioned, open-licensed benchmark corpus in Object Storage.

This is the formal counterpart to ``seed-media.sh``. Where the seed script just
copies a few Blender movies so a fresh deploy has something to transcode, this
builds a *reproducible benchmark corpus*: open-licensed masters, normalized to
realistic consumer h264 (the input the product actually sees), partitioned by
content class, with a manifest so a benchmark run can cite "corpus v1" and
anyone can rebuild the exact same set.

Why re-encode masters we already have? Two reasons. The product's use case is
h264-in at consumer bitrates, so the corpus must *be* that. And a benchmark
needs a uniform, known input baseline across every clip — same profile, pixel
format, and bitrate ceiling — so CPU-vs-GPU numbers compare like with like.
(h264 masters are re-encoded h264->h264; the small generation loss is recorded
and is irrelevant to a downstream HEVC/AV1 transcode comparison.)

Content classes matter: animation (flat, easy) and live-action grain (hard) sit
at opposite ends of how a codec behaves, and grain is exactly where x265 and
NVENC diverge. A benchmark on animation alone would flatter both and mislead.

Sources (all Creative Commons, redistributable with attribution):
  * Blender open movies (CC-BY 3.0) ............ animation
  * Netflix Open Content "Meridian" (CC BY 4.0)  live-action grain, 4K HDR

Runs on a Linux host with ffmpeg (built with libzimg for zscale), rclone, and
curl. On Ubuntu 24.04:  apt-get install -y ffmpeg rclone curl

Bucket credentials come from the environment, same contract as seed-media.sh:
    export S3_ENDPOINT=https://us-ord-1.linodeobjects.com
    export S3_ACCESS_KEY=...
    export S3_SECRET_KEY=...
    ./build_corpus.py --bucket forge-media --scratch /mnt/data/corpus-build

See CORPUS.md for the full design, licenses, and the exact encode recipe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Corpus definition (v1). Edit these two tables to change the corpus, then bump
# CORPUS_VERSION. Masters are downloaded once and shared across their clips.
# --------------------------------------------------------------------------- #

CORPUS_VERSION = "v1"

BLENDER_CREDIT = "(CC) Blender Foundation | blender.org"
NETFLIX_CREDIT = "Meridian (c) Netflix, Inc. | opencontent.netflix.com"


@dataclass(frozen=True)
class Master:
    """A source file downloaded once and reused by one or more clips."""

    url: str
    filename: str
    license: str
    attribution: str
    is_hdr: bool = False


@dataclass(frozen=True)
class Clip:
    """One output corpus clip cut from a master and normalized to h264."""

    clip_id: str  # doubles as the object key suffix: corpus/<ver>/<clip_id>.mp4
    content_class: str
    master: str  # key into MASTERS
    start_s: int
    height: int  # target height; width follows aspect (even)
    duration_s: int = 150


MASTERS: dict[str, Master] = {
    "bbb": Master(
        url="https://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_1080p_h264.mov",
        filename="big_buck_bunny_1080p_h264.mov",
        license="CC-BY 3.0",
        attribution=BLENDER_CREDIT,
    ),
    "sintel": Master(
        url="https://download.blender.org/durian/movies/Sintel.2010.1080p.mkv",
        filename="Sintel.2010.1080p.mkv",
        license="CC-BY 3.0",
        attribution=BLENDER_CREDIT,
    ),
    "tos": Master(
        url="https://download.blender.org/demo/movies/ToS/tears_of_steel_1080p.mov",
        filename="tears_of_steel_1080p.mov",
        license="CC-BY 3.0",
        attribution=BLENDER_CREDIT,
    ),
    # Netflix Open Content, CC BY 4.0. Single 811 MB MP4, 4K 59.94 HDR (P3/PQ);
    # tone-mapped to bt709 SDR below. Path-style S3 URL (the dotted bucket name
    # breaks virtual-host TLS). Anonymous public GET, no credentials needed.
    "meridian": Master(
        url="https://s3.amazonaws.com/download.opencontent.netflix.com/Meridian/Meridian_UHD4k5994_HDR_P3PQ.mp4",
        filename="Meridian_UHD4k5994_HDR_P3PQ.mp4",
        license="CC BY 4.0",
        attribution=NETFLIX_CREDIT,
        is_hdr=True,
    ),
}

CLIPS: list[Clip] = [
    # Animation (Blender) — flat, clean, easy to compress.
    Clip("animation/big_buck_bunny_1080p", "animation", "bbb", start_s=60, height=1080),
    Clip("animation/sintel_1080p", "animation", "sintel", start_s=120, height=1080),
    Clip("animation/tears_of_steel_1080p", "animation", "tos", start_s=120, height=1080),
    # Live-action grain (Meridian) — the codec-divergence stress case. Two 1080p
    # scenes plus one native-4K cut so the bench can measure the resolution axis
    # (NVENC's edge over x265 widens at 4K) on identical content.
    Clip("live_action/meridian_1080p_a", "live-action", "meridian", start_s=90, height=1080),
    Clip("live_action/meridian_1080p_b", "live-action", "meridian", start_s=360, height=1080),
    Clip("live_action/meridian_2160p", "live-action", "meridian", start_s=90, height=2160),
]

# Realistic consumer h264 ceilings per target height (Mbps -> ffmpeg args).
# CRF gives content-adaptive bitrate; maxrate/bufsize cap grain scenes so a
# clip stays in the consumer envelope. See CORPUS.md for the rationale.
ENCODE_BY_HEIGHT: dict[int, dict[str, str]] = {
    1080: {"crf": "20", "maxrate": "16M", "bufsize": "32M"},
    2160: {"crf": "22", "maxrate": "45M", "bufsize": "90M"},
}

X264_PRESET = "medium"

# Robust PQ/HLG -> bt709 SDR tone-map (hable), then resize, then 8-bit 4:2:0.
_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)


# --------------------------------------------------------------------------- #
# Build result accounting
# --------------------------------------------------------------------------- #


@dataclass
class BuiltClip:
    clip: Clip
    output_key: str
    encode_cmd: list[str]
    bytes: int = 0
    sha256: str = ""
    width: int = 0
    height: int = 0
    duration_s: float = 0.0
    bitrate_kbps: int = 0


@dataclass
class ToolVersions:
    ffmpeg: str = "unknown"
    git_sha: str = "unknown"


# --------------------------------------------------------------------------- #
# Shell helpers
# --------------------------------------------------------------------------- #


def run(cmd: list[str], *, dry: bool) -> None:
    """Run a command, streaming its output. In dry-run mode, just print it."""
    printable = " ".join(cmd)
    if dry:
        print(f"  DRY  {printable}")
        return
    print(f"  RUN  {printable}")
    subprocess.run(cmd, check=True)


def require_tools(need_rclone: bool) -> None:
    missing = [t for t in ("ffmpeg", "ffprobe", "curl") if shutil.which(t) is None]
    if need_rclone and shutil.which("rclone") is None:
        missing.append("rclone")
    if missing:
        sys.exit(
            f"error: missing required tool(s): {', '.join(missing)}\n"
            "  Ubuntu: apt-get install -y ffmpeg rclone curl"
        )


def tool_versions() -> ToolVersions:
    v = ToolVersions()
    try:
        head = subprocess.run(
            ["ffmpeg", "-hide_banner", "-version"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        if head:
            v.ffmpeg = head[0].strip()
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        v.git_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return v


# --------------------------------------------------------------------------- #
# Build steps
# --------------------------------------------------------------------------- #


def ensure_master(master: Master, scratch: Path, *, dry: bool) -> Path:
    """Download a master to the scratch dir once; reuse if already present."""
    dest = scratch / master.filename
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  have {master.filename} ({dest.stat().st_size / 1e6:.0f} MB)")
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    run(["curl", "-fL", "--retry", "3", "-o", str(tmp), master.url], dry=dry)
    if not dry:
        tmp.replace(dest)
    return dest


def video_filter(master: Master, height: int) -> str:
    scale = f"scale=-2:{height}:flags=lanczos"
    if master.is_hdr:
        return f"{_TONEMAP},{scale},format=yuv420p"
    return f"{scale},format=yuv420p"


def encode_cmd(clip: Clip, master: Master, src: Path, out: Path) -> list[str]:
    enc = ENCODE_BY_HEIGHT[clip.height]
    return [
        "ffmpeg",
        "-y",
        "-ss",
        str(clip.start_s),
        "-i",
        str(src),
        "-t",
        str(clip.duration_s),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        video_filter(master, clip.height),
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        X264_PRESET,
        "-crf",
        enc["crf"],
        "-maxrate",
        enc["maxrate"],
        "-bufsize",
        enc["bufsize"],
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out),
    ]


def probe(path: Path) -> tuple[int, int, float, int]:
    """Return (width, height, duration_s, bitrate_kbps) for a built clip."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))
    duration = float(fmt.get("duration", 0.0))
    bitrate = int(int(fmt.get("bit_rate", 0)) / 1000)
    return width, height, duration, bitrate


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def configure_rclone_env() -> None:
    """Map the S3_* contract onto rclone's on-the-fly :s3: backend."""
    endpoint = os.environ.get("S3_ENDPOINT")
    access = os.environ.get("S3_ACCESS_KEY")
    secret = os.environ.get("S3_SECRET_KEY")
    if not (endpoint and access and secret):
        sys.exit(
            "error: set S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY "
            "(or pass --no-upload to build locally only)"
        )
    os.environ["RCLONE_S3_PROVIDER"] = "Other"
    os.environ["RCLONE_S3_ENDPOINT"] = endpoint
    os.environ["RCLONE_S3_ACCESS_KEY_ID"] = access
    os.environ["RCLONE_S3_SECRET_ACCESS_KEY"] = secret


def upload(local: Path, bucket: str, key: str, *, dry: bool) -> None:
    run(["rclone", "copyto", str(local), f":s3:{bucket}/{key}", "--progress"], dry=dry)


# --------------------------------------------------------------------------- #
# Manifest + attribution
# --------------------------------------------------------------------------- #


def build_manifest(built: list[BuiltClip], bucket: str, prefix: str, tools: ToolVersions) -> dict:
    return {
        "corpus_version": CORPUS_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "builder_git_sha": tools.git_sha,
        "tools": {"ffmpeg": tools.ffmpeg},
        "bucket": bucket,
        "prefix": prefix,
        "encode_recipe": {
            "codec": "libx264",
            "profile": "high",
            "pix_fmt": "yuv420p",
            "preset": X264_PRESET,
            "by_height": ENCODE_BY_HEIGHT,
            "note": (
                "CRF with a consumer maxrate ceiling; HDR masters tone-mapped "
                "PQ->bt709 (hable). Recipe is reproducible, not bit-exact "
                "(x264 output varies by build/threads)."
            ),
        },
        "clips": [
            {
                "id": b.clip.clip_id,
                "class": b.clip.content_class,
                "key": b.output_key,
                "source": {
                    "master": b.clip.master,
                    "url": MASTERS[b.clip.master].url,
                    "license": MASTERS[b.clip.master].license,
                    "attribution": MASTERS[b.clip.master].attribution,
                },
                "trim": {"start_s": b.clip.start_s, "duration_s": b.clip.duration_s},
                "width": b.width,
                "height": b.height,
                "duration_s": round(b.duration_s, 3),
                "bitrate_kbps": b.bitrate_kbps,
                "bytes": b.bytes,
                "sha256": b.sha256,
                "encode_cmd": " ".join(b.encode_cmd),
            }
            for b in built
        ],
    }


def attribution_md() -> str:
    lines = [
        f"# Benchmark corpus {CORPUS_VERSION} — attribution",
        "",
        "This corpus is derived from openly licensed sources. Derivative clips",
        "were trimmed and re-encoded to consumer h264; see MANIFEST.json for the",
        "exact recipe per clip. Credit the originals as below.",
        "",
    ]
    for key, m in MASTERS.items():
        lines.append(f"- **{key}** — {m.attribution} — {m.license}")
        lines.append(f"  - Source: {m.url}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the benchmark corpus in Object Storage.")
    p.add_argument("--bucket", default="forge-media", help="target bucket (default: forge-media)")
    p.add_argument(
        "--scratch",
        type=Path,
        default=Path("./corpus-build"),
        help="working dir for masters + outputs (default: ./corpus-build)",
    )
    p.add_argument("--only", metavar="CLASS", help="build only one content class")
    p.add_argument("--no-upload", action="store_true", help="build locally, skip the bucket")
    p.add_argument("--force", action="store_true", help="re-encode even if the output exists")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit; no network")
    return p.parse_args(argv)


def selected_clips(only: str | None) -> list[Clip]:
    clips = [c for c in CLIPS if only is None or c.content_class == only]
    if not clips:
        sys.exit(f"error: no clips match class {only!r}")
    return clips


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dry = args.dry_run
    do_upload = not args.no_upload and not dry
    require_tools(need_rclone=do_upload)
    if do_upload:
        configure_rclone_env()

    prefix = f"corpus/{CORPUS_VERSION}/"
    clips = selected_clips(args.only)
    scratch = args.scratch
    out_dir = scratch / "out"
    if not dry:
        out_dir.mkdir(parents=True, exist_ok=True)

    tools = tool_versions() if not dry else ToolVersions()
    print(f"Corpus {CORPUS_VERSION} -> s3://{args.bucket}/{prefix}  ({len(clips)} clips)")
    print(f"ffmpeg: {tools.ffmpeg}\n")

    built: list[BuiltClip] = []
    for clip in clips:
        master = MASTERS[clip.master]
        out_path = out_dir / f"{clip.clip_id}.mp4"
        key = f"{prefix}{clip.clip_id}.mp4"
        print(f"[{clip.clip_id}]  {clip.content_class}  {clip.height}p  <- {clip.master}")
        cmd = encode_cmd(clip, master, scratch / master.filename, out_path)

        if out_path.exists() and not args.force and not dry:
            print(f"  skip encode (exists): {out_path.name}")
        else:
            ensure_master(master, scratch, dry=dry)
            if not dry:
                out_path.parent.mkdir(parents=True, exist_ok=True)
            run(cmd, dry=dry)

        record = BuiltClip(clip=clip, output_key=key, encode_cmd=cmd)
        if not dry:
            record.bytes = out_path.stat().st_size
            record.sha256 = sha256_file(out_path)
            w, h, dur, br = probe(out_path)
            record.width, record.height, record.duration_s, record.bitrate_kbps = w, h, dur, br
            print(f"  built {record.bytes / 1e6:.0f} MB  {w}x{h}  {br} kbps")
        if do_upload:
            upload(out_path, args.bucket, key, dry=dry)
        built.append(record)

    if dry:
        print("\ndry-run complete - no files written.")
        return 0

    manifest = build_manifest(built, args.bucket, prefix, tools)
    manifest_path = out_dir / "MANIFEST.json"
    attribution_path = out_dir / "ATTRIBUTION.md"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    attribution_path.write_text(attribution_md(), encoding="utf-8")
    print(f"\nwrote {manifest_path}")
    if do_upload:
        upload(manifest_path, args.bucket, f"{prefix}MANIFEST.json", dry=dry)
        upload(attribution_path, args.bucket, f"{prefix}ATTRIBUTION.md", dry=dry)

    total = sum(b.bytes for b in built)
    print(f"\nCorpus {CORPUS_VERSION}: {len(built)} clips, {total / 1e6:.0f} MB total.")
    print("Media credits: Blender Foundation (CC-BY 3.0); Netflix Meridian (CC BY 4.0).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
