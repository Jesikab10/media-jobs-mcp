# Media technical specs

## ProRes or very high bitrate sources

Browser players cannot play ProRes, and sources above the player bitrate limit stutter.
Action: generate an H.264 proxy for review; keep the original for the final render. Not an error by itself.

## Variable frame rate (VFR) phone footage

Phone recordings often have a variable frame rate, which makes cuts drift out of sync with audio after concatenation.
Action: normalise to a constant frame rate before trimming. Retrying without normalisation reproduces the drift.

## HEVC and 4K sources

HEVC and 4K decode is expensive on CPU. Route these to the GPU encoder when available.
Action: if a CPU encode times out on a 4K HEVC source, reroute to GPU instead of retrying on CPU.

## Missing audio stream

Some sources have no audio track. Concatenating them with clips that do have audio fails or produces silence gaps.
Action: add a silent audio track to audio-less clips before concatenation.
