-- vgt read-chords action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while a vgt-prepared
-- project is open. It reads the (user-editable) [vgt] Chords track's items --
-- position, length, take name -- and writes them back into the adjacent
-- .vgt sidecar as a human-verified `analysis.chords` stage, so the correction
-- survives future `vgt analyze` and apply runs. It never edits the RPP: only
-- vgt_initialize.lua mutates REAPER projects, per the "ReaScript is a thin
-- caller" rule -- this action's own job is entirely REAPER-state bookkeeping,
-- symmetric to how vgt_initialize.lua's write_settings() already writes the
-- sidecar directly from Lua.

local PREFIX = "[vgt]"
local CHORDS_NAME = PREFIX .. " Chords"

local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
end

local function sidecar_path()
  local path = project_path()
  return (path:gsub("%.[^./\\]*$", "") .. ".vgt")
end

local function track_name(track)
  local _, name = reaper.GetTrackName(track, "")
  return name
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
  local array = body:match('"managed_track_guids"%s*:%s*%[(.-)%]') or ""
  for guid in array:gmatch("{[%x%-]+}") do guids[guid] = true end
  return guids
end

local function find_track_by_guid(guid)
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if reaper.GetTrackGUID(track) == guid then return track end
  end
  return nil
end

-- Only a track vgt itself created and recorded (by GUID, in
-- managed_track_guids) is eligible -- never touch a user track that merely
-- happens to be named "[vgt] Chords".
local function find_vgt_chords_track()
  local managed = read_managed_guids()
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if track_name(track) == CHORDS_NAME and managed[reaper.GetTrackGUID(track)] then return track end
  end
  return nil
end

local function reference_start(reference)
  local start_time = nil
  for index = 0, reaper.CountTrackMediaItems(reference) - 1 do
    local item = reaper.GetTrackMediaItem(reference, index)
    local start = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    start_time = start_time and math.min(start_time, start) or start
  end
  return start_time or 0
end

-- Same balanced-brace object-span finder vgt_initialize.lua's
-- read_analysis_block() uses, generalized to any object-valued key so it can
-- locate both "analysis" (top-level) and "chords" (inside analysis).
local function find_object_span(haystack, key)
  local key_start = haystack:find('"' .. key .. '"%s*:%s*{')
  if not key_start then return nil end
  local brace_start = haystack:find("{", key_start)
  local depth = 0
  local in_string = false
  local escaped_char = false
  for index = brace_start, #haystack do
    local char = haystack:sub(index, index)
    if in_string then
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
      if depth == 0 then return brace_start, index end
    end
  end
  return nil
end

-- Minimal JSON decoder, matching vgt_initialize.lua's read_analysis()
-- decoder: analysis is produced by Python, and chord labels are
-- user-correctable text that a pattern-scraper would mishandle.
local function decode_json(text)
  local position = 1
  local function whitespace()
    while text:sub(position, position):match("%s") do position = position + 1 end
  end
  local function string_value()
    position = position + 1
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

