"""Technical inspection of media files with ffprobe.

Design notes:
- ffprobe runs with an argument list (never a shell string), so file names cannot inject commands.
- Paths are resolved and must stay inside ``media_root``; anything else is rejected before
  touching the filesystem.
- Output is parsed into a typed model so the agent receives stable, validated fields
  instead of raw ffprobe JSON.
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel

from media_jobs_mcp.config import Settings


class ProbeError(Exception):
    """Raised when a file cannot be probed. The message is safe to show to the agent."""


class MediaSpecs(BaseModel):
    filename: str
    container: str
    duration_s: float | None
    video_codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    bitrate_kbps: int | None
    has_audio: bool


def resolve_inside_root(relative_path: str, root: Path) -> Path:
    if chr(0) in relative_path or len(relative_path) > 255:
        raise ProbeError("invalid path")
    root_resolved = root.resolve()
    candidate = (root_resolved / relative_path).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ProbeError("path is outside the allowed media root")
    if not candidate.is_file():
        raise ProbeError("file not found")
    return candidate


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate in {"0/0", "0"}:
        return None
    return round(float(Fraction(rate)), 3)


def parse_ffprobe(raw: dict[str, object], filename: str) -> MediaSpecs:
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})
    assert isinstance(streams, list) and isinstance(fmt, dict)  # noqa: S101 - shape of ffprobe JSON

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = fmt.get("duration")
    bitrate = fmt.get("bit_rate")
    return MediaSpecs(
        filename=filename,
        container=str(fmt.get("format_name", "unknown")),
        duration_s=round(float(duration), 3) if duration else None,
        video_codec=video.get("codec_name") if video else None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        fps=_parse_fps(video.get("avg_frame_rate")) if video else None,
        bitrate_kbps=int(bitrate) // 1000 if bitrate else None,
        has_audio=has_audio,
    )


def probe_file(relative_path: str, settings: Settings) -> MediaSpecs:
    path = resolve_inside_root(relative_path, settings.media_root)
    cmd = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, argument list, no shell
            cmd, capture_output=True, text=True, timeout=settings.ffprobe_timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe timed out") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe is not installed") from exc

    if result.returncode != 0:
        raise ProbeError("ffprobe could not read the file (corrupt or unsupported)")
    try:
        return parse_ffprobe(json.loads(result.stdout), filename=path.name)
    except (ValueError, TypeError) as exc:
        raise ProbeError("ffprobe returned unexpected output") from exc
