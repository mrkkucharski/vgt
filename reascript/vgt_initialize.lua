-- vgt Phase 0 apply action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.
-- It is the only writer of REAPER projects: the Python CLI intentionally never edits RPP text.

local PREFIX = "[vgt]"
local MIRROR_NAME = PREFIX .. " Mirror"
local CHORDS_NAME = PREFIX .. " Chords"
local BEATS_NAME = PREFIX .. " Beats"

local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
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

-- Candidate reference tracks are the project's own (non-vgt) tracks, in order.
local function candidate_tracks()
  local candidates = {}
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if not starts_with_vgt(track) then candidates[#candidates + 1] = track end
  end
  return candidates
end

-- Ask which track is the reference to mirror. Automation (e.g. the headless
-- verifier) can preselect it via the "vgt"/"reference_index" ExtState, a 0-based
-- index over candidate_tracks(); interactive users get a popup menu. Returns the
-- chosen track, or nil if the user dismissed the menu.
local function choose_reference(candidates)
  if #candidates == 0 then error("No non-[vgt] tracks to use as a reference.") end

  local forced = reaper.GetExtState("vgt", "reference_index")
  if forced ~= "" then
    local index = tonumber(forced)
    if not index or index < 0 or index >= #candidates then
      error("vgt reference_index ExtState is out of range: " .. forced)
    end
    return candidates[index + 1]
  end

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
  -- earlier eligible apply.  We never delete or rewrite that map on re-apply.
  return body:match('"tempo_map_applied"%s*:%s*true') ~= nil
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

local function copy_file_backed_items(source, destination)
  for item_index = 0, reaper.CountTrackMediaItems(source) - 1 do
    local source_item = reaper.GetTrackMediaItem(source, item_index)
    local source_take = reaper.GetActiveTake(source_item)
    if source_take then
      local source_media = reaper.GetMediaItemTake_Source(source_take)
      -- Lua returns the filename directly (unlike several APIs that return an
      -- `ok, value` pair). Keeping it in one variable avoids passing nil to
      -- PCM_Source_CreateFromFile and leaving an empty item behind.
      local filename = reaper.GetMediaSourceFileName(source_media, "")
      if filename ~= "" then
        local item = reaper.AddMediaItemToTrack(destination)
        reaper.SetMediaItemInfo_Value(item, "D_POSITION", reaper.GetMediaItemInfo_Value(source_item, "D_POSITION"))
        reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.GetMediaItemInfo_Value(source_item, "D_LENGTH"))
        local take = reaper.AddTakeToMediaItem(item)
        reaper.SetMediaItemTake_Source(take, reaper.PCM_Source_CreateFromFile(filename))
        reaper.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", reaper.GetMediaItemTakeInfo_Value(source_take, "D_STARTOFFS"))
        reaper.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", reaper.GetMediaItemTakeInfo_Value(source_take, "D_PLAYRATE"))
        reaper.SetMediaItemTakeInfo_Value(take, "D_PITCH", reaper.GetMediaItemTakeInfo_Value(source_take, "D_PITCH"))
        -- Tempo maps must never stretch vgt-owned audio.
        reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
      end
    end
  end
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
  -- vgt_read_chords.lua reads those edits back into the sidecar.
  if locked ~= false then reaper.SetMediaItemInfo_Value(item, "C_LOCK", 1) end
  reaper.GetSetMediaItemInfo_String(item, "P_NOTES", label, true)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", label, true)
end

local function add_locked_muted_track(index, name, muted)
  reaper.InsertTrackAtIndex(index, true)
  local track = reaper.GetTrack(0, index)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)
  -- Muted tracks render dark-on-dark in REAPER; the chords track has no audio
  -- to mute, so it's created unmuted purely so its labels stay readable.
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", muted == false and 0 or 1)
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