local function encode_string(value)
  local out = {'"'}
  for index = 1, #value do
    local char = value:sub(index, index)
    local byte = char:byte()
    if char == '"' then
      out[#out + 1] = '\\"'
    elseif char == "\\" then
      out[#out + 1] = "\\\\"
    elseif char == "\n" then
      out[#out + 1] = "\\n"
    elseif char == "\r" then
      out[#out + 1] = "\\r"
    elseif char == "\t" then
      out[#out + 1] = "\\t"
    elseif byte < 0x20 then
      out[#out + 1] = string.format("\\u%04x", byte)
    else
      out[#out + 1] = char
    end
  end
  out[#out + 1] = '"'
  return table.concat(out)
end

-- decode_json() (mirroring standard Lua JSON libraries) represents a JSON
-- array as a table with keys 1..n and a JSON object as a table with string
-- keys; an empty table is ambiguous between the two, so an empty table
-- always round-trips as "[]" (fine here: every object this action touches --
-- chord stage fields -- is non-empty).
local function is_array(value)
  local count = 0
  for _ in pairs(value) do count = count + 1 end
  if count == 0 then return true end
  for index = 1, count do
    if value[index] == nil then return false end
  end
  return true
end

local function encode_json(value)
  local value_type = type(value)
  if value_type == "string" then return encode_string(value) end
  if value_type == "number" then
    if value == math.floor(value) and math.abs(value) < 1e15 then return string.format("%d", value) end
    return string.format("%.10g", value)
  end
  if value_type == "boolean" then return value and "true" or "false" end
  if value_type == "nil" then return "null" end
  if value_type == "table" then
    if is_array(value) then
      local parts = {}
      for _, item in ipairs(value) do parts[#parts + 1] = encode_json(item) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local keys = {}
    for key in pairs(value) do keys[#keys + 1] = key end
    table.sort(keys)
    local parts = {}
    for _, key in ipairs(keys) do parts[#parts + 1] = encode_string(key) .. ":" .. encode_json(value[key]) end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  error("cannot encode value of type " .. value_type)
end

local function round6(value)
  return math.floor(value * 1e6 + 0.5) / 1e6
end

-- Splice a corrected `segments` array into the sidecar's
-- analysis.chords.value.segments, and set analysis.chords.human_verified,
-- leaving every other byte of the sidecar (other stages, config, formatting)
-- untouched.
local function write_corrected_chords(segments)
  local body = read_sidecar_body()
  if not body then error("No .vgt sidecar found; run vgt_initialize.lua first.") end

  local analysis_start, analysis_end = find_object_span(body, "analysis")
  if not analysis_start then error("sidecar has no analysis block; run `vgt analyze` first.") end
  local analysis_text = body:sub(analysis_start, analysis_end)

  local chords_start, chords_end = find_object_span(analysis_text, "chords")
  if not chords_start then error("sidecar analysis has no chords stage; run `vgt analyze` first.") end
  local chords_text = analysis_text:sub(chords_start, chords_end)

  local decoded = decode_json(chords_text)
  decoded.value = type(decoded.value) == "table" and decoded.value or {}
  decoded.value.segments = segments
  decoded.human_verified = true
  decoded.verified_at = os.date("!%Y-%m-%dT%H:%M:%SZ")

  local new_chords_text = encode_json(decoded)
  local new_analysis_text = analysis_text:sub(1, chords_start - 1) .. new_chords_text .. analysis_text:sub(chords_end + 1)
  local new_body = body:sub(1, analysis_start - 1) .. new_analysis_text .. body:sub(analysis_end + 1)

  local file, error_message = io.open(sidecar_path(), "w")
  if not file then error(error_message) end
  file:write(new_body)
  file:close()
end

local function reference_track()
  local body = read_sidecar_body() or ""
  local guid = body:match('"reference_track_guid"%s*:%s*"({[%x%-]+})"')
  if not guid then error("sidecar has no config.reference_track_guid; run vgt_initialize.lua first.") end
  local track = find_track_by_guid(guid)
  if not track then error("the reference track no longer exists.") end
  return track
end

local function chord_items_as_segments(chords_track, offset)
  local items = {}
  for index = 0, reaper.CountTrackMediaItems(chords_track) - 1 do
    local item = reaper.GetTrackMediaItem(chords_track, index)
    local position = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local length = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    if length > 0 then
      local take = reaper.GetActiveTake(item)
      local label = take and reaper.GetTakeName(take) or ""
      if label == "" then label = "N" end
      items[#items + 1] = {position = position, length = length, label = label}
    end
  end
  table.sort(items, function(a, b) return a.position < b.position end)

  local segments = {}
  for _, item in ipairs(items) do
    segments[#segments + 1] = {
      start_seconds = round6(item.position - offset),
      end_seconds = round6(item.position + item.length - offset),
      chord = item.label,
    }
  end
  return segments
end

local function read_chords()
  if project_path() == "" then error("Save the REAPER project before reading chords back.") end

  local chords_track = find_vgt_chords_track()
  if not chords_track then error("No " .. CHORDS_NAME .. " track found; run vgt_initialize.lua first.") end

  local offset = reference_start(reference_track())
  local segments = chord_items_as_segments(chords_track, offset)

  -- This action only ever writes the sidecar file, never REAPER project
  -- state, so there is nothing to wrap in an undo block.
  write_corrected_chords(segments)

  -- ShowConsoleMsg (not ShowMessageBox) on success: a modal dialog here
  -- would block headless/automated runs waiting for a click that never
  -- comes, matching vgt_initialize.lua's convention of only popping a
  -- blocking message box on failure.
  reaper.ShowConsoleMsg(string.format("vgt: read %d chord item(s) from %s into the sidecar as human-verified.\n", #segments, CHORDS_NAME))
end

local ok, error_message = xpcall(read_chords, debug.traceback)
if not ok then
  reaper.ShowMessageBox("vgt: reading chords failed:\n" .. error_message, "vgt", 0)
end
