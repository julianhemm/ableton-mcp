"""Record what Live plays - the master or one track - for a number of bars.

The flow runs here, in the MCP server, so the Remote Script only gets short
main-thread commands:

    listen_prepare -> listen_record_start -> (wait) -> listen_record_stop -> listen_finish

Launch quantization is one bar while recording (set by listen_prepare), so the
recording starts on a bar line; listen_record_stop is sent in the second half
of the last bar - timed by Live's song position, not the wall clock, because a
status round trip takes up to ~0.3 s - and Live ends the recording on the next
bar line. The result is whole bars on a known grid.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Callable, Dict, Union

MAX_BARS = 64
POLL_SECONDS = 0.02
STOP_AT_BAR_FRACTION = 0.5  # send the stop this far into the last bar
FILE_STABLE_SECONDS = 0.3
PREPARE_ATTEMPTS = 4
PREPARE_RETRY_SECONDS = 0.3
BUSY_MESSAGE = "Changes cannot be triggered by notifications"

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


def _prepare(send_command: SendCommand, source: Union[str, int], sleep: Callable[[float], None]) -> Dict[str, Any]:
    """listen_prepare, repeated while Live is busy or the LISTEN track was only just created."""
    for _ in range(PREPARE_ATTEMPTS):
        try:
            prep = send_command("listen_prepare", {"source": source})
        except Exception as e:
            if BUSY_MESSAGE not in str(e):
                raise
            sleep(PREPARE_RETRY_SECONDS)
            continue
        if not prep.get("pending"):
            return prep
        sleep(PREPARE_RETRY_SECONDS)
    raise ListenError("Live stayed busy - could not set up the LISTEN track; try again")


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

    prep = _prepare(send_command, source, sleep)
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
        # Detection lags the real start by less than a bar, so the bar it falls in is the start bar.
        start_beat = math.floor(started["song_time"] / beats + 1e-6) * beats
        stop_beat = start_beat + (bars - 1 + STOP_AT_BAR_FRACTION) * beats
        song_time = started["song_time"]
        deadline = clock() + (stop_beat - song_time) * beat_seconds + 2 * bar_seconds + 2.0
        while song_time < stop_beat:
            if clock() > deadline:
                raise ListenError("Song position stopped advancing while listening")
            remaining = (stop_beat - song_time) * beat_seconds
            sleep(max(POLL_SECONDS, min(1.0, remaining * 0.8)))
            status = send_command("listen_status", ids)
            if not status.get("is_playing", True):
                raise ListenError("Transport stopped while listening")
            song_time = status["song_time"]
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
    result = {
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
    warnings = []
    if abs(recorded_bars - bars) > 0.01:
        warnings.append(f"recorded {recorded_bars:g} bars instead of {bars} - use bars_recorded")
    if prep.get("soloed"):
        warnings.append(
            "soloed: " + ", ".join(prep["soloed"]) + " - everything else was silent in this recording"
        )
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result
