-- vgt Phase 0 apply action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.
-- It is the only writer of REAPER projects: the Python CLI intentionally never edits RPP text.

local PREFIX = "[vgt]"
local CHORDS_NAME = PREFIX .. " Chords"
local BEATS_NAME = PREFIX .. " Beats"
local CLICK_NAME = PREFIX .. " Click"
local STEM_TRACKS = {
  {artifact = "vocals", filename = "stems/vocals.wav", name = PREFIX .. " Vocals"},
  {artifact = "instrumental", filename = "stems/instrumental.wav", name = PREFIX .. " Instrumental"},
  {artifact = "bass", filename = "stems/bass.wav", name = PREFIX .. " Bass"},
  {artifact = "drums", filename = "stems/drums.wav", name = PREFIX .. " Drums"},
  {artifact = "guitar", filename = "stems/guitar.wav", name = PREFIX .. " Guitar"},
  {artifact = "backing", filename = "stems/backing-no-guitar.wav", name = PREFIX .. " Backing (no guitar)"},
  {artifact = "strings", filename = "stems/strings.wav", name = PREFIX .. " Strings"},
  {artifact = "piano", filename = "stems/piano.wav", name = PREFIX .. " Keys / Piano"},
}
local STEM_LEASE_TIMEOUT_SECONDS = 30 * 60

local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
end

local function project_dir()
  return project_path():match("^(.*[/\\])") or ""
end

local function sidecar_path()
  -- The sidecar shares the project's name with a .vgt extension (Foo.RPP -> Foo.vgt).
  local path = project_path()
  return (path:gsub("%.[^./\\]*$", "") .. ".vgt")
end

local function escaped(value)
  return value:gsub("\\", "\\\\"):gsub('"', '\\"')
end

local function track_name(track)
  local _, name = reaper.GetTrackName(track, "")
  return name
end

