"""Record what Live plays - the master or one track - for a number of bars.

The flow runs here, in the MCP server, so the Remote Script only gets short
main-thread commands:

    listen_prepare -> listen_record_start -> (wait) -> listen_record_stop -> listen_finish

Launch quantization is one bar while recording (set by listen_prepare), so the
recording starts on a bar line; listen_record_stop is sent half a beat before
the last bar ends and Live ends the recording on that bar line. The result is
whole bars on a known grid.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Callable, Dict, Union

MAX_BARS = 64
POLL_SECONDS = 0.02
STOP_LEAD_BEATS = 0.5
FILE_STABLE_SECONDS = 0.3

SendCommand = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class ListenError(Exception):
    """A listen run failed; the message is meant for the user."""


def beats_per_bar(numerator: int, denominator: int) -> float:
    """Live's tempo counts quarter notes; 6/8 has three of them per bar."""
    return numerator * 4.0 / denominator


def _wait_for(
    send_command: SendCommand,
    ids: Dict[str, int],
    done: Callable[[Dict[str, Any]], bool],
    timeout: float,
    what: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Dict[str, Any]:
    deadline = clock() + timeout
    while True:
        status = send_command("listen_status", ids)
        if done(status):
            return status
        if not status.get("is_playing", True):
            raise ListenError("Transport stopped while listening")
        if clock() >= deadline:
            raise ListenError(f"Timed out waiting for the recording to {what}")
        sleep(POLL_SECONDS)


def _wait_file_stable(path: str, sleep: Callable[[float], None], timeout: float = 10.0) -> int:
    """Wait until Live has finished writing the WAV (size unchanged for a moment)."""
    waited = 0.0
    last = -1
    while waited < timeout:
        size = os.path.getsize(path) if os.path.exists(path) else -1
        if size > 0 and size == last:
            return size
        last = size
        sleep(FILE_STABLE_SECONDS)
        waited += FILE_STABLE_SECONDS
    raise ListenError(f"Recorded file did not appear or kept changing: {path}")


def set_name_from_path(file_path: str) -> str:
    """Live writes recordings to <Live project>/Samples/Recorded/<file>.wav."""
    parts = os.path.normpath(file_path).split(os.sep)
    if "Live Recordings" in parts:  # an unsaved set records into Live's temporary project
        return "_unsaved"
    if len(parts) >= 4 and parts[-2] == "Recorded" and parts[-3] == "Samples":
        name = parts[-4]
        return name[: -len(" Project")] if name.endswith(" Project") else name
    return "_unsaved"


def run_listen(
    send_command: SendCommand,
    bars: int = 8,
    source: Union[str, int] = "master",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Record `bars` bars of `source` ("master" or a track index); return file and timing."""
    if not isinstance(bars, int) or bars < 1 or bars > MAX_BARS:
        raise ListenError(f"bars must be a whole number from 1 to {MAX_BARS}")
    if source != "master":
        try:
            source = int(source)
        except (TypeError, ValueError):
            raise ListenError('source must be "master" or a track index') from None

    prep = send_command("listen_prepare", {"source": source})
    ids = {"track_index": prep["track_index"], "slot_index": prep["slot_index"]}
    restore = {**ids, "previous_quantization": prep.get("previous_quantization")}
    numerator = int(prep["signature_numerator"])
    denominator = int(prep["signature_denominator"])
    tempo = float(prep["tempo"])
    beats = beats_per_bar(numerator, denominator)
    beat_seconds = 60.0 / tempo
    bar_seconds = beats * beat_seconds

    try:
        send_command("listen_record_start", ids)
        started = _wait_for(
            send_command, ids, lambda s: s.get("is_recording"), 2 * bar_seconds + 2.0, "start", sleep, clock
        )
        started_at = clock()
        start_beat = math.floor(started["song_time"] / beats + 1e-6) * beats

        stop_after = bars * bar_seconds - STOP_LEAD_BEATS * beat_seconds
        while clock() - started_at < stop_after:
            sleep(max(POLL_SECONDS, min(0.25, stop_after - (clock() - started_at))))
        send_command("listen_record_stop", ids)
        _wait_for(
            send_command, ids, lambda s: s.get("has_clip") and not s.get("is_recording"),
            bar_seconds + 2.0, "stop", sleep, clock,
        )
        finished = send_command("listen_finish", restore)
    except Exception:
        try:
            send_command("listen_abort", restore)
        except Exception:
            pass
        raise

    file_path = finished["file_path"]
    size = _wait_file_stable(file_path, sleep)
    recorded_bars = float(finished["clip_length"]) / beats
    return {
        "file_path": file_path,
        "file_bytes": size,
        "set_name": set_name_from_path(file_path),
        "source": prep["source_name"],
        "input": f'{prep["input_routing"]} / {prep["input_channel"]}',
        "tempo": tempo,
        "signature": f"{numerator}/{denominator}",
        "bars_requested": bars,
        "bars_recorded": round(recorded_bars, 3),
        "start_beat": start_beat,
        "start_bar": int(start_beat // beats) + 1,
        "created_track": bool(prep.get("created_track")),
        "track_index": ids["track_index"],
    }
