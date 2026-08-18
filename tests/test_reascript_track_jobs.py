"""Offline coverage for the on-demand track-transcription trigger action's
pure logic (vgt_transcribe_track.lua): selection validation/refusal paths,
item-span arithmetic, program-guess resolution, the tempo/runtime
preconditions, and the exact RENDER_* values render_selected_track sets
(verified against a live project's own render-settings persistence -- see
that function's comments). Actually invoking a live render, or the OS-level
spawn, still needs a live REAPER (see the plan's "Requires a human running
REAPER" list) and is not covered here.
"""

from pathlib import Path
import math
import os
import struct
import subprocess
import wave

TRIGGER_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_transcribe_track.lua"
LUA = os.environ.get("VGT_TEST_LUA", "lua")


def _run(lua_program: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run([LUA, "-", *args], input=lua_program, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result


def _helpers_prefix(fake_common: str) -> str:
    """The trigger script's helper functions, with its `dofile` of
    vgt_common.lua stubbed out by a minimal fake -- these helpers only ever
    call a handful of `common.*` functions, so a real REAPER/vgt_common.lua
    load is unnecessary for testing pure arithmetic and refusal paths."""
    script = TRIGGER_SCRIPT.read_text()
    body = script[script.index("local DEFER_WINDOW_SECONDS") : script.index("local function transcribe_selected_track()")]
    return "\n".join([
        "reaper = reaper or {}",
        f"dofile = function(_) return {fake_common} end",
        "local directory = ''",
        "local common = dofile(directory)",
        body,
    ])


FAKE_COMMON = "{ starts_with_vgt = function(t) return t.is_vgt or false end, track_name = function(t) return t.name end }"


def _write_wav_16bit(
    path: Path, *, seconds_of_signal: float, seconds_of_silence: float,
    sample_rate: int = 44100, channels: int = 2, amplitude: float = 0.5,
) -> None:
    """A synthetic WAV: `seconds_of_signal` of an audible 440 Hz tone,
    followed by `seconds_of_silence` of exact digital silence -- either can
    be 0. `amplitude` well above the -60 dBFS trailing-silence threshold by
    default (0.5 ~= -6 dBFS)."""
    signal_frames = int(seconds_of_signal * sample_rate)
    silence_frames = int(seconds_of_silence * sample_rate)
    max_value = 32767
    frames = bytearray()
    for i in range(signal_frames):
        value = int(amplitude * max_value * math.sin(2 * math.pi * 440 * i / sample_rate))
        frames += struct.pack("<h", value) * channels
    frames += struct.pack("<h", 0) * channels * silence_frames
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))