local function starts_with_vgt(track)
  return track_name(track):sub(1, #PREFIX) == PREFIX
end

-- A reference must be real audio vgt can later resolve a source path for
-- (see project.track_source_path): at least one item whose active take
-- has a file-backed source. This is deliberately false for tracks with no
-- items, MIDI-only tracks, and REAPER-native generators such as a count-in
-- click track (<SOURCE CLICK>, which has no file at all).
local function has_file_backed_media(track)
  for item_index = 0, reaper.CountTrackMediaItems(track) - 1 do
    local item = reaper.GetTrackMediaItem(track, item_index)
    local take = reaper.GetActiveTake(item)
    if take then
      local filename = reaper.GetMediaSourceFileName(reaper.GetMediaItemTake_Source(take), "")
      if filename ~= "" then return true end
    end
  end
  return false
end

-- Candidate reference tracks are the project's own (non-vgt) tracks that
-- actually have file-backed audio on them, in order.
local function candidate_tracks()
  local candidates = {}
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if not starts_with_vgt(track) and has_file_backed_media(track) then candidates[#candidates + 1] = track end
  end
  return candidates
end

-- Ask which track is the reference. Automation (e.g. the headless
-- verifier) can preselect it via the "vgt"/"reference_index" ExtState, a 0-based
-- index over candidate_tracks(). A lone candidate is used without prompting;
-- otherwise interactive users get a popup menu. Returns the chosen track, or
-- nil if the user dismissed the menu.
local function choose_reference(candidates)
  if #candidates == 0 then error("No non-[vgt] tracks with file-backed media to use as a reference.") end

  local forced = reaper.GetExtState("vgt", "reference_index")
  if forced ~= "" then
    local index = tonumber(forced)
    if not index or index < 0 or index >= #candidates then
      error("vgt reference_index ExtState is out of range: " .. forced)
    end
    return candidates[index + 1]
  end

  if #candidates == 1 then return candidates[1] end

  local labels = {}
  for position, track in ipairs(candidates) do
    -- "|" is the menu separator, so it must not appear inside a label.
    labels[position] = track_name(track):gsub("|", "/")
  end
  gfx.init("vgt: choose the reference track", 0, 0)
  gfx.x, gfx.y = gfx.mouse_x, gfx.mouse_y
  local choice = gfx.showmenu(table.concat(labels, "|"))
  gfx.quit()
  if choice < 1 then return nil end
  return candidates[choice]
end

local function read_sidecar_body()
  local file = io.open(sidecar_path(), "r")
  if not file then return nil end
  local body = file:read("*a")
  file:close()
  return body
end

-- Guitar type is a declaration, never an inference.  An automation caller
-- may set vgt/guitar_type to electric or acoustic; otherwise first use asks
-- once and persists the answer in the sidecar config.
local function persisted_guitar_type()
  local body = read_sidecar_body() or ""
  local value = body:match('"guitar_type"%s*:%s*"([^"]+)"')
  if value == "electric" or value == "acoustic" then return value end
  return nil
end

local function choose_guitar_type()
  local forced = reaper.GetExtState("vgt", "guitar_type")
  if forced ~= "" then
    if forced ~= "electric" and forced ~= "acoustic" then
      error("vgt guitar_type ExtState must be electric or acoustic: " .. forced)
    end
    return forced
  end
  local existing = persisted_guitar_type()
  if existing then return existing end
  gfx.init("vgt: choose the guitar type", 0, 0)
  gfx.x, gfx.y = gfx.mouse_x, gfx.mouse_y
  local choice = gfx.showmenu("Electric|Acoustic")
  gfx.quit()
  if choice == 1 then return "electric" end
  if choice == 2 then return "acoustic" end
  return nil
end

local function read_managed_guids()
  local body = read_sidecar_body()
  if not body then return {} end
  local guids = {}
  -- GUIDs are deliberately read only from our schema's managed_track_guids array.
  local array = body:match('"managed_track_guids"%s*:%s*%[(.-)%]') or ""
  for guid in array:gmatch("{[%x%-]+}") do guids[guid] = true end
  return guids
end

local function read_managed_region_ids()
  local body = read_sidecar_body()
  if not body then return {} end
  local ids = {}
  local array = body:match('"managed_region_ids"%s*:%s*%[(.-)%]') or ""
  for id in array:gmatch("%-?%d+") do ids[tonumber(id)] = true end
  return ids
end

local function prior_tempo_map_applied()
  local body = read_sidecar_body() or ""
  -- This flag is vgt's record that the current map was created during an
  -- earlier eligible apply.  We never blindly rewrite that map on re-apply --
  -- see prior_tempo_map_fingerprint / current_tempo_fingerprint below, which
  -- decide whether it is still safe to do so.
  return body:match('"tempo_map_applied"%s*:%s*true') ~= nil
end

local function prior_tempo_map_fingerprint()
  local body = read_sidecar_body() or ""
  return body:match('"tempo_map_fingerprint"%s*:%s*"(.-)"') or ""
end

local function prior_tempo_data_fingerprint()
  local body = read_sidecar_body() or ""
  return body:match('"tempo_data_fingerprint"%s*:%s*"(.-)"') or ""
end

-- A canonical snapshot of the *analyzed* tempo data itself (bpm, downbeat
-- offset, time signature, piecewise spans) -- independent of where the
-- reference track happens to sit in the timeline. Comparing this against the
-- fingerprint recorded for the live map is how a re-apply tells "the
-- detected/corrected tempo actually changed" apart from "nothing changed,
-- don't needlessly rewrite (and thereby re-shift any beat-attached
-- reference items) an already-current map".
local function tempo_data_fingerprint(tempo)
  local parts = {
    string.format("%.6f:%s:%.6f", tonumber(tempo.bpm) or 0, tostring(tempo.time_signature or ""), tonumber(tempo.downbeat_offset_seconds) or 0),
  }
  if type(tempo.spans) == "table" then
    for _, span in ipairs(tempo.spans) do
      parts[#parts + 1] = string.format("%.6f:%.6f:%.6f", tonumber(span.start_seconds) or 0, tonumber(span.end_seconds) or 0, tonumber(span.bpm) or 0)
    end
  end
  return table.concat(parts, ";")
end

-- The Python `vgt analyze` stage adds a top-level "analysis"
-- object to the sidecar. This action is the sole writer of the sidecar's
-- other fields, so it must round-trip that object verbatim on re-apply
-- rather than silently dropping any analysis a user has already run.
local function read_analysis_block()
  local body = read_sidecar_body()
  if not body then return nil end
  local key_start = body:find('"analysis"%s*:%s*{')
  if not key_start then return nil end
  local brace_start = body:find("{", key_start)
  local depth = 0
  local in_string = false
  local escaped_char = false
  for index = brace_start, #body do
    local char = body:sub(index, index)
    if in_string then
      -- JSON strings may contain braces (for example in a human-entered
      -- section label), which are not object delimiters. A backslash only
      -- escapes the immediately following character.
      if escaped_char then
        escaped_char = false
      elseif char == "\\" then
        escaped_char = true
      elseif char == '"' then
        in_string = false
      end
    elseif char == '"' then
      in_string = true
    elseif char == "{" then
      depth = depth + 1
    elseif char == "}" then
      depth = depth - 1
      if depth == 0 then return body:sub(brace_start, index) end
    end
  end
  return nil
end

-- Analysis is produced by Python, so use a small JSON reader here instead of
-- trying to scrape individual values with patterns.  In particular, labels
-- are user-correctable and may contain punctuation that a pattern parser
-- would mishandle.  This intentionally only decodes JSON; the RPP remains
-- exclusively a REAPER API write.
local function decode_json(text)
  local position = 1
  local function whitespace()
    while text:sub(position, position):match("%s") do position = position + 1 end
  end
  local function string_value()
    position = position + 1 -- opening quote
    local pieces = {}
    while position <= #text do
      local char = text:sub(position, position)
      position = position + 1
      if char == '"' then return table.concat(pieces) end
      if char ~= "\\" then
        pieces[#pieces + 1] = char
      else
        local escaped_char = text:sub(position, position)
        position = position + 1
        local escapes = {['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t'}
        if escaped_char == "u" then
          local code = tonumber(text:sub(position, position + 3), 16)
          position = position + 4
          pieces[#pieces + 1] = (code and utf8.char(code)) or "?"
        elseif escapes[escaped_char] then
          pieces[#pieces + 1] = escapes[escaped_char]
        else
          error("invalid JSON escape")
        end
      end
    end
    error("unterminated JSON string")
  end
  local value
  local function array_value()
    position = position + 1
    local result = {}
    whitespace()
    if text:sub(position, position) == "]" then position = position + 1 return result end
    while true do
      result[#result + 1] = value()
      whitespace()
      local char = text:sub(position, position)
      position = position + 1
      if char == "]" then return result end
      if char ~= "," then error("invalid JSON array") end
      whitespace()
    end
  end
  local function object_value()
    position = position + 1
    local result = {}
    whitespace()
    if text:sub(position, position) == "}" then position = position + 1 return result end
    while true do
      if text:sub(position, position) ~= '"' then error("invalid JSON object key") end
      local key = string_value()
      whitespace()
      if text:sub(position, position) ~= ":" then error("invalid JSON object") end
      position = position + 1
      result[key] = value()
      whitespace()
      local char = text:sub(position, position)
      position = position + 1
      if char == "}" then return result end
      if char ~= "," then error("invalid JSON object") end
      whitespace()
    end
  end
  value = function()
    whitespace()
    local char = text:sub(position, position)
    if char == '"' then return string_value() end
    if char == "{" then return object_value() end
    if char == "[" then return array_value() end
    local literal = text:sub(position):match("^-?%d+%.?%d*[eE]?[-+]?%d*")
    if literal and literal ~= "" then position = position + #literal return tonumber(literal) end
    if text:sub(position, position + 3) == "null" then position = position + 4 return nil end
    for word, decoded in pairs({["true"] = true, ["false"] = false}) do
      if text:sub(position, position + #word - 1) == word then position = position + #word return decoded end
    end
    error("invalid JSON value")
  end
  local result = value()
  whitespace()
  if position <= #text then error("trailing JSON data") end
  return result
end

local function read_analysis()
  local block = read_analysis_block()
  return block and decode_json(block) or nil
end

local function remove_previous_managed_tracks()
  local managed = read_managed_guids()
  -- A GUID in the sidecar alone is not enough: preserve any track whose current name is not vgt-owned.
  for index = reaper.CountTracks(0) - 1, 0, -1 do
    local track = reaper.GetTrack(0, index)
    if managed[reaper.GetTrackGUID(track)] and starts_with_vgt(track) then
      reaper.DeleteTrack(track)
    end
  end
end

local function find_track_by_guid(guid)
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if reaper.GetTrackGUID(track) == guid then return track end
  end
  return nil
end

local function add_labeled_item(track, start_time, end_time, label, locked)
  if end_time <= start_time then return end
  local item = reaper.AddMediaItemToTrack(track)
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", start_time)
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", end_time - start_time)
  reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
  -- REAPER locks items rather than whole tracks; locking a vgt label item
  -- makes it read-only in the arrange view. Chord items are deliberately left
  -- unlocked (locked == false) so the user can correct them on the timeline;
  -- vgt_sync.lua reads those edits back into the sidecar.
  if locked ~= false then reaper.SetMediaItemInfo_Value(item, "C_LOCK", 1) end
  reaper.GetSetMediaItemInfo_String(item, "P_NOTES", label, true)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", label, true)
end

local function add_locked_track(index, name, muted)
  reaper.InsertTrackAtIndex(index, true)
  local track = reaper.GetTrack(0, index)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", muted and 1 or 0)
  return track
end

local function reference_start_and_end(reference)
  local start_time, end_time = nil, nil
  for index = 0, reaper.CountTrackMediaItems(reference) - 1 do
    local item = reaper.GetTrackMediaItem(reference, index)
    local start = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local finish = start + reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    start_time = start_time and math.min(start_time, start) or start
    end_time = end_time and math.max(end_time, finish) or finish
  end
  return start_time or 0, end_time or 0
end

local function parse_time_signature(value)
  local numerator, denominator = tostring(value or "4/4"):match("^(%d+)%s*/%s*(%d+)$")
  return tonumber(numerator) or 4, tonumber(denominator) or 4
end

local function is_single_default_tempo_marker()
  local count = reaper.CountTempoTimeSigMarkers(0)
  -- A fresh REAPER project has no *explicit* marker, but its project-settings
  -- tempo is semantically the single default marker described by the rule.
  if count == 0 then
    local numerator, denominator, bpm = reaper.TimeMap_GetTimeSigAtTime(0, 0)
    return math.abs(bpm - 120) < 0.001 and numerator == 4 and denominator == 4
  end
  if count ~= 1 then return false end
  local ok, time, _, _, bpm, numerator, denominator = reaper.GetTempoTimeSigMarker(0, 0)
  return ok and math.abs(time) < 0.000001 and math.abs(bpm - 120) < 0.001 and numerator == 4 and denominator == 4
end

local function apply_tempo_map(tempo, reference_start)
  local bpm = tonumber(tempo.bpm)
  if not bpm or bpm <= 0 then return false end
  local numerator, denominator = parse_time_signature(tempo.time_signature)
  -- On a refresh, clear every marker vgt previously wrote first (index 0 can
  -- only be updated, never deleted) so the new map is built from a clean
  -- slate -- otherwise a stale marker left at the old downbeat/span times
  -- would linger and make the map internally inconsistent.
  for index = reaper.CountTempoTimeSigMarkers(0) - 1, 1, -1 do
    reaper.DeleteTempoTimeSigMarker(0, index)
  end
  -- Update the one default marker, then put explicit markers at the analyzed
  -- downbeat / piecewise boundaries.  REAPER owns the map construction.
  reaper.SetTempoTimeSigMarker(0, 0, 0, -1, -1, bpm, numerator, denominator, false)
  local downbeat = reference_start + (tonumber(tempo.downbeat_offset_seconds) or 0)
  reaper.SetTempoTimeSigMarker(0, -1, downbeat, 0, 0, bpm, numerator, denominator, false)
  if tempo.mode == "piecewise" and type(tempo.spans) == "table" then
    for _, span in ipairs(tempo.spans) do
      local span_bpm = tonumber(span.bpm)
      if span_bpm and span_bpm > 0 then
        reaper.SetTempoTimeSigMarker(0, -1, reference_start + (tonumber(span.start_seconds) or 0), -1, -1, span_bpm, numerator, denominator, false)
      end
    end
  end
  return true
end

-- A canonical snapshot of every tempo/time-sig marker currently in the
-- project, in REAPER's own marker order. Comparing this against the
-- fingerprint recorded the last time vgt wrote the map is how a re-apply
-- tells "still exactly what vgt left it as" apart from "user has since
-- edited it by hand" -- only the former is safe to overwrite.
local function current_tempo_fingerprint()
  local parts = {}
  for index = 0, reaper.CountTempoTimeSigMarkers(0) - 1 do
    local ok, time, _, _, bpm, numerator, denominator = reaper.GetTempoTimeSigMarker(0, index)
    if ok then
      parts[#parts + 1] = string.format("%.6f:%.3f:%d:%d", time, bpm, numerator, denominator)
    end
  end
  return table.concat(parts, ";")
end

local function add_beat_markers(track, tempo, reference_start, reference_end)
  local bpm = tonumber(tempo.bpm)
  if not bpm or bpm <= 0 or reference_end <= reference_start then return end
  local interval = 60 / bpm
  local time = reference_start + (tonumber(tempo.downbeat_offset_seconds) or 0)
  while time > reference_start do time = time - interval end
  local beat = 1
  while time < reference_end do
    if time >= reference_start then add_labeled_item(track, time, math.min(time + interval, reference_end), "Beat " .. beat) end
    time = time + interval
    beat = beat + 1
  end
end

local function offer_beats_track(index, tempo, reference_start, reference_end, managed_tracks)
  -- This is an item-label-only track, so it does not need muting. Keeping it
  -- unmuted ensures its beat labels remain readable in REAPER.
  local beats = add_locked_track(index, BEATS_NAME, false)
  add_beat_markers(beats, tempo, reference_start, reference_end)
  managed_tracks[#managed_tracks + 1] = beats
end

-- Imports the rendered click WAV (analysis.tempo.value.click_artifact_path,
-- a filename under the project's vgt/<namespace>/ folder) as a muted audio track, so
-- it never surprises the user with playback -- unlike Beats/Chords, which
-- are read rather than heard, this one only needs to exist for the user to
-- unmute it. Absent gracefully if `vgt analyze` has not produced the
-- artifact yet, or if no namespace has been recorded yet.
local function add_click_track(index, tempo, reference_start, managed_tracks, artifact_namespace)
  local filename = tempo.click_artifact_path
  if not filename or filename == "" then return end
  if not artifact_namespace or artifact_namespace == "" then return end
  local click_path = project_dir() .. "vgt/" .. tostring(artifact_namespace) .. "/" .. tostring(filename)
  local probe = io.open(click_path, "rb")
  if not probe then return end
  probe:close()
  local source = reaper.PCM_Source_CreateFromFile(click_path)
  if not source then return end
  local click_track = add_locked_track(index, CLICK_NAME, true)
  local item = reaper.AddMediaItemToTrack(click_track)
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", reference_start)
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.GetMediaSourceLength(source))
  -- Tempo maps must never stretch vgt-owned audio.
  reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.SetMediaItemTake_Source(take, source)
  managed_tracks[#managed_tracks + 1] = click_track
end

local function warn(message)
  reaper.ShowConsoleMsg("vgt: " .. message .. "\n")
end

-- Separation can take several minutes and is the Python process's exclusive
-- sidecar-writing window.  A current heartbeat (or, for older producers, the
-- start timestamp) means we must leave both the project and sidecar alone.
-- Timestamps are UTC ISO-8601, which is deliberately parsed without relying
-- on a machine-local timezone.
local function utc_timestamp_seconds(value)
  if type(value) ~= "string" then return nil end
  local year, month, day, hour, minute, second = value:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)T(%d%d):(%d%d):(%d%d)Z$")
  if not year then return nil end
  return os.time({year = tonumber(year), month = tonumber(month), day = tonumber(day), hour = tonumber(hour), min = tonumber(minute), sec = tonumber(second), isdst = false})
end

local function stems_lease_is_live(stems)
  local lease = type(stems) == "table" and stems.in_progress or nil
  if type(lease) ~= "table" then return false end
  local timestamp = utc_timestamp_seconds(lease.heartbeat_at or lease.heartbeat or lease.started_at or lease.start)
  -- `os.time(table)` uses the local timezone.  Interpret both ends the same
  -- way so the elapsed-time comparison remains UTC-correct on every host.
  local utc_now = os.time(os.date("!*t"))
  return timestamp ~= nil and utc_now - timestamp >= 0 and utc_now - timestamp < STEM_LEASE_TIMEOUT_SECONDS
end

-- `artifact.file` is stored relative to the song folder -- the same form the
-- separator writes (see separation.py's artifact_path) -- so it always carries
-- its own `vgt/<namespace>/` prefix. Rebuilding that prefix here and demanding
-- an exact match is what keeps the import confined to the committed namespace.
local function valid_stem_artifact(artifact, expected_filename, artifact_namespace)
  if type(artifact) ~= "table" or type(artifact.file) ~= "string" then return nil, "sidecar record is missing file" end
  if not artifact_namespace or artifact_namespace == "" or artifact_namespace:find("[\\/]") or artifact_namespace:find("%.%.") then
    return nil, "sidecar artifact namespace is invalid"
  end
  if artifact.file ~= "vgt/" .. artifact_namespace .. "/" .. expected_filename then
    return nil, "sidecar file is outside the expected stem namespace"
  end
  local path = project_dir() .. artifact.file
  local file = io.open(path, "rb")
  if not file then return nil, "WAV is missing" end
  local size = file:seek("end")
  file:seek("set", 0)
  local header = file:read(12) or ""
  file:close()
  if type(artifact.size_bytes) ~= "number" or artifact.size_bytes <= 0 or size ~= artifact.size_bytes then
    return nil, "WAV size does not match its committed artifact record"
  end
  if type(artifact.duration_seconds) ~= "number" or artifact.duration_seconds <= 0 then
    return nil, "WAV duration is invalid in its committed artifact record"
  end
  if header:sub(1, 4) ~= "RIFF" or header:sub(9, 12) ~= "WAVE" then return nil, "WAV header is invalid" end
  return path
end

-- Import only records that the separator committed into its own
-- namespace.  The API gets an absolute local path, but saving the project is
-- what causes REAPER to serialize it project-relative; the live verifier
-- checks that persisted behavior separately.
local function add_stem_tracks(index, stems, reference_start, managed_tracks)
  if type(stems) ~= "table" then return end
  local artifacts = stems.artifacts
  local artifact_namespace = stems.artifact_namespace
  if type(artifacts) ~= "table" then return end
  if next(artifacts) == nil then return end -- separation has not produced any outputs yet.
  for _, definition in ipairs(STEM_TRACKS) do
    -- Omitted optional records are not an error: strings/piano are opt-in.
    if artifacts[definition.artifact] ~= nil then
      local path, reason = valid_stem_artifact(artifacts[definition.artifact], definition.filename, artifact_namespace)
      if not path then
        warn("skipping stem " .. definition.artifact .. ": " .. reason)
      else
        local source = reaper.PCM_Source_CreateFromFile(path)
        if not source then
          warn("skipping stem " .. definition.artifact .. ": REAPER could not open WAV")
        else
          local stem_track = add_locked_track(index, definition.name, false)
          local item = reaper.AddMediaItemToTrack(stem_track)
          reaper.SetMediaItemInfo_Value(item, "D_POSITION", reference_start)
          reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.GetMediaSourceLength(source))
          reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
          local take = reaper.AddTakeToMediaItem(item)
          reaper.SetMediaItemTake_Source(take, source)
          managed_tracks[#managed_tracks + 1] = stem_track
          index = index + 1
        end
      end
    end
  end
end

local function remove_previous_managed_regions()
  local managed = read_managed_region_ids()
  for index = reaper.CountProjectMarkers(0) - 1, 0, -1 do
    local _, is_region, _, _, _, region_id = reaper.EnumProjectMarkers3(0, index)
    if is_region and managed[region_id] then reaper.DeleteProjectMarker(0, region_id, true) end
  end
end

local function add_sections(sections, reference_start)
  local region_ids = {}
  if type(sections) ~= "table" then return end
  for _, section in ipairs(sections) do
    local start_time = reference_start + (tonumber(section.start_seconds) or 0)
    local end_time = reference_start + (tonumber(section.end_seconds) or 0)
    local label = tostring(section.label or section.name or "section")
    if end_time > start_time then
      region_ids[#region_ids + 1] = reaper.AddProjectMarker2(0, true, start_time, end_time, PREFIX .. " " .. label, -1, 0)
    end
  end
  return region_ids
end

local function write_settings(managed_tracks, managed_region_ids, reference, tempo_map_applied, tempo_map_fingerprint, tempo_data_fp, guitar_type)
  -- Preserve any analysis the Python CLI already wrote (schema v4); a fresh
  -- sidecar with no prior analysis stays schema v1, matching Phase 0's
  -- long-standing on-disk format.
  local analysis = read_analysis_block()
  local prior_body = read_sidecar_body() or ""
  local prior_schema = tonumber(prior_body:match('"schema_version"%s*:%s*(%d+)')) or 3
  local schema_version = analysis and math.max(prior_schema, 4) or 1
  local analysis_field = analysis and ('\n  "analysis": ' .. analysis .. ",") or ""

  -- Write a complete replacement beside the sidecar, then rename it into
  -- place.  Python uses the same replace discipline for paid-operation
  -- checkpoints; a crash can therefore leave either complete version, never
  -- a truncated JSON document.
  local temporary_path = sidecar_path() .. ".tmp"
  local file, error_message = io.open(temporary_path, "w")
  if not file then error(error_message) end
  local guids = {}
  for _, track in ipairs(managed_tracks) do guids[#guids + 1] = '"' .. reaper.GetTrackGUID(track) .. '"' end
  local region_ids = {}
  for _, region_id in ipairs(managed_region_ids) do region_ids[#region_ids + 1] = tostring(region_id) end
  file:write(string.format([[{
  "schema_version": %d,%s
  "managed_track_guids": [%s],
  "managed_region_ids": [%s],
  "config": {"reference_track_name": "%s", "reference_track_guid": "%s", "folder_name": "%s", "tempo_map_applied": %s, "tempo_map_fingerprint": "%s", "tempo_data_fingerprint": "%s", "guitar_type": "%s"}
}
]],
    schema_version, analysis_field,
    table.concat(guids, ", "), table.concat(region_ids, ", "),
    escaped(track_name(reference)), reaper.GetTrackGUID(reference),
    escaped(PREFIX .. " " .. track_name(reference)), tempo_map_applied and "true" or "false",
    escaped(tempo_map_fingerprint or ""), escaped(tempo_data_fp or ""), escaped(guitar_type)))
  file:close()
  local renamed, rename_error = os.rename(temporary_path, sidecar_path())
  if not renamed then
    os.remove(temporary_path)
    error(rename_error)
  end
end

local function apply()
  local path = project_path()
  if path == "" then error("Save the REAPER project before running vgt Phase 0.") end

  local analysis = read_analysis()
  if analysis and stems_lease_is_live(analysis.stems) then
    warn("stem separation is in progress; retry after it finishes")
    return
  end

  -- Ask before touching the project so dismissing either declaration menu
  -- leaves both REAPER and the sidecar unchanged.
  local guitar_type = choose_guitar_type()
  if not guitar_type then return end

  -- Choose the reference before mutating anything, so cancelling leaves the project untouched.
  local reference = choose_reference(candidate_tracks())
  if not reference then return end
  local reference_guid = reaper.GetTrackGUID(reference)
  local folder_name = PREFIX .. " " .. track_name(reference)

  -- Choosing a reference may involve an interactive pause. Check again
  -- immediately before the first project mutation so a separator that began
  -- while the menu was open wins cleanly rather than having its sidecar
  -- checkpoint overwritten by this apply.
  analysis = read_analysis()
  if analysis and stems_lease_is_live(analysis.stems) then
    warn("stem separation is in progress; retry after it finishes")
    return
  end

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)
  remove_previous_managed_tracks()
  remove_previous_managed_regions()

  -- Re-resolve the reference by GUID: deleting the previous managed tracks can invalidate the pointer.
  reference = find_track_by_guid(reference_guid)
  if not reference then
    reaper.PreventUIRefresh(-1)
    error("the chosen reference track no longer exists.")
  end

  local insert_at = reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(insert_at, true)
  local folder = reaper.GetTrack(0, insert_at)
  reaper.GetSetMediaTrackInfo_String(folder, "P_NAME", folder_name, true)
  -- Tentatively open a folder; if nothing ends up nested under it (no tempo,
  -- click, stems, chords, or sections to add), it is flattened back to a
  -- plain track below rather than left as a folder with no children.
  reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)

  local managed_tracks = {folder}
  local reference_start, reference_end = reference_start_and_end(reference)
  local tempo = analysis and analysis.tempo and analysis.tempo.value
  local artifact_namespace = analysis and analysis.stems and analysis.stems.artifact_namespace
  local tempo_map_applied = prior_tempo_map_applied()
  local tempo_map_fingerprint = ""
  local tempo_data_fp = ""
  if type(tempo) == "table" and tonumber(tempo.bpm) then
    tempo_data_fp = tempo_data_fingerprint(tempo)
    local prior_map_fingerprint = prior_tempo_map_fingerprint()
    local map_untouched = tempo_map_applied and prior_map_fingerprint ~= "" and current_tempo_fingerprint() == prior_map_fingerprint
    if map_untouched and tempo_data_fp == prior_tempo_data_fingerprint() then
      -- Live map still matches what vgt wrote, and the tempo data hasn't
      -- changed either -- nothing to do. Rewriting an unchanged map would
      -- needlessly re-shift any beat-attached reference items.
      tempo_map_fingerprint = prior_map_fingerprint
    elseif map_untouched then
      -- The live map is byte-for-byte what vgt wrote last time, but the
      -- detected/corrected tempo data has since changed -- safe to refresh.
      if apply_tempo_map(tempo, reference_start) then
        tempo_map_fingerprint = current_tempo_fingerprint()
        -- Rewriting the map can shift any beat-attached reference item;
        -- re-read its position so chords/sections/beats placed below land
        -- exactly where the reference audio now actually sits.
        reference_start, reference_end = reference_start_and_end(reference)
      else
        tempo_map_applied = false
        tempo_data_fp = ""
      end
    elseif tempo_map_applied then
      -- Either the user has since edited the map vgt wrote, or an older
      -- sidecar recorded no fingerprint to check against. Either way we
      -- cannot prove the map is still ours, so it is never touched again;
      -- offer the latest tempo data non-invasively instead.
      tempo_data_fp = ""
      offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)
    elseif is_single_default_tempo_marker() then
      tempo_map_applied = apply_tempo_map(tempo, reference_start)
      if tempo_map_applied then
        tempo_map_fingerprint = current_tempo_fingerprint()
        reference_start, reference_end = reference_start_and_end(reference)
      else
        tempo_data_fp = ""
      end
    else
      tempo_data_fp = ""
      offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)
    end
  end

  if type(tempo) == "table" then
    add_click_track(reaper.CountTracks(0), tempo, reference_start, managed_tracks, artifact_namespace)
  end

  local chords = analysis and analysis.chords and analysis.chords.value
  local segments = type(chords) == "table" and (chords.segments or chords) or nil
  if type(segments) == "table" then
    -- Like Beats, this is an item-label-only track: muting it would only
    -- make the chord labels unreadable in REAPER, since there is no audio
    -- on it to silence.
    local chords_track = add_locked_track(reaper.CountTracks(0), CHORDS_NAME, false)
    for _, chord in ipairs(segments) do
      -- locked = false: chord items are the editing surface for corrections (see add_labeled_item).
      add_labeled_item(chords_track, reference_start + (tonumber(chord.start_seconds) or 0), reference_start + (tonumber(chord.end_seconds) or 0), tostring(chord.chord or chord.label or "N"), false)
    end
    managed_tracks[#managed_tracks + 1] = chords_track
  end

  add_stem_tracks(reaper.CountTracks(0), analysis and analysis.stems, reference_start, managed_tracks)

  local managed_region_ids = add_sections(analysis and analysis.sections and analysis.sections.value, reference_start) or {}

  if #managed_tracks > 1 then
    -- The folder must close after every child we appended.
    reaper.SetMediaTrackInfo_Value(managed_tracks[#managed_tracks], "I_FOLDERDEPTH", -1)
  else
    -- Nothing ended up nested under the folder track -- flatten it back to a
    -- plain track rather than leave a folder with no children.
    reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 0)
  end

  write_settings(managed_tracks, managed_region_ids, reference, tempo_map_applied, tempo_map_fingerprint, tempo_data_fp, guitar_type)
  reaper.MarkProjectDirty(0)
  reaper.UpdateArrange()
  reaper.PreventUIRefresh(-1)
  reaper.Undo_EndBlock("vgt: prepare managed practice area", -1)
end

local ok, error_message = xpcall(apply, debug.traceback)
if not ok then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("vgt Phase 0 failed:\n" .. error_message, "vgt", 0)
end
