-- vgt: transcribe the selected track (MT3, on-demand) for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP
-- is open, with exactly one track selected. It renders that track's audio,
-- captures the selection's geometry, spawns a detached background MT3 job,
-- and starts a bounded poll that auto-imports the result when it finishes.
-- See docs/on-demand-track-transcription-plan.md for the full design.
--
-- Two REAPER API details below are explicitly unverified spikes (see the
-- plan's "Open questions" #2): the RENDER_SETTINGS/RENDER_BOUNDSFLAG bit
-- values for a "stems, selected track, custom time bounds" render, and
-- reaper.ExecProcess's timeout-for-fire-and-forget semantics. Both are
-- marked SPIKE below and must be confirmed against the live ReaScript API
-- docs before this action is trusted; nothing here claims to have verified
-- them (see docs/AGENTS.md's human-owned-REAPER-verification rule).

local source = debug.getinfo(1, "S").source
local directory = source:match("^@(.*/)")
if not directory then error("vgt transcribe track must be loaded from a file") end
local common = dofile(directory .. "vgt_common.lua")

local DEFER_WINDOW_SECONDS = 15 * 60
local DEFER_POLL_INTERVAL_SECONDS = 5

-- Best-effort default GM program (0-indexed, matching MT3's own program-
-- change byte convention -- see mt3_normalize.GM_PROGRAM_FAMILIES) per known
-- vgt target label. A product-taste call the plan deliberately leaves open
-- (see "Open questions" #1); always overridable in the dialog below.
local TARGET_PROGRAM_GUESS = {
  guitar = 25,       -- Acoustic Guitar (steel)
  bass = 33,         -- Electric Bass (finger)
  vocals = 53,       -- Choir Aahs (closest generic vocal GM patch)
  strings = 49,      -- String Ensemble 1
  piano = 0,          -- Acoustic Grand Piano
  keys = 0,
}

local function track_guid(track)
  return reaper.GetTrackGUID(track)
end

-- Best-effort target guess from a `[vgt] <Label> ...` track name.
local function guessed_program_for_track_name(name)
  local lower = name:lower()
  for target, program in pairs(TARGET_PROGRAM_GUESS) do
    if lower:find(target, 1, true) then return program end
  end
  return 0
end

local function validated_single_selection()
  local count = reaper.CountSelectedTracks(0)
  if count == 0 then error("Select the track you want to transcribe, then run this action again.") end
  if count > 1 then error("Select exactly one track to transcribe (found " .. count .. " selected).") end
  local track = reaper.GetSelectedTrack(0, 0)
  if common.starts_with_vgt(track) then
    error("\"" .. common.track_name(track) .. "\" is a [vgt]-owned track; select a different (non-[vgt]) track to transcribe.")
  end
  return track
end

-- The full span of every media item on `track` -- not just file-backed
-- audio (unlike vgt_initialize.lua's reference_start_and_end, this track may
-- be a MIDI or freshly-created working-copy track) -- so a track with
-- several items still renders/positions across their combined extent.
local function track_item_span(track)
  local start_s, end_s
  for index = 0, reaper.CountTrackMediaItems(track) - 1 do
    local item = reaper.GetTrackMediaItem(track, index)
    local position = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local finish = position + reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    if not start_s or position < start_s then start_s = position end
    if not end_s or finish > end_s then end_s = finish end
  end
  if not start_s then error("The selected track has no media items to transcribe.") end
  return start_s, end_s
end

local function project_tempo_or_refuse(analysis)
  local tempo = analysis and analysis.tempo and analysis.tempo.value
  if type(tempo) ~= "table" or not tempo.bpm then
    error("No analyzed tempo is on record for this project; run `vgt analyze` at least once before transcribing an arbitrary track.")
  end
  return tempo.bpm
end

local function resolve_vgt_runtime(body)
  local runtime_text = common.find_json_object(body, "runtime")
  local runtime = runtime_text and common.decode_json(runtime_text)
  local python_executable = runtime and runtime.python_executable
  if not python_executable or python_executable == "" then
    error("vgt's runtime location is not recorded in the sidecar; run `vgt analyze` once (from a terminal) to register it.")
  end
  return python_executable
end

local function new_job_id()
  math.randomseed(os.time() + math.floor(reaper.time_precise() * 1000))
  return string.format("%d-%04x", os.time(), math.random(0, 0xffff))
end

-- Windowed RMS silence check (N-second windows), *not* a whole-file check:
-- the sibling mt3 repo's own guardrail is that a REAPER bounce can go dead
-- partway through while still reporting the correct nominal total duration
-- (silence-padded) -- a whole-file check misses exactly that failure mode.
-- REAPER's own PCM_Source/AudioAccessor API gives windowed peak/RMS without
-- decoding the file in Lua by hand.
local RMS_WINDOW_SECONDS = 2.0
local TRAILING_SILENCE_THRESHOLD = -60.0 -- dBFS; below this counts as silent

local function windowed_rms_ok(path, expected_duration_s)
  local source_handle = reaper.PCM_Source_CreateFromFile(path)
  if not source_handle then return false, "REAPER could not open the rendered WAV" end
  local length = reaper.GetMediaSourceLength(source_handle)
  if length <= 0 then
    reaper.PCM_Source_Destroy(source_handle)
    return false, "rendered WAV reports zero length"
  end
  -- SPIKE (unverified): reaper.CreateAudioAccessor's exact signature for a
  -- bare PCM_source (as opposed to a take/track) is assumed, not confirmed,
  -- against the live ReaScript API docs -- see this file's header.
  local accessor = reaper.CreateAudioAccessor and reaper.CreateAudioAccessor(source_handle)
  if not accessor then
    -- Fall back to a coarse duration-only check when AudioAccessor is
    -- unavailable in this REAPER version; still refuses a badly truncated
    -- nominal-duration file, just without the windowed trailing-silence view.
    reaper.PCM_Source_Destroy(source_handle)
    if math.abs(length - expected_duration_s) > math.max(0.5, expected_duration_s * 0.02) then
      return false, string.format("rendered WAV duration (%.2fs) does not match the item span (%.2fs)", length, expected_duration_s)
    end
    return true
  end
  local channels = reaper.GetMediaSourceNumChannels(source_handle)
  local samplerate = 48000
  local window_samples = math.floor(RMS_WINDOW_SECONDS * samplerate)
  local buffer = reaper.new_array(window_samples * math.max(channels, 1))
  local cursor = 0.0
  local last_loud_at = 0.0
  while cursor < length do
    buffer:clear()
    local got = reaper.GetAudioAccessorSamples(accessor, samplerate, channels, cursor, window_samples, buffer)
    if got > 0 then
      local values = buffer:table(1, got * channels)
      local sum_sq = 0.0
      for _, sample in ipairs(values) do sum_sq = sum_sq + sample * sample end
      local rms = #values > 0 and math.sqrt(sum_sq / #values) or 0
      local db = rms > 0 and (20 * math.log(rms, 10)) or -math.huge
      if db > TRAILING_SILENCE_THRESHOLD then last_loud_at = cursor + RMS_WINDOW_SECONDS end
    end
    cursor = cursor + RMS_WINDOW_SECONDS
  end
  reaper.DestroyAudioAccessor(accessor)
  reaper.PCM_Source_Destroy(source_handle)
  if math.abs(length - expected_duration_s) > math.max(0.5, expected_duration_s * 0.02) then
    return false, string.format("rendered WAV duration (%.2fs) does not match the item span (%.2fs)", length, expected_duration_s)
  end
  -- A long trailing run of silence that reaches end-of-file is the signature
  -- guardrail: a file that is mostly real audio with a normal quiet outro
  -- passes (last_loud_at close to length); a file that went dead partway
  -- through and stayed silent to the nominal end does not.
  if length - last_loud_at > math.max(RMS_WINDOW_SECONDS * 3, expected_duration_s * 0.2) then
    return false, string.format(
      "rendered WAV has %.1fs of trailing silence reaching end-of-file (possible truncated bounce)", length - last_loud_at
    )
  end
  return true
end

-- Render the selected track's audio via REAPER's "stems (selected tracks)"
-- mode -- computed from just the selection, touching no track/FX state --
-- into `job_dir/source.wav`, spanning [start_s, end_s). Restores whatever
-- project-level RENDER_* settings it changed via pcall; deliberately no
-- Undo_BeginBlock/EndBlock, since a correct stems render mutates no project
-- state at all (see the plan's Safety section).
local function render_selected_track(job_dir, start_s, end_s)
  local saved = {}
  local string_keys = {"RENDER_FILE", "RENDER_PATTERN", "RENDER_FORMAT"}
  local number_keys = {"RENDER_SETTINGS", "RENDER_BOUNDSFLAG", "RENDER_STARTPOS", "RENDER_ENDPOS"}
  for _, key in ipairs(string_keys) do
    local _, value = reaper.GetSetProjectInfo_String(0, key, "", false)
    saved[key] = value
  end
  for _, key in ipairs(number_keys) do
    saved[key] = reaper.GetSetProjectInfo(0, key, 0, false)
  end

  local restore = function()
    for _, key in ipairs(string_keys) do reaper.GetSetProjectInfo_String(0, key, saved[key] or "", true) end
    for _, key in ipairs(number_keys) do reaper.GetSetProjectInfo(0, key, saved[key] or 0, true) end
  end

  local ok, err = pcall(function()
    reaper.GetSetProjectInfo_String(0, "RENDER_FILE", job_dir, true)
    reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", "source", true)
    -- SPIKE (unverified): 'ev' + 'aw' little-endian fourCC for WAV, matching
    -- REAPER's GetSetProjectInfo_String RENDER_FORMAT convention as
    -- documented in the ReaScript API -- confirm before relying on this.
    reaper.GetSetProjectInfo_String(0, "RENDER_FORMAT", "evaw", true)
    -- SPIKE (unverified): RENDER_SETTINGS=3 is this implementation's
    -- best-effort guess at "stems (selected tracks) via master";
    -- RENDER_BOUNDSFLAG=0 is "custom time bounds", paired with explicit
    -- RENDER_STARTPOS/RENDER_ENDPOS below so the render never depends on
    -- (or mutates) the project's own time selection. Confirm both bit
    -- values against the live ReaScript API docs during human verification.
    reaper.GetSetProjectInfo(0, "RENDER_SETTINGS", 3, true)
    reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", 0, true)
    reaper.GetSetProjectInfo(0, "RENDER_STARTPOS", start_s, true)
    reaper.GetSetProjectInfo(0, "RENDER_ENDPOS", end_s, true)
    -- SPIKE (unverified): action id 42230 is this implementation's
    -- best-effort identification of "File: Render project, using the most
    -- recent render settings, auto-close render dialog" -- confirm against
    -- the live Action List during human verification.
    reaper.Main_OnCommand(42230, 0)
  end)
  restore()
  if not ok then error(err) end

  local expected = job_dir .. "/source.wav"
  local info = reaper.file_exists and reaper.file_exists(expected)
  if not info then
    -- REAPER's stems-mode filename wildcarding may not have produced the
    -- exact literal "source.wav" this action asked for; fall back to
    -- picking up the one WAV file EnumerateFiles finds in job_dir.
    local index, found = 0, nil
    while true do
      local name = reaper.EnumerateFiles(job_dir, index)
      if not name then break end
      if name:lower():match("%.wav$") then
        if found then error("render produced more than one WAV file in " .. job_dir .. "; expected exactly one") end
        found = name
      end
      index = index + 1
    end
    if not found then error("render did not produce a WAV file in " .. job_dir) end
    os.rename(job_dir .. "/" .. found, expected)
  end
  return expected
end

local function transcribe_selected_track()
  local track = validated_single_selection()
  local body = common.read_sidecar_body()
  if not body then error("No .vgt sidecar found; run vgt_initialize.lua (apply) first.") end
  local analysis = common.read_analysis(body)
  local tempo_bpm = project_tempo_or_refuse(analysis)
  local namespace = analysis and analysis.stems and analysis.stems.artifact_namespace
  if not namespace or namespace == "" then
    error("No artifact namespace recorded yet; run `vgt analyze` at least once before transcribing an arbitrary track.")
  end
  local python_executable = resolve_vgt_runtime(body)

  local name = common.track_name(track)
  local ok_program, csv = reaper.GetUserInputs(
    "vgt: transcribe " .. name, 1, "GM program (0-127),extrawidth=60",
    tostring(guessed_program_for_track_name(name))
  )
  if not ok_program then return end
  local program = tonumber(csv)
  if not program or program < 0 or program > 127 or program ~= math.floor(program) then
    reaper.ShowMessageBox("GM program must be a whole number from 0 to 127.", "vgt", 0)
    return
  end

  local start_s, end_s = track_item_span(track)
  local job_id = new_job_id()
  local job_dir = common.project_dir() .. "vgt/" .. namespace .. "/track-jobs/" .. job_id
  reaper.RecursiveCreateDirectory(job_dir, 0)

  -- Capture the selection's geometry before rendering (§3 step 3): the
  -- import step needs it, and the live selection may change afterwards.
  local status_path = job_dir .. "/status.json"
  local status_file = io.open(status_path, "w")
  if not status_file then error("could not write " .. status_path) end
  status_file:write(string.format(
    [[{
  "job_id": %s,
  "source_track_name": %s,
  "source_track_guid": %s,
  "item_start_s": %f,
  "item_end_s": %f,
  "requested_program": %d,
  "midi_tempo": %f,
  "created_at": %s
}
]],
    common.encode_json_scalar(job_id), common.encode_json_scalar(name), common.encode_json_scalar(track_guid(track)),
    start_s, end_s, program, tempo_bpm, common.encode_json_scalar(os.date("!%Y-%m-%dT%H:%M:%SZ"))
  ))
  status_file:close()

  local wav_path = render_selected_track(job_dir, start_s, end_s)
  local render_ok, render_error = windowed_rms_ok(wav_path, end_s - start_s)
  if not render_ok then
    error("Refusing to transcribe a possibly-truncated render: " .. render_error)
  end

  local cmdline = table.concat({
    common.shell_quote(python_executable), "-m", "vgt", "transcription", "track", "run",
    common.shell_quote((common.project_path())), common.shell_quote(job_id),
    "--source", common.shell_quote(wav_path), "--force-program", tostring(program),
    "--label", common.shell_quote(name),
  }, " ")

  local spawned = false
  if reaper.ExecProcess then
    -- SPIKE (unverified): confirm which timeoutmsec value returns
    -- immediately without waiting for the child to exit -- see the plan's
    -- "Open questions" #2. 0 is this implementation's best-effort guess.
    local ok_spawn = pcall(reaper.ExecProcess, cmdline, 0)
    spawned = ok_spawn
  end
  if not spawned then
    -- Fallback: a bare background shell spawn. No quoting help beyond
    -- shell_quote above, and no documented lifetime guarantee once this
    -- script returns -- second choice, per the plan.
    os.execute(cmdline .. " > /dev/null 2>&1 &")
  end

  reaper.ShowConsoleMsg("vgt: started transcription job " .. job_id .. " for \"" .. name .. "\"\n")

  -- Reuses vgt_get_transcription.lua's own importer (common.check_and_import_job)
  -- so there is exactly one importer implementation, per the plan's #9 --
  -- this defer loop is a second *caller* of it, not a second copy.
  local deadline = reaper.time_precise() + DEFER_WINDOW_SECONDS
  local next_poll = 0
  local function poll()
    if reaper.time_precise() < next_poll then reaper.defer(poll) return end
    next_poll = reaper.time_precise() + DEFER_POLL_INTERVAL_SECONDS
    local done = common.check_and_import_job(job_id)
    if done or reaper.time_precise() > deadline then return end
    reaper.defer(poll)
  end
  reaper.defer(poll)
end

local ok, error_message = xpcall(transcribe_selected_track, debug.traceback)
if not ok then
  reaper.ShowMessageBox("vgt transcribe track failed:\n" .. error_message, "vgt", 0)
end