def test_validated_single_selection_refuses_when_nothing_selected() -> None:
    lua_program = "\n".join([
        "reaper = {CountSelectedTracks = function() return 0 end}",
        _helpers_prefix(FAKE_COMMON),
        "local ok, err = pcall(validated_single_selection)",
        "io.write(tostring(ok), '|', tostring(tostring(err):match('Select the track') ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_validated_single_selection_refuses_more_than_one() -> None:
    lua_program = "\n".join([
        "reaper = {CountSelectedTracks = function() return 2 end}",
        _helpers_prefix(FAKE_COMMON),
        "local ok, err = pcall(validated_single_selection)",
        "io.write(tostring(ok), '|', tostring(tostring(err):match('exactly one') ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_validated_single_selection_refuses_a_vgt_owned_track() -> None:
    lua_program = "\n".join([
        "reaper = {CountSelectedTracks = function() return 1 end, GetSelectedTrack = function() return {is_vgt = true, name = '[vgt] Guitar Ref'} end}",
        _helpers_prefix(FAKE_COMMON),
        "local ok, err = pcall(validated_single_selection)",
        "io.write(tostring(ok), '|', tostring(tostring(err):match('owned track') ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_validated_single_selection_accepts_a_lone_non_vgt_track() -> None:
    lua_program = "\n".join([
        "reaper = {CountSelectedTracks = function() return 1 end,"
        " GetSelectedTrack = function() return {is_vgt = false, name = 'Guitar (stem)'} end,"
        " GetMediaTrackInfo_Value = function(t, key) return 0 end}",
        _helpers_prefix(FAKE_COMMON),
        "local track = validated_single_selection()",
        "io.write(track.name)",
    ])
    assert _run(lua_program).stdout == "Guitar (stem)"


def test_validated_single_selection_refuses_a_muted_track() -> None:
    """Regression test for a real bug/gap: the "stems (selected tracks)"
    render mode renders what would actually be audible, so a muted track
    renders as exact, correct silence -- not a render bug, but confusing
    and wasteful (a full render + MT3 inference run) if not caught up front.
    Confirmed for real against a track muted for unrelated editing reasons."""
    lua_program = "\n".join([
        "reaper = {CountSelectedTracks = function() return 1 end,"
        " GetSelectedTrack = function() return {is_vgt = false, name = 'Electric Guitar'} end,"
        " GetMediaTrackInfo_Value = function(t, key) return key == 'B_MUTE' and 1 or 0 end}",
        _helpers_prefix(FAKE_COMMON),
        "local ok, err = pcall(validated_single_selection)",
        "io.write(tostring(ok), '|', tostring(tostring(err):match('is muted') ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_track_item_span_covers_every_item_on_the_track() -> None:
    lua_program = "\n".join([
        "local items = {{position=5, length=3}, {position=1, length=2}, {position=10, length=1}}",
        "reaper = {",
        "  CountTrackMediaItems = function() return #items end,",
        "  GetTrackMediaItem = function(_, i) return items[i + 1] end,",
        "  GetMediaItemInfo_Value = function(item, key) return key == 'D_POSITION' and item.position or item.length end,",
        "}",
        _helpers_prefix(FAKE_COMMON),
        "local start_s, end_s = track_item_span('track')",
        "io.write(start_s, '|', end_s)",
    ])
    # earliest position (1) to the latest item's end (10 + 1 = 11)
    assert _run(lua_program).stdout == "1|11"


def test_track_item_span_refuses_a_track_with_no_items() -> None:
    lua_program = "\n".join([
        "reaper = {CountTrackMediaItems = function() return 0 end}",
        _helpers_prefix(FAKE_COMMON),
        "local ok, err = pcall(track_item_span, 'track')",
        "io.write(tostring(ok), '|', tostring(tostring(err):match('no media items') ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_as_integer_program_renders_a_gfx_showmenu_style_float_without_a_decimal() -> None:
    """Regression test for a real bug: gfx.showmenu's return value crosses
    the REAPER API boundary as a Lua float (e.g. 4.0), so a program number
    derived from it (family.first + choice - 1) was a float too. Left
    uncoerced, `tostring` rendered it as "27.0" on the spawned command line,
    which Python's `argparse(type=int)` rejected outright -- the process
    exited on an argument error before ever reaching `run_track_job`, so the
    job looked exactly like it silently never started at all."""
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "local float_program = 24 + 4.0 - 1",  # mirrors family.first + choice - 1
        "io.write(tostring(float_program), '|', tostring(as_integer_program(float_program)))",
    ])
    stdout = _run(lua_program).stdout
    before, after = stdout.split("|")
    assert before == "27.0"  # the uncoerced float -- proves the bug is real, not hypothetical
    assert after == "27"


def test_family_for_guess_identifies_guitar_and_bass_ranges() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(tostring(family_for_guess(25).first), '-', tostring(family_for_guess(25).last), '|')",
        "io.write(tostring(family_for_guess(24).first), '|')",  # guitar family start
        "io.write(tostring(family_for_guess(31).first), '|')",  # guitar family end
        "io.write(tostring(family_for_guess(33).first), '-', tostring(family_for_guess(33).last), '|')",
        "io.write(tostring(family_for_guess(53)))",  # vocals guess: no family menu
    ])
    assert _run(lua_program).stdout == "24-31|24|24|32-39|nil"


def test_family_menu_labels_lists_every_program_plus_a_manual_escape_hatch() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "local labels = family_menu_labels({first = 24, last = 31})",
        "io.write(#labels, '|', labels[1], '|', labels[2], '|', labels[#labels])",
    ])
    assert _run(lua_program).stdout == (
        "9|24: Acoustic Guitar (nylon)|25: Acoustic Guitar (steel)|Other (enter a GM program number)..."
    )


def test_family_menu_labels_bass_range() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "local labels = family_menu_labels({first = 32, last = 39})",
        "io.write(#labels, '|', labels[1], '|', labels[8])",
    ])
    assert _run(lua_program).stdout == "9|32: Acoustic Bass|39: Synth Bass 2"


def test_guessed_program_for_track_name_matches_known_targets() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(guessed_program_for_track_name('[vgt] Guitar (stem)'), '|')",
        "io.write(guessed_program_for_track_name('[vgt] Bass (stem)'), '|')",
        "io.write(guessed_program_for_track_name('Some Random Track'))",
    ])
    assert _run(lua_program).stdout == "25|33|0"


