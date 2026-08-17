"""Offline coverage for vgt_common.lua's on-demand-track-transcription helpers
(docs/on-demand-track-transcription-plan.md): JSON encoding, the balanced
`find_json_object`/`splice_json_object` text surgery, the `commit_track_job`
sidecar-commit protocol, shell quoting, and the track-job track marker.
Everything here is pure Lua or real file I/O -- no live REAPER required (see
test_reascript_working_copy.py for the established harness pattern this
mirrors, and the plan's "Offline Lua testing is available and expected").
"""

from pathlib import Path
import json
import os
import subprocess

COMMON_SCRIPT = Path(__file__).parents[1] / "reascript" / "vgt_common.lua"
LUA = os.environ.get("VGT_TEST_LUA", "lua")


def _run(lua_program: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run([LUA, "-", *args], input=lua_program, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result


def _helpers_prefix() -> str:
    """Every local function/value defined in the file, up to (not including)
    the final `run(action)` dispatcher. Defining a function that touches
    `reaper.*` in its body does not itself require `reaper` to exist -- only
    *calling* one does, so this single prefix covers both the pure-Lua
    helpers (JSON encode/decode, splice, shell_quote) and the ones that need
    a `reaper` stub only when actually invoked (commit_track_job,
    add_track_job_track)."""
    script = COMMON_SCRIPT.read_text()
    return script[: script.index("local function run(action)")]


_pure_helpers_prefix = _helpers_prefix
_through_commit_track_job = _helpers_prefix


def test_decode_json_round_trips_through_encode_flat_record() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        "local record = decode_json('{\"a\": 1, \"b\": \"x\", \"c\": true, \"d\": null}')",
        "io.write(encode_flat_record(record))",
    ])
    # decode_json cannot distinguish JSON null from an absent key (documented
    # limitation on record_key_order), so "d" is dropped on the round trip.
    assert _run(lua_program).stdout == '{"a": 1, "b": "x", "c": true}'


def test_encode_flat_record_respects_explicit_key_order() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        "local record = {status = 'done', __key_order = {'status', 'note_count', 'error'}, note_count = 42, error = JSON_NULL}",
        "io.write(encode_flat_record(record))",
    ])
    assert _run(lua_program).stdout == '{"status": "done", "note_count": 42, "error": null}'


def test_encode_json_scalar_handles_every_scalar_type() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        "io.write(encode_json_scalar(25), '|')",
        "io.write(encode_json_scalar(128.4), '|')",
        "io.write(encode_json_scalar(true), '|')",
        "io.write(encode_json_scalar(false), '|')",
        "io.write(encode_json_scalar('Guitar (stem)'), '|')",
        "io.write(encode_json_scalar(nil), '|')",
        "io.write(encode_json_scalar(JSON_NULL))",
    ])
    assert _run(lua_program).stdout == '25|128.4|true|false|"Guitar (stem)"|null|null'


def test_encode_json_scalar_escapes_quotes_and_backslashes() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        r"""io.write(encode_json_scalar('He said "hi"\\bye'))""",
    ])
    assert _run(lua_program).stdout == r'"He said \"hi\"\\bye"'


def test_find_json_object_balances_nested_braces_and_string_contents() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        '''local body = '{"analysis": {"tempo": {"bpm": 120}, "label": "a { b } c"}, "other": 1}' ''',
        "io.write(find_json_object(body, 'analysis'))",
    ])
    assert _run(lua_program).stdout == '{"tempo": {"bpm": 120}, "label": "a { b } c"}'


def test_find_json_object_returns_nil_when_key_absent() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        '''local body = '{"analysis": {}}' ''',
        "io.write(tostring(find_json_object(body, 'track_jobs')))",
    ])
    assert _run(lua_program).stdout == "nil"


def test_splice_json_object_replaces_an_existing_value() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        '''local container = '{"track_jobs": {"a": 1}, "other": 2}' ''',
        "io.write(splice_json_object(container, 'track_jobs', '{\"b\": 2}'))",
    ])
    assert _run(lua_program).stdout == '{"track_jobs": {"b": 2}, "other": 2}'


def test_splice_json_object_inserts_a_missing_key() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        '''local container = '{"other": 2}' ''',
        "local spliced = splice_json_object(container, 'track_jobs', '{}')",
        "io.write(tostring(decode_json(spliced).other), '|', tostring(find_json_object(spliced, 'track_jobs')))",
    ])
    assert _run(lua_program).stdout == "2|{}"


