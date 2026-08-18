"""Offline coverage for vgt_common.lua's on-demand-track-transcription helpers
(docs/on-demand-track-transcription-plan.md): JSON encoding, the balanced
`find_json_object`/`splice_json_object` text surgery, the `commit_track_job`
sidecar-commit protocol, shell quoting, and the track-job track marker.
Everything here is pure Lua or real file I/O -- no live REAPER required (see
test_reascript_working_copy.py for the established harness pattern this
mirrors, and the plan's "Offline Lua testing is available and expected").
"""

from datetime import UTC, datetime
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


def test_parse_iso8601_utc_round_trips_now_regardless_of_local_timezone_or_dst() -> None:
    """Regression test for a real bug: the previous implementation computed
    the local/UTC offset via os.date("*t")/os.date("!*t") pushed back
    through os.time(), which silently missed daylight saving by exactly one
    hour whenever the local zone was currently observing it -- caught for
    real (not hypothetically) running this project's own test suite in a
    CEST (UTC+2) environment in August. Run under several timezones,
    including ones with a DST offset different from the dev machine's, to
    prove the calendar-arithmetic replacement has no such dependence."""
    lua_program = "\n".join([
        "reaper = {}",
        _pure_helpers_prefix(),
        "local now = os.time()",
        "local now_iso = os.date('!%Y-%m-%dT%H:%M:%SZ', now)",
        "io.write(tostring(now - parse_iso8601_utc(now_iso)))",
    ])
    for timezone in ("UTC", "Europe/Warsaw", "America/New_York", "Pacific/Auckland", "Australia/Sydney"):
        result = subprocess.run(
            [LUA, "-"], input=lua_program, text=True, capture_output=True, env={**os.environ, "TZ": timezone},
        )
        assert result.returncode == 0, (timezone, result.stderr)
        assert result.stdout == "0", f"timezone {timezone}: expected 0s drift, got {result.stdout}s"


def test_parse_iso8601_utc_matches_a_known_reference_epoch() -> None:
    lua_program = "\n".join([
        "reaper = {}",
        _pure_helpers_prefix(),
        "io.write(parse_iso8601_utc('2000-01-01T00:00:00Z'))",
    ])
    assert _run(lua_program).stdout == "946684800"  # well-known reference Unix epoch


def test_check_and_import_job_reports_never_started_quickly_not_after_the_full_stale_window(tmp_path: Path) -> None:
    """A job whose spawn itself failed (bad interpreter path, an argument
    error -- see vgt_transcribe_track.lua's as_integer_program, a real prior
    instance) never writes even a "running" status. That must be reported in
    seconds (NEVER_STARTED_SECONDS), not by waiting out the full
    STALE_JOB_SECONDS budget meant for a job that is actually running long
    MT3 inference."""
    project = tmp_path / "song.RPP"
    project.write_text("")
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 19, "generation": 1,
        "analysis": {"stems": {"artifact_namespace": "ns"}, "tempo": {"value": {"bpm": 120}}, "track_jobs": {}},
    }))
    job_dir = tmp_path / "vgt" / "ns" / "track-jobs" / "job-1"
    job_dir.mkdir(parents=True)
    old_timestamp = "2000-01-01T00:00:00Z"  # far more than NEVER_STARTED_SECONDS ago, never started
    (job_dir / "status.json").write_text(json.dumps({
        "job_id": "job-1", "source_track_name": "Guitar", "created_at": old_timestamp,
    }))

    lua_program = "\n".join([
        "reaper = {}",
        "function reaper.EnumProjects() return true, arg[1] end",
        "local messages = {}",
        "function reaper.ShowConsoleMsg(msg) messages[#messages + 1] = msg end",
        _helpers_prefix(),
        "local settled = check_and_import_job('job-1')",
        "io.write(tostring(settled), '|', table.concat(messages))",
    ])
    stdout = _run(lua_program, str(project)).stdout
    settled, _, message = stdout.partition("|")
    assert settled == "true"
    assert "never started" in message
    assert "job-1" in message