local function write_settings(folder, managed_tracks, managed_region_ids, reference, tempo_map_applied)
  -- Preserve any analysis the Python CLI already wrote (schema v4); a fresh
  -- sidecar with no prior analysis stays schema v1, matching Phase 0's
  -- long-standing on-disk format.
  local analysis = read_analysis_block()
  local prior_body = read_sidecar_body() or ""
  local prior_schema = tonumber(prior_body:match('"schema_version"%s*:%s*(%d+)')) or 3
  local schema_version = analysis and math.max(prior_schema, 4) or 1
  local analysis_field = analysis and ('\n  "analysis": ' .. analysis .. ",") or ""

  local file, error_message = io.open(sidecar_path(), "w")
  if not file then error(error_message) end
  local guids = {}
  for _, track in ipairs(managed_tracks) do guids[#guids + 1] = '"' .. reaper.GetTrackGUID(track) .. '"' end
  local region_ids = {}
  for _, region_id in ipairs(managed_region_ids) do region_ids[#region_ids + 1] = tostring(region_id) end
  file:write(string.format([[{
  "schema_version": %d,%s
  "managed_track_guids": [%s],
  "managed_region_ids": [%s],
  "config": {"reference_track_name": "%s", "reference_track_guid": "%s", "folder_name": "%s", "mirror_name": "%s", "tempo_map_applied": %s}
}
]],
    schema_version, analysis_field,
    table.concat(guids, ", "), table.concat(region_ids, ", "),
    escaped(track_name(reference)), reaper.GetTrackGUID(reference),
    escaped(PREFIX .. " " .. track_name(reference)), escaped(MIRROR_NAME), tempo_map_applied and "true" or "false"))
  file:close()
end

local function apply()
  local path = project_path()
  if path == "" then error("Save the REAPER project before running vgt Phase 0.") end

  -- Choose the reference before mutating anything, so cancelling leaves the project untouched.
  local reference = choose_reference(candidate_tracks())
  if not reference then return end
  local reference_guid = reaper.GetTrackGUID(reference)
  local folder_name = PREFIX .. " " .. track_name(reference)

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)
  local analysis = read_analysis()
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
  reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)

  reaper.InsertTrackAtIndex(insert_at + 1, true)
  local mirror = reaper.GetTrack(0, insert_at + 1)
  reaper.GetSetMediaTrackInfo_String(mirror, "P_NAME", MIRROR_NAME, true)
  -- Close the folder after all analysis tracks have been added below.
  reaper.SetMediaTrackInfo_Value(mirror, "I_FOLDERDEPTH", 0)

  -- Clone only the chosen reference track's file-backed media. Every other track stays untouched.
  copy_file_backed_items(reference, mirror)

  local managed_tracks = {folder, mirror}
  local reference_start, reference_end = reference_start_and_end(reference)
  local tempo = analysis and analysis.tempo and analysis.tempo.value
  local tempo_map_applied = prior_tempo_map_applied()
  if type(tempo) == "table" and tonumber(tempo.bpm) then
    if tempo_map_applied then
      -- Already written by vgt on an earlier run; leave the live map alone.
    elseif is_single_default_tempo_marker() then
      tempo_map_applied = apply_tempo_map(tempo, reference_start)
    else
      local beats = add_locked_muted_track(insert_at + 2, BEATS_NAME)
      add_beat_markers(beats, tempo, reference_start, reference_end)
      managed_tracks[#managed_tracks + 1] = beats
    end
  end

  local chords = analysis and analysis.chords and analysis.chords.value
  local segments = type(chords) == "table" and (chords.segments or chords) or nil
  if type(segments) == "table" then
    local chords_track = add_locked_muted_track(reaper.CountTracks(0), CHORDS_NAME, false)
    for _, chord in ipairs(segments) do
      -- locked = false: chord items are the editing surface for corrections (see add_labeled_item).
      add_labeled_item(chords_track, reference_start + (tonumber(chord.start_seconds) or 0), reference_start + (tonumber(chord.end_seconds) or 0), tostring(chord.chord or chord.label or "N"), false)
    end
    managed_tracks[#managed_tracks + 1] = chords_track
  end

  local managed_region_ids = add_sections(analysis and analysis.sections and analysis.sections.value, reference_start) or {}

  -- The folder must close after every child we appended.
  reaper.SetMediaTrackInfo_Value(managed_tracks[#managed_tracks], "I_FOLDERDEPTH", -1)

  write_settings(folder, managed_tracks, managed_region_ids, reference, tempo_map_applied)
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