def test_project_tempo_or_refuse_requires_an_analyzed_bpm() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "local ok, err = pcall(project_tempo_or_refuse, {tempo = {value = nil}})",
        "io.write(tostring(ok), '|', tostring(tostring(err):match('No analyzed tempo') ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_project_tempo_or_refuse_returns_the_bpm_when_present() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(project_tempo_or_refuse({tempo = {value = {bpm = 128.4}}}))",
    ])
    assert _run(lua_program).stdout == "128.4"


FAKE_COMMON_WITH_JSON = (
    "{ starts_with_vgt = function(t) return false end, track_name = function(t) return t.name end,"
    " find_json_object = function(body, key) return nil end, decode_json = function(text) return nil end }"
)


def test_resolve_vgt_runtime_refuses_without_a_runtime_block() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON_WITH_JSON),
        "local ok, err = pcall(resolve_vgt_runtime, '{}')",
        "io.write(tostring(ok), '|', tostring(tostring(err):match(\"run `vgt analyze`\") ~= nil))",
    ])
    assert _run(lua_program).stdout == "false|true"


def test_resolve_vgt_runtime_returns_the_python_executable() -> None:
    fake_common = (
        "{ starts_with_vgt = function(t) return false end, track_name = function(t) return t.name end,"
        " find_json_object = function(body, key) return '{}' end,"
        " decode_json = function(text) return {python_executable = '/usr/bin/python3', console_script = '/usr/bin/vgt'} end }"
    )
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(fake_common),
        "io.write(resolve_vgt_runtime('{}'))",
    ])
    assert _run(lua_program).stdout == "/usr/bin/python3"


def test_spawn_unconditionally_uses_os_execute_not_a_dead_execprocess_fallback() -> None:
    """Regression test for a real bug: an earlier version tried
    `pcall(reaper.ExecProcess, ...)` first and only fell back to `os.execute`
    when that pcall failed -- but pcall succeeds (no Lua-level error) even
    when ExecProcess's underlying process launch silently does nothing, so
    the os.execute fallback was unreachable on any real REAPER install and
    every on-demand transcription job appeared to run forever without ever
    starting. `os.execute` must be the unconditional spawn mechanism."""
    script = TRIGGER_SCRIPT.read_text()
    assert "os.execute(cmdline" in script
    assert "reaper.ExecProcess(" not in script  # only mentioned in prose explaining the fix, never called


def test_gm_program_name_matches_the_standard_gm1_patch_list() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(gm_program_name(0), '|')",
        "io.write(gm_program_name(24), '|')",
        "io.write(gm_program_name(25), '|')",
        "io.write(gm_program_name(33), '|')",
        "io.write(gm_program_name(127))",
    ])
    assert _run(lua_program).stdout == (
        "Acoustic Grand Piano|Acoustic Guitar (nylon)|Acoustic Guitar (steel)|"
        "Electric Bass (finger)|Gunshot"
    )


def test_gm_program_name_has_exactly_128_entries_no_gaps() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "local names = {}",
        "for i = 0, 127 do names[#names + 1] = gm_program_name(i) end",
        "io.write(#names, '|', gm_program_name(128))",
    ])
    assert _run(lua_program).stdout == "128|unknown"


def test_new_job_id_is_a_timestamp_hex_suffix_pair() -> None:
    lua_program = "\n".join([
        "reaper = {time_precise = function() return 12345.678 end}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(new_job_id())",
    ])
    job_id = _run(lua_program).stdout
    timestamp, _, suffix = job_id.partition("-")
    assert timestamp.isdigit()
    assert len(suffix) == 4
    int(suffix, 16)  # raises if not valid hex


def test_render_selected_track_sets_the_full_render_format_blob_not_just_the_fourcc(tmp_path: Path) -> None:
    """Regression test for a real bug: RENDER_FORMAT is not a plain 4-byte
    "evaw" fourCC string -- REAPER's own value (read back from a live
    project via a throwaway diagnostic action) is 7 bytes: "evaw" followed
    by 3 more bytes encoding bit depth and format flags. Writing only the
    first 4 bytes left the rest unset, which produced a technically valid,
    correctly-sized, but genuinely silent WAV render every time -- confirmed
    against a real REAPER project's LUFS meter reading -inf, not a
    hypothetical."""
    job_dir = tmp_path
    (job_dir / "source.wav").write_bytes(b"placeholder")  # file_exists stub below ignores content
    lua_program = "\n".join([
        # A plain last-value-wins map would be clobbered by render_selected_
        # track's own end-of-function restore() call (which re-sets every key
        # back to its saved pre-render value); record every SET call instead
        # and pick out the actual render-time value explicitly.
        "local render_format_values = {}",
        "reaper = {}",
        "function reaper.GetSetProjectInfo_String(id, key, value, set)",
        "  if set then",
        "    if key == 'RENDER_FORMAT' then render_format_values[#render_format_values + 1] = value end",
        "    return true, value",
        "  end",
        "  return true, ''",
        "end",
        "function reaper.GetSetProjectInfo(id, key, value, set) return 0 end",
        "function reaper.Main_OnCommand(id, flag) end",
        "function reaper.file_exists(path) return true end",
        _helpers_prefix(FAKE_COMMON),
        f"render_selected_track({job_dir.as_posix()!r}, 0, 10)",
        "local format = render_format_values[1]",  # the render-time value, before restore() overwrites it
        "io.write(#format, '|', format:sub(1, 4), '|', format:byte(5), '|', format:byte(6), '|', format:byte(7))",
    ])
    assert _run(lua_program).stdout == "7|evaw|24|0|1"


