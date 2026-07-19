-- vgt sync action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while a vgt-prepared
-- project is open. It synchronizes every manual REAPER edit back into the
-- adjacent .vgt sidecar in one invocation:
--   * the (user-editable) [vgt] Chords track's items -- position, length,
--     take name -- become analysis.chords.value.segments;
--   * the [vgt]-owned section regions -- start, end, name -- become
--     analysis.sections.value.
-- Both are written as human_verified: true, so the corrections survive
-- future `vgt analyze` and apply runs. This consolidates the formerly
-- separate chord and section read-back actions into a single action (#33).
-- It never edits the RPP: only vgt_initialize.lua mutates REAPER projects,
-- per the "ReaScript is a thin caller" rule -- this action's own job is
-- entirely REAPER-state bookkeeping, symmetric to how vgt_initialize.lua's
-- write_settings() already writes the sidecar directly from Lua.

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

local function read_managed_region_ids()
  local body = read_sidecar_body()
  if not body then return {} end
  local ids = {}
  local array = body:match('"managed_region_ids"%s*:%s*%[(.-)%]') or ""
  for id in array:gmatch("%-?%d+") do ids[tonumber(id)] = true end
  return ids
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

local function reference_track()
  local body = read_sidecar_body() or ""
  local guid = body:match('"reference_track_guid"%s*:%s*"({[%x%-]+})"')
  if not guid then error("sidecar has no config.reference_track_guid; run vgt_initialize.lua first.") end
  local track = find_track_by_guid(guid)
  if not track then error("the reference track no longer exists.") end
  return track
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

-- Balanced-brace object-span finder, matching vgt_initialize.lua's
-- read_analysis_block(), generalized to any object-valued key so it can
-- locate both "analysis" (top-level) and "chords"/"sections" (inside
-- analysis).
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
-- decoder: analysis is produced by Python, and chord/section labels are
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
-- always round-trips as "[]" (fine here: the only empty-table case this
-- action can produce is an empty sections list, which is itself an array).
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

local function section_label(name)
  if name:sub(1, #PREFIX) == PREFIX then
    return name:sub(#PREFIX + 1):gsub("^%s+", "")
  end
  -- Ownership comes from the saved region ID, not from presentation. Retain
  -- a user rename even if they removed the conventional [vgt] prefix.
  return name
end

local function managed_regions_as_sections(offset)
  local managed = read_managed_region_ids()
  local sections = {}
  for index = 0, reaper.CountProjectMarkers(0) - 1 do
    local _, is_region, start_time, end_time, name, region_id = reaper.EnumProjectMarkers3(0, index)
    if is_region and managed[region_id] and end_time > start_time then
      sections[#sections + 1] = {
        start_seconds = round6(start_time - offset),
        end_seconds = round6(end_time - offset),
        label = section_label(name or "section"),
      }
    end
  end
  table.sort(sections, function(a, b) return a.start_seconds < b.start_seconds end)
  return sections
end

-- Decode the JSON object stored at analysis.<key> and return its start/end
-- byte offsets within `analysis_text` alongside the decoded table, so the
-- caller can later splice a re-encoded replacement back in.
local function decode_stage(analysis_text, key)
  local start, finish = find_object_span(analysis_text, key)
  if not start then error("sidecar analysis has no " .. key .. " stage; run `vgt analyze` first.") end
  return start, finish, decode_json(analysis_text:sub(start, finish))
end

-- Splice corrected `chords` segments and `sections` entries into the
-- sidecar's analysis.chords.value.segments and analysis.sections.value,
-- setting each stage's human_verified/verified_at, leaving every other
-- byte of the sidecar (other stages, detected baselines, config, formatting)
-- untouched. Both stages are located before either is rewritten, then
-- spliced back in from the last span to the first, so rewriting one stage's
-- (possibly different-length) text never invalidates the other's offsets.
local function write_sync(segments, sections)
  local body = read_sidecar_body()
  if not body then error("No .vgt sidecar found; run vgt_initialize.lua first.") end

  local analysis_start, analysis_end = find_object_span(body, "analysis")
  if not analysis_start then error("sidecar has no analysis block; run `vgt analyze` first.") end
  local analysis_text = body:sub(analysis_start, analysis_end)

  local timestamp = os.date("!%Y-%m-%dT%H:%M:%SZ")

  local chords_start, chords_end, chords_decoded = decode_stage(analysis_text, "chords")
  chords_decoded.value = type(chords_decoded.value) == "table" and chords_decoded.value or {}
  chords_decoded.value.segments = segments
  chords_decoded.human_verified = true
  chords_decoded.verified_at = timestamp

  local sections_start, sections_end, sections_decoded = decode_stage(analysis_text, "sections")
  sections_decoded.value = sections
  sections_decoded.human_verified = true
  sections_decoded.verified_at = timestamp

  local edits = {
    {start = chords_start, finish = chords_end, text = encode_json(chords_decoded)},
    {start = sections_start, finish = sections_end, text = encode_json(sections_decoded)},
  }
  table.sort(edits, function(a, b) return a.start > b.start end)
  for _, edit in ipairs(edits) do
    analysis_text = analysis_text:sub(1, edit.start - 1) .. edit.text .. analysis_text:sub(edit.finish + 1)
  end

  local new_body = body:sub(1, analysis_start - 1) .. analysis_text .. body:sub(analysis_end + 1)

  local file, error_message = io.open(sidecar_path(), "w")
  if not file then error(error_message) end
  file:write(new_body)
  file:close()
end

local function sync()
  if project_path() == "" then error("Save the REAPER project before syncing.") end

  local chords_track = find_vgt_chords_track()
  if not chords_track then error("No " .. CHORDS_NAME .. " track found; run vgt_initialize.lua first.") end

  local offset = reference_start(reference_track())
  local segments = chord_items_as_segments(chords_track, offset)
  local sections = managed_regions_as_sections(offset)

  -- This action only ever writes the sidecar file, never REAPER project
  -- state, so there is nothing to wrap in an undo block.
  write_sync(segments, sections)

  -- ShowConsoleMsg (not ShowMessageBox) on success: a modal dialog here
  -- would block headless/automated runs waiting for a click that never
  -- comes, matching vgt_initialize.lua's convention of only popping a
  -- blocking message box on failure.
  reaper.ShowConsoleMsg(string.format(
    "vgt: synced %d chord item(s) and %d section region(s) into the sidecar as human-verified.\n",
    #segments, #sections
  ))
end

local ok, error_message = xpcall(sync, debug.traceback)
if not ok then
  reaper.ShowMessageBox("vgt: sync failed:\n" .. error_message, "vgt", 0)
end
