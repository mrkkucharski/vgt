"""Offline coverage for the on-demand track-transcription trigger action's
pure logic (vgt_transcribe_track.lua): selection validation/refusal paths,
item-span arithmetic, program-guess resolution, and the tempo/runtime
preconditions. Rendering and OS-level spawning need a live REAPER (see the
plan's "Requires a human running REAPER" list) and are not covered here.
"""

from pathlib import Path
import os
import subprocess

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
        "reaper = {CountSelectedTracks = function() return 1 end, GetSelectedTrack = function() return {is_vgt = false, name = 'Guitar (stem)'} end}",
        _helpers_prefix(FAKE_COMMON),
        "local track = validated_single_selection()",
        "io.write(track.name)",
    ])
    assert _run(lua_program).stdout == "Guitar (stem)"


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