def test_splice_json_object_inserts_into_an_empty_object() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        '''local container = '{}' ''',
        "local spliced = splice_json_object(container, 'track_jobs', '{}')",
        "io.write(tostring(find_json_object(spliced, 'track_jobs')))",
    ])
    assert _run(lua_program).stdout == "{}"


def test_shell_quote_escapes_single_quotes_and_wraps_spaces() -> None:
    lua_program = "\n".join([
        _pure_helpers_prefix(),
        "io.write(shell_quote(\"/Users/marek/Reaper Project/it's here.wav\"))",
    ])
    assert _run(lua_program).stdout == r"""'/Users/marek/Reaper Project/it'\''s here.wav'"""


def test_commit_track_job_writes_a_new_job_and_bumps_generation(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    project.write_text("")
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 19, "generation": 4,
        "config": {"reference_track_guid": "{REF}"},
        "analysis": {"tempo": {"value": {"bpm": 120}}, "track_jobs": {}},
    }))
    lua_program = "\n".join([
        "reaper = {}",
        "function reaper.EnumProjects() return true, arg[1] end",
        _through_commit_track_job(),
        "commit_track_job('job-1', {"
        "  status = 'imported', source_track_name = 'Guitar (stem)', requested_program = 25,"
        "  midi_tempo = 128.4, note_count = 812, error = JSON_NULL,"
        "  __key_order = {'status', 'source_track_name', 'requested_program', 'midi_tempo', 'note_count', 'error'},"
        "})",
    ])
    _run(lua_program, str(project))

    persisted = json.loads(sidecar.read_text())
    assert persisted["generation"] == 5
    assert persisted["analysis"]["track_jobs"]["job-1"] == {
        "status": "imported", "source_track_name": "Guitar (stem)", "requested_program": 25,
        "midi_tempo": 128.4, "note_count": 812, "error": None,
    }
    # Untouched fields survive verbatim.
    assert persisted["config"]["reference_track_guid"] == "{REF}"
    assert persisted["analysis"]["tempo"]["value"]["bpm"] == 120


def test_commit_track_job_preserves_other_jobs_and_is_idempotent_on_rerun(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    project.write_text("")
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 19, "generation": 1,
        "analysis": {"track_jobs": {"job-0": {"status": "imported", "note_count": 3}}},
    }))
    lua_program = "\n".join([
        "reaper = {}",
        "function reaper.EnumProjects() return true, arg[1] end",
        _through_commit_track_job(),
        "commit_track_job('job-1', {status = 'imported', note_count = 5, __key_order = {'status', 'note_count'}})",
    ])
    _run(lua_program, str(project))

    persisted = json.loads(sidecar.read_text())
    assert persisted["analysis"]["track_jobs"]["job-0"]["note_count"] == 3
    assert persisted["analysis"]["track_jobs"]["job-1"]["note_count"] == 5
    assert persisted["generation"] == 2


def test_commit_track_job_refuses_without_an_analyzed_sidecar(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    project.write_text("")
    lua_program = "\n".join([
        "reaper = {}",
        "function reaper.EnumProjects() return true, arg[1] end",
        _through_commit_track_job(),
        "local ok, err = pcall(commit_track_job, 'job-1', {status = 'imported', __key_order = {'status'}})",
        "io.write(tostring(ok), '|', tostring(err))",
    ])
    assert "no .vgt sidecar" in _run(lua_program, str(project)).stdout


def test_add_track_job_track_is_named_vgt_but_not_marked_managed(tmp_path: Path) -> None:
    """The core non-destructive-invariant guard for this feature: a track-job
    result must never be deletable by vgt_initialize.lua's next apply (see
    vgt_common.lua's comment on add_track_job_track for the reasoning)."""
    lua_program = "\n".join([
        "local created = {}",
        "reaper = {}",
        "function reaper.InsertTrackAtIndex(index, want_defaults) end",
        "function reaper.GetTrack(_, index) return created end",
        "function reaper.GetSetMediaTrackInfo_String(track, key, value, set) "
        "  if set then track[key] = value return true, value end return true, track[key] or '' end",
        "function reaper.SetMediaTrackInfo_Value(track, key, value) track[key] = value end",
        _helpers_prefix(),
        "local track = add_track_job_track(3, '[vgt] Guitar (stem) (MT3)', 'job-1')",
        "io.write(track['P_NAME'], '|', tostring(track['P_EXT:vgt_managed']), '|', track['P_EXT:vgt_track_job'], '|', tostring(is_track_job_track(track)))",
    ])
    assert _run(lua_program).stdout == "[vgt] Guitar (stem) (MT3)|nil|job-1|true"
