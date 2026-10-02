"""listen: server-side flow (MCP_Server/listen.py) and the Remote Script commands.

The flow is driven against a simulated Live with a virtual clock; the Remote
Script commands run against a small fake Live Object Model. No Ableton.
"""

import os

import pytest

from MCP_Server.listen import ListenError, beats_per_bar, run_listen, set_name_from_path
from tests.test_remote_script_bridge import _load_script_module, make_instance


# ------------------------------------------------------------ server-side flow


class FakeLive:
    """Session recording with one-bar launch quantization on a virtual clock."""

    def __init__(self, tmp_path, tempo=120.0, num=4, den=4, song_time=5.3, playing=True):
        self.now = 0.0
        self.tempo, self.num, self.den = tempo, num, den
        self.song_time0 = song_time
        self.playing = playing
        self.calls = []
        self.record_start_beat = None
        self.record_end_beat = None
        self.stop_requested_beat = None
        self.deleted = False
        self.wav = tmp_path / "Samples" / "Recorded" / "LISTEN 0001.wav"
        self.fail_on = None
        self.status_lag_s = 0.0  # each status round trip costs this much time
        self.prepare_script = []  # per listen_prepare call: "busy" or "pending" before the real answer
        self.soloed = []

    # clock
    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now

    @property
    def beat(self):
        return self.song_time0 + self.now * self.tempo / 60.0

    @property
    def bar_beats(self):
        return beats_per_bar(self.num, self.den)

    def _next_bar(self, beat):
        return (int(beat // self.bar_beats) + 1) * self.bar_beats

    def _update(self):
        if self.stop_requested_beat is not None and self.record_end_beat is None:
            end = self._next_bar(self.stop_requested_beat)
            if self.beat >= end:
                self.record_end_beat = end
                self.wav.parent.mkdir(parents=True, exist_ok=True)
                self.wav.write_bytes(b"RIFF" + b"\0" * 100)

    def send(self, command, params):
        self.calls.append(command)
        if command == self.fail_on:
            raise Exception("boom")
        self._update()
        if command == "listen_prepare":
            if self.prepare_script:
                step = self.prepare_script.pop(0)
                if step == "busy":
                    raise Exception("Changes cannot be triggered by notifications. You will need to defer.")
                return {"pending": True}
            if not self.playing:
                raise Exception("Transport is stopped - press play, then listen again")
            return {
                "track_index": 26, "slot_index": 0, "created_track": True,
                "source_name": "master", "input_routing": "Resampling", "input_channel": "",
                "tempo": self.tempo, "signature_numerator": self.num,
                "signature_denominator": self.den, "previous_quantization": 7,
                "soloed": self.soloed,
            }
        if command == "listen_record_start":
            self.record_start_beat = self._next_bar(self.beat)
            return {"fired": True}
        if command == "listen_status":
            self.now += self.status_lag_s
            self._update()
            started = self.record_start_beat is not None and self.beat >= self.record_start_beat
            recording = started and self.record_end_beat is None
            return {"song_time": self.beat, "is_playing": True,
                    "has_clip": started, "is_recording": recording}
        if command == "listen_record_stop":
            self.stop_requested_beat = self.beat
            return {"stopped": True}
        if command == "listen_finish":
            self.deleted = True
            return {"file_path": str(self.wav),
                    "clip_length": self.record_end_beat - self.record_start_beat}
        if command == "listen_abort":
            return {"cleaned": ["stop", "delete", "restore"]}
        raise AssertionError(command)


def _run(live, **kw):
    return run_listen(live.send, sleep=live.sleep, clock=live.clock, **kw)


def test_records_exactly_the_requested_bars_from_a_bar_line(tmp_path):
    live = FakeLive(tmp_path, tempo=123.0)
    result = _run(live, bars=8)
    assert result["bars_recorded"] == 8
    assert result["start_beat"] == 8.0  # next bar line after beat 5.3
    assert result["start_bar"] == 3
    assert result["signature"] == "4/4" and result["tempo"] == 123.0
    assert result["file_path"] == str(live.wav) and result["file_bytes"] > 0
    assert live.deleted


def test_six_eight_counts_three_quarters_per_bar(tmp_path):
    live = FakeLive(tmp_path, num=6, den=8, song_time=0.2)
    result = _run(live, bars=4)
    assert result["bars_recorded"] == 4
    assert result["start_beat"] == 3.0


def test_stop_is_sent_inside_the_last_bar(tmp_path):
    live = FakeLive(tmp_path)
    _run(live, bars=2)
    last_bar_start = live.record_start_beat + live.bar_beats
    assert last_bar_start < live.stop_requested_beat < live.record_start_beat + 2 * live.bar_beats


def test_slow_status_round_trips_still_give_exact_bars(tmp_path):
    for bars in (1, 2, 8):
        live = FakeLive(tmp_path, tempo=98.5, song_time=68.2 + bars)
        live.status_lag_s = 0.3  # measured in Live 10.1 on 2026-10-02
        result = _run(live, bars=bars)
        assert result["bars_recorded"] == bars and "warning" not in result


def test_prepare_waits_for_a_new_track_and_a_busy_live(tmp_path):
    live = FakeLive(tmp_path)
    live.prepare_script = ["pending", "busy"]
    result = _run(live, bars=1)
    assert result["bars_recorded"] == 1
    assert live.calls[:3] == ["listen_prepare"] * 3


def test_prepare_gives_up_when_live_stays_busy(tmp_path):
    live = FakeLive(tmp_path)
    live.prepare_script = ["busy"] * 10
    with pytest.raises(ListenError, match="busy"):
        _run(live, bars=1)
    assert "listen_record_start" not in live.calls


def test_soloed_tracks_are_reported(tmp_path):
    live = FakeLive(tmp_path)
    live.soloed = ["CAL Kick"]
    assert "soloed: CAL Kick" in _run(live, bars=1)["warning"]


def test_stopped_transport_is_reported(tmp_path):
    live = FakeLive(tmp_path, playing=False)
    with pytest.raises(Exception, match="press play"):
        _run(live)


def test_failure_after_prepare_cleans_up(tmp_path):
    live = FakeLive(tmp_path)
    live.fail_on = "listen_record_stop"
    with pytest.raises(Exception, match="boom"):
        _run(live)
    assert live.calls[-1] == "listen_abort"


@pytest.mark.parametrize("bars", [0, 65, 2.5])
def test_bars_are_validated(tmp_path, bars):
    with pytest.raises(ListenError):
        _run(FakeLive(tmp_path), bars=bars)


def test_set_name_from_recorded_path():
    p = os.path.join("D:", os.sep, "Music", "When The Music Is Over Project", "Samples", "Recorded", "a.wav")
    assert set_name_from_path(p) == "When The Music Is Over"
    tmp = os.path.join("C:", os.sep, "Users", "x", "Documents", "Ableton", "Live Recordings",
                       "2026-10-02 153000 Temp Project", "Samples", "Recorded", "a.wav")
    assert set_name_from_path(tmp) == "_unsaved"
    assert set_name_from_path(os.path.join("C:", os.sep, "a.wav")) == "_unsaved"


# ------------------------------------------------------- Remote Script commands


class Routing:
    def __init__(self, name, obj=None):
        self.display_name = name
        self.attached_object = obj


class Clip:
    def __init__(self):
        self.is_recording = False
        self.length = 32.0
        self.file_path = "D:/x/Samples/Recorded/LISTEN.wav"


class Slot:
    def __init__(self):
        self.clip = None
        self.fired = False

    @property
    def has_clip(self):
        return self.clip is not None

    def fire(self):
        self.fired = True

    def stop(self):
        pass

    def delete_clip(self):
        self.clip = None


class Track:
    def __init__(self, name, song):
        self.name = name
        self.solo = False
        self.arm = False
        self.current_monitoring_state = 1
        self.clip_slots = [Slot() for _ in range(4)]
        self._song = song
        self.input_routing_type = Routing("Ext. In")
        self.input_routing_channel = Routing("1/2")

    @property
    def available_input_routing_types(self):
        return [Routing("Ext. In"), Routing("Resampling")] + [
            Routing(t.name, t) for t in self._song.tracks if t is not self]

    @property
    def available_input_routing_channels(self):
        return [Routing("Pre FX"), Routing("Post FX"), Routing("Post Mixer")]


class Song:
    def __init__(self, names, playing=True):
        self.tracks = []
        for n in names:
            self.tracks.append(Track(n, self))
        self.is_playing = playing
        self.tempo = 123.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.clip_trigger_quantization = 7
        self.current_song_time = 12.0

    def create_audio_track(self, index):
        self.tracks.append(Track("27-Audio", self))


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


def _instance(script_module, song):
    inst = make_instance(script_module)
    inst._song = song
    return inst


def test_prepare_appends_listen_track_on_resampling(script_module):
    song = Song(["1 PEAK", "2 RYTM"])
    song.tracks[0].arm = True
    inst = _instance(script_module, song)
    assert inst._listen_prepare("master") == {"pending": True}  # Live: no changes in the creating tick
    assert len(song.tracks) == 3
    r = inst._listen_prepare("master")
    listen = song.tracks[2]
    assert len(song.tracks) == 3
    assert listen.name == "LISTEN" and r["track_index"] == 2 and r["created_track"]
    assert listen.input_routing_type.display_name == "Resampling"
    assert listen.arm and listen.current_monitoring_state == 2
    assert song.tracks[0].arm and not song.tracks[1].arm  # other arm states untouched
    assert song.clip_trigger_quantization == 4 and r["previous_quantization"] == 7
    assert [t.name for t in song.tracks[:2]] == ["1 PEAK", "2 RYTM"]


def test_prepare_reuses_listen_track_and_routes_a_source_track_post_mixer(script_module):
    song = Song(["1 PEAK", "2 RYTM", "LISTEN"])
    inst = _instance(script_module, song)
    r = inst._listen_prepare(1)
    assert len(song.tracks) == 3 and not r["created_track"]
    assert song.tracks[2].input_routing_type.attached_object is song.tracks[1]
    assert r["source_name"] == "2 RYTM" and r["input_channel"] == "Post Mixer"


def test_prepare_lists_soloed_tracks(script_module):
    song = Song(["1 PEAK", "CAL Kick", "LISTEN"])
    song.tracks[1].solo = True
    assert _instance(script_module, song)._listen_prepare(0)["soloed"] == ["CAL Kick"]


def test_prepare_refuses_stopped_transport_and_self_listening(script_module):
    with pytest.raises(Exception, match="press play"):
        _instance(script_module, Song(["1 PEAK"], playing=False))._listen_prepare("master")
    song = Song(["1 PEAK", "LISTEN"])
    with pytest.raises(ValueError):
        _instance(script_module, song)._listen_prepare(1)


def test_commands_only_touch_the_listen_track(script_module):
    song = Song(["1 PEAK", "LISTEN"])
    inst = _instance(script_module, song)
    with pytest.raises(ValueError):
        inst._listen_record_start(0, 0)


def test_finish_returns_file_deletes_clip_and_restores(script_module):
    song = Song(["1 PEAK", "LISTEN"])
    inst = _instance(script_module, song)
    inst._listen_prepare("master")
    inst._listen_record_start(1, 0)
    song.tracks[1].clip_slots[0].clip = Clip()
    r = inst._listen_finish(1, 0, 7)
    assert r["file_path"].endswith("LISTEN.wav") and r["clip_length"] == 32.0
    assert not song.tracks[1].clip_slots[0].has_clip
    assert not song.tracks[1].arm and song.clip_trigger_quantization == 7


def test_abort_never_raises(script_module):
    song = Song(["1 PEAK", "LISTEN"])
    inst = _instance(script_module, song)
    song.tracks[1].arm = True
    song.tracks[1].clip_slots[0].clip = Clip()
    assert set(inst._listen_abort(1, 0, 7)["cleaned"]) == {"stop", "delete", "restore"}
    assert inst._listen_abort(0, 0, 7) == {"cleaned": []}