def _rms_check_program(path: Path, expected_duration_s: float) -> str:
    return "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        f"local ok, err = windowed_rms_ok({path.as_posix()!r}, {expected_duration_s})",
        "io.write(tostring(ok), '|', tostring(err))",
    ])


def test_windowed_rms_ok_accepts_a_normal_render(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    _write_wav_16bit(path, seconds_of_signal=4.0, seconds_of_silence=0.0)

    stdout = _run(_rms_check_program(path, 4.0)).stdout
    ok, _, err = stdout.partition("|")
    assert ok == "true", err


def test_windowed_rms_ok_refuses_a_fully_silent_render(tmp_path: Path) -> None:
    """Regression test for a real bug: an earlier implementation (using
    reaper.CreateAudioAccessor) never once refused a real all-zero-sample
    render -- two different genuinely silent renders both sailed straight
    through it to a wasted MT3 inference run."""
    path = tmp_path / "source.wav"
    _write_wav_16bit(path, seconds_of_signal=0.0, seconds_of_silence=4.0)

    stdout = _run(_rms_check_program(path, 4.0)).stdout
    ok, _, err = stdout.partition("|")
    assert ok == "false"
    assert "entirely silent" in err


def test_windowed_rms_ok_refuses_a_render_that_goes_dead_partway_through(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    _write_wav_16bit(path, seconds_of_signal=2.0, seconds_of_silence=18.0)  # dies at 2s of a nominal 20s

    stdout = _run(_rms_check_program(path, 20.0)).stdout
    ok, _, err = stdout.partition("|")
    assert ok == "false"
    assert "trailing silence" in err


def test_windowed_rms_ok_refuses_a_duration_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    _write_wav_16bit(path, seconds_of_signal=4.0, seconds_of_silence=0.0)

    stdout = _run(_rms_check_program(path, 10.0)).stdout  # claims a 10s item span for a 4s render
    ok, _, err = stdout.partition("|")
    assert ok == "false"
    assert "does not match the item span" in err


def test_windowed_rms_ok_refuses_a_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    path.write_bytes(b"not a wav file at all")

    stdout = _run(_rms_check_program(path, 4.0)).stdout
    ok, _, err = stdout.partition("|")
    assert ok == "false"
    assert "not a readable WAV" in err


def test_pcm_sample_decodes_24bit_little_endian_correctly() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(pcm_sample(string.char(0, 0, 0), 1, 3, false), '|')",  # 0
        "io.write(pcm_sample(string.char(0xFF, 0xFF, 0x7F), 1, 3, false), '|')",  # max positive (0x7FFFFF)
        "io.write(pcm_sample(string.char(0x00, 0x00, 0x80), 1, 3, false))",  # min negative (-0x800000)
    ])
    stdout = _run(lua_program).stdout
    zero, max_positive, min_negative = (float(v) for v in stdout.split("|"))
    assert zero == 0.0
    assert abs(max_positive - 1.0) < 1e-6
    assert min_negative == -1.0


def test_le_uint_reads_little_endian_unsigned_integers() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(FAKE_COMMON),
        "io.write(le_uint(string.char(0x01, 0x00), 1, 2), '|')",  # 1
        "io.write(le_uint(string.char(0x00, 0x01), 1, 2), '|')",  # 256
        "io.write(le_uint(string.char(0xFF, 0xFF, 0xFF, 0xFF), 1, 4))",  # 4294967295
    ])
    # le_uint's `256 ^ i` is always a Lua float (even for a whole number), so
    # compare numerically rather than against an exact string rendering.
    values = [int(float(v)) for v in _run(lua_program).stdout.split("|")]
    assert values == [1, 256, 4294967295]
