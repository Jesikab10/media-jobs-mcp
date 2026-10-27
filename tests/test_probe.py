from pathlib import Path

import pytest

from media_jobs_mcp.probe import ProbeError, parse_ffprobe, resolve_inside_root

FFPROBE_SAMPLE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
        },
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"format_name": "mov,mp4,m4a", "duration": "12.512000", "bit_rate": "8123456"},
}


def test_parse_ffprobe_extracts_specs() -> None:
    specs = parse_ffprobe(FFPROBE_SAMPLE, filename="clip.mp4")
    assert specs.video_codec == "h264"
    assert (specs.width, specs.height) == (1920, 1080)
    assert specs.fps == 29.97
    assert specs.duration_s == 12.512
    assert specs.bitrate_kbps == 8123
    assert specs.has_audio is True


def test_parse_ffprobe_audio_only() -> None:
    raw = {"streams": [{"codec_type": "audio"}], "format": {"format_name": "wav"}}
    specs = parse_ffprobe(raw, filename="vo.wav")
    assert specs.video_codec is None
    assert specs.fps is None
    assert specs.has_audio is True


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "media").mkdir()
    (tmp_path / "secret.txt").write_text("x")
    with pytest.raises(ProbeError, match="outside"):
        resolve_inside_root("../secret.txt", tmp_path / "media")


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="not found"):
        resolve_inside_root("nope.mp4", tmp_path)