def test_check_and_import_job_does_not_report_a_still_starting_job_as_never_started(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    project.write_text("")
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 19, "generation": 1,
        "analysis": {"stems": {"artifact_namespace": "ns"}, "tempo": {"value": {"bpm": 120}}, "track_jobs": {}},
    }))
    job_dir = tmp_path / "vgt" / "ns" / "track-jobs" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text(json.dumps({
        "job_id": "job-1", "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))

    lua_program = "\n".join([
        "reaper = {}",
        "function reaper.EnumProjects() return true, arg[1] end",
        _helpers_prefix(),
        "io.write(tostring(check_and_import_job('job-1')))",
    ])
    assert _run(lua_program, str(project)).stdout == "false"


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


def test_track_job_name_mirrors_the_source_name_verbatim() -> None:
    """The result track's name is the source's own name plus " (MT3)" --
    deliberately not re-prefixed with "[vgt]" (a deviation from the plan's
    original wording, made after seeing the forced-[vgt] name in practice):
    a `[work] Guitar` source produces `[work] Guitar (MT3)`, sitting next to
    whatever the user was actually working on rather than jumping into vgt's
    own managed namespace."""
    lua_program = "\n".join([
        "reaper = {}",
        _helpers_prefix(),
        "io.write(track_job_name('[work] Electric Guitar'), '|')",
        "io.write(track_job_name('[vgt] Guitar (stem)'), '|')",
        "io.write(track_job_name('Plain Track Name'), '|')",
        "io.write(track_job_name(nil), '|')",
        "io.write(track_job_name(''))",
    ])
    assert _run(lua_program).stdout == (
        "[work] Electric Guitar (MT3)|[vgt] Guitar (stem) (MT3)|Plain Track Name (MT3)|Track (MT3)|Track (MT3)"
    )


def _import_finished_job_reaper_stubs() -> str:
    """A minimal live-project stand-in for import_finished_job: one source
    track (index 0) whose I_FOLDERDEPTH the test sets before calling in,
    plus enough of the item/take/sidecar API to reach commit_track_job."""
    return "\n".join([
        "local tracks = {{guid = 'SRC', folder_depth = SOURCE_FOLDER_DEPTH}}",
        "reaper = {}",
        "function reaper.CountTracks() return #tracks end",
        "function reaper.GetTrack(_, idx) return tracks[idx + 1] end",
        "function reaper.GetTrackGUID(t) return t.guid end",
        "function reaper.GetMediaTrackInfo_Value(t, key) if key == 'I_FOLDERDEPTH' then return t.folder_depth or 0 end return 0 end",
        "function reaper.SetMediaTrackInfo_Value(t, key, value) if key == 'I_FOLDERDEPTH' then t.folder_depth = value end end",
        "function reaper.InsertTrackAtIndex(idx, want_defaults) table.insert(tracks, idx + 1, {guid = 'NEW', folder_depth = 0}) end",
        "function reaper.GetSetMediaTrackInfo_String(t, key, value, set) if set then t[key] = value return true, value end return true, t[key] or '' end",
        "function reaper.PCM_Source_CreateFromFile(path) return {path = path} end",
        "function reaper.GetMediaSourceLength(s) return 10 end",
        "function reaper.AddMediaItemToTrack(t) return {track = t} end",
        "function reaper.SetMediaItemInfo_Value(item, key, value) item[key] = value end",
        "function reaper.AddTakeToMediaItem(item) return {item = item} end",
        "function reaper.SetMediaItemTake_Source(take, source) take.source = source end",
        "function reaper.GetItemStateChunk(item, str, isundo) return true, 'IGNTEMPO 0 120.000000 4 4' end",
        "function reaper.SetItemStateChunk(item, chunk, isundo) return true end",
        "function reaper.MarkProjectDirty() end",
        "function reaper.UpdateArrange() end",
        "function reaper.ShowConsoleMsg(msg) end",
        "function reaper.EnumProjects() return true, arg[1] end",
    ])


def test_import_finished_job_reopens_a_closed_folder_so_the_result_stays_nested(tmp_path: Path) -> None:
    """Regression test for a real, user-reported bug: when the source track
    is the last child closing its folder (I_FOLDERDEPTH < 0), inserting the
    result track right after it landed one level shallower, outside that
    folder, instead of nested alongside its source -- confirmed against a
    real project's track panel, not hypothetically."""
    project = tmp_path / "song.RPP"
    project.write_text("")
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 19, "generation": 1,
        "analysis": {"stems": {"artifact_namespace": "ns"}, "track_jobs": {}},
    }))
    job_dir = tmp_path / "vgt" / "ns" / "track-jobs" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "result.mid").write_bytes(b"fake-midi")

    lua_program = "\n".join([
        "SOURCE_FOLDER_DEPTH = -1",
        _import_finished_job_reaper_stubs(),
        _helpers_prefix(),
        "import_finished_job('ns', 'job-1', {"
        "  source_track_guid = 'SRC', source_track_name = '[work] Guitar',"
        "  item_start_s = 0, item_end_s = 10, midi_tempo = 120, note_count = 5,"
        "})",
        "io.write(tracks[1].folder_depth, '|', tracks[2].folder_depth)",
    ])
    assert _run(lua_program, str(project)).stdout == "0|-1"


def test_import_finished_job_leaves_folder_depth_alone_for_a_flat_sibling(tmp_path: Path) -> None:
    project = tmp_path / "song.RPP"
    project.write_text("")
    sidecar = tmp_path / "song.vgt"
    sidecar.write_text(json.dumps({
        "schema_version": 19, "generation": 1,
        "analysis": {"stems": {"artifact_namespace": "ns"}, "track_jobs": {}},
    }))
    job_dir = tmp_path / "vgt" / "ns" / "track-jobs" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "result.mid").write_bytes(b"fake-midi")

    lua_program = "\n".join([
        "SOURCE_FOLDER_DEPTH = 0",
        _import_finished_job_reaper_stubs(),
        _helpers_prefix(),
        "import_finished_job('ns', 'job-1', {"
        "  source_track_guid = 'SRC', source_track_name = '[work] Guitar',"
        "  item_start_s = 0, item_end_s = 10, midi_tempo = 120, note_count = 5,"
        "})",
        "io.write(tracks[1].folder_depth, '|', tracks[2].folder_depth)",
    ])
    assert _run(lua_program, str(project)).stdout == "0|0"
