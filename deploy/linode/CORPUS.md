# Benchmark corpus

The formal, versioned media set the benchmark numbers are computed against.
Built by `build_corpus.py`; consumed by the CPU-vs-GPU A/B and `scripts/bench/`.

This is deliberately separate from `seed-media.sh`. The seed script drops a few
Blender movies into a fresh deploy so there's something to transcode on day one.
The *corpus* is a controlled experiment input: open-licensed, normalized to
realistic consumer h264, partitioned by content class, and pinned to a version
with a manifest — so a result can say "corpus v1" and anyone can reproduce it.

## Why it's built the way it is

- **Consumer h264, re-encoded.** The product's job is h264-in → HEVC/AV1. The
  corpus must *be* consumer h264, and every clip must share one known input
  baseline (profile, pixel format, bitrate ceiling) so CPU and GPU results
  compare like with like. Blender masters are already h264; they're re-encoded
  anyway to normalize them. The h264→h264 generation loss is recorded in the
  manifest and is irrelevant to a downstream HEVC/AV1 comparison.
- **Content classes.** Animation (flat, easy) and live-action grain (hard) sit
  at opposite ends of codec behavior, and grain is exactly where x265 and NVENC
  diverge. A benchmark on animation alone flatters both encoders and misleads.
- **Two resolutions.** Most clips are 1080p (the consumer norm); one native-4K
  live-action cut is included because NVENC's edge over x265 widens at 4K, and
  the bench should measure that axis on identical content.
- **Short representative segments** (150 s default). Bounds per-clip encode time
  so the A/B is affordable and comparable across clips. Tune via `duration_s`.

## Sources & licenses

All Creative Commons, redistributable with attribution. `ATTRIBUTION.md` is
generated into the bucket alongside the clips.

| Master | Class | License | Notes |
| --- | --- | --- | --- |
| Big Buck Bunny, Sintel, Tears of Steel | animation | CC-BY 3.0 | Blender Foundation, h264 1080p |
| Netflix "Meridian" | live-action | CC BY 4.0 | Netflix Open Content; 4K 59.94 **HDR (P3/PQ)**, 811 MB MP4 |

Meridian is tone-mapped PQ→bt709 SDR (hable) and used for three clips (two
1080p scenes + one native-4K cut). **The master ships UNTAGGED** — despite the
filename, its container carries no colorimetry, and zscale fails fast with
`zscale: no path between colorspaces` on untagged input (its `tin`/`pin`/`min`
hints don't help). The builder stamps the HDR10 signal set up front with
`setparams` so the tone-map chain has a defined starting point.

> **v1 caveat (no silent gaps):** live-action is Meridian only, so that class is
> one production sampled three ways. Netflix **El Fuente** (SDR 4K60, CC BY 4.0,
> y4m) is the natural v1.1 addition for live-action diversity — add it to
> `MASTERS`/`CLIPS` and bump `CORPUS_VERSION`.

## Layout in the bucket

```
s3://<bucket>/corpus/v1/
  animation/     big_buck_bunny_1080p.mp4, sintel_1080p.mp4, tears_of_steel_1080p.mp4
  live_action/   meridian_1080p_a.mp4, meridian_1080p_b.mp4, meridian_2160p.mp4
  MANIFEST.json  per-clip: source, license, sha256, bytes, res, duration, bitrate, encode cmd
  ATTRIBUTION.md
```

`report.py` / `economics.py` break `$/100GB` and throughput down per content
class (plus blended), keyed to the manifest's `corpus_version`.

## Encode recipe

`libx264 -profile:v high -pix_fmt yuv420p -preset medium`, consumer bitrate
ceilings per target height:

| Height | CRF | maxrate | bufsize |
| --- | --- | --- | --- |
| 1080p | 20 | 16 Mbps | 32 Mbps |
| 2160p | 22 | 45 Mbps | 90 Mbps |

HDR masters get the tone-map chain prepended:
`setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv`.
Audio is AAC 128k (copied through if present, dropped if absent). `+faststart`
so every clip probes cleanly. Chapters and mov timecode are stripped
(`-map_chapters -1 -write_tmcd 0`): the mp4 muxer re-emits both as
"codec none" data tracks, which break any downstream stream-preserving
encode ("Could not find tag for codec none") — the Blender masters carry
chapters, and Big Buck Bunny a tmcd track.

Reproducible by **recipe**, not bit-for-bit: x264 output varies by build and
thread count. The manifest records the exact command, tool version, and each
clip's sha256 so drift is detectable.

## Running it

On a Linux host with `ffmpeg`, `rclone`, and `curl`. The tone-map needs a
static [BtbN](https://github.com/BtbN/FFmpeg-Builds) ffmpeg build (n7.1+) —
Ubuntu 24.04's distro ffmpeg 6.1 fails the zscale chain outright ("no path
between colorspaces" from its zimg). Put the static `ffmpeg`/`ffprobe` first
on `PATH`; `rclone` and `curl` are fine from `apt-get install -y rclone curl`.

```bash
export S3_ENDPOINT=https://us-ord-1.linodeobjects.com
export S3_ACCESS_KEY=…          # bucket-scoped limited key
export S3_SECRET_KEY=…
cd deploy/linode
./build_corpus.py --bucket forge-media --scratch /mnt/data/corpus-build
```

Downloads ~3 GB of masters once (cached in `--scratch`), encodes 6 clips, and
uploads them plus the manifest. Idempotent: re-runs skip existing outputs
(`--force` to rebuild). Useful flags: `--dry-run` (print the plan, no network),
`--no-upload` (build locally only), `--only animation`.

**In the Phase B session:** run this on the scheduler instance (same region as
the bucket → fast upload) *before* attaching the GPU worker, so the corpus is in
place when the A/B starts. See the Linode deploy + GPU bench brief.
