-- vgt: transcribe the selected track (MT3, on-demand) for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP
-- is open, with exactly one track selected. It renders that track's audio,
-- captures the selection's geometry, spawns a detached background MT3 job,
-- and starts a bounded poll that auto-imports the result when it finishes.
-- See docs/on-demand-track-transcription-plan.md for the full design.
--
-- RENDER_SETTINGS/RENDER_BOUNDSFLAG/RENDER_FORMAT (the "stems, selected
-- track, custom time bounds" render) were originally unverified guesses;
-- they are now confirmed against a live project via a throwaway diagnostic
-- action (see the comment at the render site) -- one of the three, a
-- truncated RENDER_FORMAT, was in fact wrong and produced real silent
-- renders before the fix. The job spawn itself uses plain
-- `os.execute(cmd .. " &")`, not `reaper.ExecProcess`: an earlier version's
-- attempt to use ExecProcess first silently never actually launched a
-- process on a real REAPER install (see the comment at the spawn site).

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

-- The standard General MIDI Level 1 instrument names, in program-change
-- order. Index 1 in this Lua array is GM program 0 (Acoustic Grand Piano);
-- see gm_program_name below for the 0-indexed lookup. A bare number in the
-- transcribe dialog otherwise means nothing to anyone who hasn't memorized
-- the GM patch list.
local GM_PROGRAM_NAMES = {
  "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano",
  "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
  "Celesta", "Glockenspiel", "Music Box", "Vibraphone",
  "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
  "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
  "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
  "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)",
  "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar", "Guitar Harmonics",
  "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass",
  "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
  "Violin", "Viola", "Cello", "Contrabass",
  "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
  "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2",
  "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
  "Trumpet", "Trombone", "Tuba", "Muted Trumpet",
  "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
  "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax",
  "Oboe", "English Horn", "Bassoon", "Clarinet",
  "Piccolo", "Flute", "Recorder", "Pan Flute",
  "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
  "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
  "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
  "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)",
  "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
  "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
  "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
  "Sitar", "Banjo", "Shamisen", "Koto",
  "Kalimba", "Bag pipe", "Fiddle", "Shanai",
  "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock",
  "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
  "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
  "Telephone Ring", "Helicopter", "Applause", "Gunshot",
}

local function gm_program_name(program)
  return GM_PROGRAM_NAMES[(tonumber(program) or 0) + 1] or "unknown"
end

-- gfx.showmenu's return value crosses the REAPER API boundary as a Lua
-- float (e.g. 4.0, not 4), so a program number derived from it (family.first
-- + choice - 1) is a float too, even though it is always a whole number by
-- construction. Left as a float, string concatenation (the console log) and
-- `--force-program` on the spawned command line both render it as "27.0",
-- which Python's `argparse(type=int)` rejects outright -- the spawned
-- process then exits on an argument-parsing error before it ever reaches
-- `run_track_job`, so status.json never gets even a "running" state and the
-- job looks exactly like it silently never started at all. math.floor
-- coerces back to a genuine Lua integer subtype (Lua 5.3+), not just an
-- integer-valued float, so every downstream use renders "27".
local function as_integer_program(value)
  return math.floor((tonumber(value) or 0) + 0.5)
end

-- The two contiguous GM ranges vgt already treats as a family elsewhere
-- (see mt3_normalize.GM_PROGRAM_FAMILIES) -- exactly the ones worth
-- offering as a full clickable menu instead of a bare number field. Other
-- guesses (vocals, strings, piano/keys) have no equally clean 8-item
-- family, so they fall straight to manual number entry.
local GM_PROGRAM_FAMILIES = {
  guitar = {first = 24, last = 31},
  bass = {first = 32, last = 39},
}

local function family_for_guess(guess)
  for _, family in pairs(GM_PROGRAM_FAMILIES) do
    if guess >= family.first and guess <= family.last then return family end
  end
  return nil
end

-- Menu labels for every program in `family`, plus a manual-entry escape
-- hatch, in the exact order gfx.showmenu will number them (1-based) --
-- split out from pick_program_from_family so this part is testable without
-- a live gfx context.
local function family_menu_labels(family)
  local labels = {}
  for program = family.first, family.last do
    labels[#labels + 1] = program .. ": " .. gm_program_name(program)
  end
  labels[#labels + 1] = "Other (enter a GM program number)..."
  return labels
end

-- Offer a clickable menu of every program in `family` instead of making the
-- user memorize or guess a GM program number (the whole point of this
-- picker: "how should I know what 25 is?"). Returns the chosen program
-- number, the string "manual" if the user asked for the plain number-entry
-- fallback instead, or nil if the menu was dismissed.
local function pick_program_from_family(name, family)
  local labels = family_menu_labels(family)
  gfx.init('vgt: transcribe "' .. name .. '"', 0, 0)
  gfx.x, gfx.y = gfx.mouse_x, gfx.mouse_y
  local choice = gfx.showmenu(table.concat(labels, "|"))
  gfx.quit()
  if choice < 1 then return nil end
  if choice == #labels then return "manual" end
  return family.first + choice - 1
end

local function validated_single_selection()
  local count = reaper.CountSelectedTracks(0)
  if count == 0 then error("Select the track you want to transcribe, then run this action again.") end
  if count > 1 then error("Select exactly one track to transcribe (found " .. count .. " selected).") end
  local track = reaper.GetSelectedTrack(0, 0)
  if common.starts_with_vgt(track) then
    error("\"" .. common.track_name(track) .. "\" is a [vgt]-owned track; select a different (non-[vgt]) track to transcribe.")
  end
  -- The "stems (selected tracks)" render mode renders what would actually
  -- be audible, muted tracks included -- a muted track therefore renders
  -- as exact, correct silence, not a bug in the render itself. Confirmed
  -- for real (not hypothetically): the first non-crashing failure this
  -- action ever produced was exactly this, on a track muted for unrelated
  -- editing reasons. Refuse clearly up front rather than spending a full
  -- render + MT3 inference run on audio nothing will ever be audible in.
  if reaper.GetMediaTrackInfo_Value(track, "B_MUTE") == 1 then
    error("\"" .. common.track_name(track) .. "\" is muted; unmute it before transcribing (a muted track renders as silence).")
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
--
-- Deliberately does not use any REAPER audio-reading API (PCM_Source,
-- AudioAccessor): an earlier version tried reaper.CreateAudioAccessor on a
-- bare PCM_source, and it never once actually refused a bad render --
-- confirmed for real, not hypothetically: two different genuinely
-- all-zero-sample renders (one from a since-fixed RENDER_FORMAT bug, one
-- from transcribing a muted track) both sailed straight through it to a
-- wasted MT3 inference run. This instead parses the rendered WAV directly
-- (RIFF/fmt/data chunks, 16/24/32-bit PCM or 32-bit float), which has no
-- REAPER-version-specific API surface to get wrong and is fully
-- offline-testable against synthetic WAV fixtures.
local RMS_WINDOW_SECONDS = 2.0
local TRAILING_SILENCE_THRESHOLD = -60.0 -- dBFS; below this counts as silent
-- Only the first slice of each window is actually decoded (not every frame
-- in it): full-resolution RMS over a multi-minute file is too slow for pure
-- Lua byte-by-byte decoding, and the failure mode this guards against (a
-- render that went dead or a muted track) is silent for many consecutive
-- whole seconds, not for a single sub-second gap, so a short probe per
-- window is exactly as effective at catching it.
local PROBE_FRAMES_PER_WINDOW = 4096

local function le_uint(bytes, offset, count)
  local value = 0
  for i = 0, count - 1 do
    value = value + bytes:byte(offset + i) * (256 ^ i)
  end
  return value
end

-- Advance past RIFF/WAVE and any chunk before `data` (typically just `fmt `)
-- to find the format and the data chunk's byte offset/size. Returns
-- `format, data_offset, data_size` or `nil, message` on a malformed file.
local function read_wav_format(file)
  if file:read(4) ~= "RIFF" then return nil, "not a RIFF file" end
  file:read(4) -- overall size, unused -- the data chunk's own size is authoritative
  if file:read(4) ~= "WAVE" then return nil, "not a WAVE file" end
  local format
  while true do
    local chunk_id = file:read(4)
    if not chunk_id or #chunk_id < 4 then return nil, "missing data chunk" end
    local size_bytes = file:read(4)
    if not size_bytes or #size_bytes < 4 then return nil, "truncated chunk header" end
    local size = le_uint(size_bytes, 1, 4)
    if chunk_id == "fmt " then
      local body = file:read(size)
      if not body or #body < 16 then return nil, "truncated fmt chunk" end
      format = {
        audio_format = le_uint(body, 1, 2), channels = le_uint(body, 3, 2),
        sample_rate = le_uint(body, 5, 4), bits_per_sample = le_uint(body, 15, 2),
      }
      if size % 2 == 1 then file:read(1) end -- RIFF chunks are word-aligned
    elseif chunk_id == "data" then
      if not format then return nil, "data chunk before fmt chunk" end
      return format, file:seek(), size
    else
      file:seek("cur", size + (size % 2))
    end
  end
end

-- Decode one little-endian PCM (or, for a 4-byte float format, IEEE 754
-- single-precision) sample at `offset` (1-based) in `bytes` to [-1, 1].
local function pcm_sample(bytes, offset, bytes_per_sample, is_float)
  if is_float and bytes_per_sample == 4 then
    local b1, b2, b3, b4 = bytes:byte(offset), bytes:byte(offset + 1), bytes:byte(offset + 2), bytes:byte(offset + 3)
    local sign = (b4 >= 128) and -1 or 1
    local exponent = ((b4 % 128) * 2) + math.floor(b3 / 128)
    local mantissa = ((b3 % 128) * 65536) + (b2 * 256) + b1
    if exponent == 0 and mantissa == 0 then return 0.0 end
    if exponent == 0 then return sign * mantissa * 2 ^ (-149) end
    return sign * (1 + mantissa / 8388608) * 2 ^ (exponent - 127)
  end
  local max_value = 2 ^ (bytes_per_sample * 8 - 1)
  local value = le_uint(bytes, offset, bytes_per_sample)
  if value >= max_value then value = value - max_value * 2 end
  return value / max_value
end

local function windowed_rms_ok(path, expected_duration_s)
  local file = io.open(path, "rb")
  if not file then return false, "could not open the rendered WAV" end
  local format, data_offset, data_size = read_wav_format(file)
  if not format then
    file:close()
    return false, "rendered file is not a readable WAV: " .. tostring(data_offset)
  end
  local bytes_per_sample = format.bits_per_sample / 8
  local frame_bytes = bytes_per_sample * format.channels
  if frame_bytes <= 0 or format.sample_rate <= 0 then
    file:close()
    return false, "rendered WAV has an unreadable format"
  end
  local total_frames = math.floor(data_size / frame_bytes)
  local length = total_frames / format.sample_rate
  if length <= 0 then
    file:close()
    return false, "rendered WAV reports zero length"
  end
  if math.abs(length - expected_duration_s) > math.max(0.5, expected_duration_s * 0.02) then
    file:close()
    return false, string.format("rendered WAV duration (%.2fs) does not match the item span (%.2fs)", length, expected_duration_s)
  end

  local is_float = format.audio_format == 3
  local window_frames = math.floor(RMS_WINDOW_SECONDS * format.sample_rate)
  local last_loud_at = 0.0
  local frame_index = 0
  while frame_index < total_frames do
    file:seek("set", data_offset + frame_index * frame_bytes)
    local probe_frames = math.min(PROBE_FRAMES_PER_WINDOW, total_frames - frame_index)
    local chunk = file:read(probe_frames * frame_bytes)
    local available_frames = chunk and math.floor(#chunk / frame_bytes) or 0
    if available_frames > 0 then
      local sum_sq, count = 0.0, 0
      for frame = 0, available_frames - 1 do
        for channel = 0, format.channels - 1 do
          local sample = pcm_sample(chunk, frame * frame_bytes + channel * bytes_per_sample + 1, bytes_per_sample, is_float)
          sum_sq = sum_sq + sample * sample
          count = count + 1
        end
      end
      local rms = math.sqrt(sum_sq / count)
      local db = rms > 0 and (20 * math.log(rms, 10)) or -math.huge
      if db > TRAILING_SILENCE_THRESHOLD then
        last_loud_at = math.min(length, (frame_index / format.sample_rate) + RMS_WINDOW_SECONDS)
      end
    end
    frame_index = frame_index + window_frames
  end
  file:close()

  -- A file with no loud window at all is unambiguously bad regardless of
  -- how short it is: `last_loud_at` only ever advances past its initial 0.0
  -- once some window is found loud, so this alone (a duration-independent
  -- check) catches full-file silence that the trailing-gap check below
  -- would otherwise miss on a short file -- its own maximum possible gap
  -- (bounded by the file's own length) can never exceed the gap check's
  -- fixed floor. Caught for real testing a 4s fixture, not hypothetically.
  if last_loud_at <= 0.0 then
    return false, "rendered WAV is entirely silent (no audio detected in any window)"
  end
  -- A long trailing run of silence that reaches end-of-file is the signature
  -- guardrail for the other failure shape: a file that is mostly real audio
  -- with a normal quiet outro passes (last_loud_at close to length); a file
  -- that went dead partway through does not.
  if length - last_loud_at > math.max(RMS_WINDOW_SECONDS * 3, expected_duration_s * 0.2) then
    return false, string.format(
      "rendered WAV has %.1fs of trailing silence reaching end-of-file (possible truncated or muted bounce)",
      length - last_loud_at
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
    -- RENDER_FORMAT is not a plain 4-byte "evaw" fourCC string: REAPER's own
    -- value (read back from a live project via a throwaway diagnostic
    -- action -- see docs/on-demand-track-transcription-plan.md's render
    -- SPIKE) is 7 bytes: the "evaw" WAV fourCC followed by 3 more bytes
    -- encoding bit depth (24 = 0x18) and format flags. Writing only the
    -- first 4 bytes left the rest unset -- confirmed, not hypothetical, the
    -- actual cause of a real silent render: REAPER's WAV encoder reads
    -- those trailing bytes too, so a truncated blob produced a technically
    -- valid, correctly-sized, but silent WAV file every time.
    reaper.GetSetProjectInfo_String(0, "RENDER_FORMAT", "evaw" .. string.char(24, 0, 1), true)
    -- Verified against a live project (same diagnostic): RENDER_SETTINGS=3
    -- is exactly "stems (selected tracks)"; RENDER_BOUNDSFLAG=0 is "custom
    -- time bounds", paired with explicit RENDER_STARTPOS/RENDER_ENDPOS below
    -- so the render never depends on (or mutates) the project's own time
    -- selection. Both already matched this implementation's original guess.
    reaper.GetSetProjectInfo(0, "RENDER_SETTINGS", 3, true)
    reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", 0, true)
    reaper.GetSetProjectInfo(0, "RENDER_STARTPOS", start_s, true)
    reaper.GetSetProjectInfo(0, "RENDER_ENDPOS", end_s, true)
    -- Verified against a live project: action id 42230 ("File: Render
    -- project, using the most recent render settings...") does trigger the
    -- render (visible "Rendering to file..." progress dialog, real output).
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
  local guess = guessed_program_for_track_name(name)
  local family = family_for_guess(guess)
  local program
  if family then
    local picked = pick_program_from_family(name, family)
    if picked == nil then return end
    if picked ~= "manual" then program = picked end
  end
  if not program then
    -- Either no family menu applies to this guess, or the user explicitly
    -- asked for manual entry from that menu. The field caption column has a
    -- fixed width GetUserInputs never widens (extrawidth only grows the
    -- input box itself), so a longer instrument name gets silently clipped
    -- there; the dialog's own title bar has much more room and renders in
    -- full, so the guess's name goes there instead.
    local ok_program, csv = reaper.GetUserInputs(
      string.format('vgt: transcribe "%s" (guess: %d = %s)', name, guess, gm_program_name(guess)),
      1, "GM program (0-127),extrawidth=60", tostring(guess)
    )
    if not ok_program then return end
    program = tonumber(csv)
    if not program or program < 0 or program > 127 or program ~= math.floor(program) then
      reaper.ShowMessageBox("GM program must be a whole number from 0 to 127.", "vgt", 0)
      return
    end
  end
  program = as_integer_program(program)
  -- Echo back what the number means before actually spending an inference
  -- run on it: the whole point of naming it is so the user can catch a typo
  -- (e.g. 35 instead of 33) before the job starts, not just after.
  if reaper.ShowMessageBox(
    string.format('Transcribe "%s" onto GM program %d (%s)?', name, program, gm_program_name(program)),
    "vgt: transcribe " .. name, 4
  ) ~= 6 then
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

  -- `os.execute(cmd .. " &")` is the spawn mechanism, not `reaper.ExecProcess`:
  -- an earlier version tried ExecProcess first and only fell back to this on
  -- failure, but that "fell back" check was itself broken -- `pcall(reaper.
  -- ExecProcess, ...)` returns true (no Lua-level error) even when the
  -- process never actually launched, since a failed launch is a return
  -- value, not a Lua exception, so the fallback below was silently
  -- unreachable on any real REAPER install and every job appeared to run
  -- forever without ever starting. `os.execute` backgrounded with a
  -- trailing `&` is standard POSIX shell detachment (reparented to init once
  -- backgrounded, independent of this script's own lifetime) and does not
  -- depend on any REAPER-specific, unverified timeout semantics.
  local log_path = job_dir .. "/spawn.log"
  local exec_result = os.execute(cmdline .. " > " .. common.shell_quote(log_path) .. " 2>&1 &")
  if exec_result == false then
    error("failed to spawn the transcription job (os.execute refused the command); see " .. log_path)
  end

  reaper.ShowConsoleMsg(
    "vgt: started transcription job " .. job_id .. " for \"" .. name .. "\" (program " .. program
      .. " = " .. gm_program_name(program) .. ")\n"
  )

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
