-- vgt read-sections action for REAPER 7.x.
--
-- Run this while a vgt-prepared project is open after renaming or moving its
-- section regions. It reads only region IDs that vgt previously recorded in
-- the adjacent sidecar, then stores their names and boundaries as a
-- human-verified `analysis.sections` value. It never changes REAPER state.

local PREFIX = "[vgt]"

local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
end

local function sidecar_path()
  return (project_path():gsub("%.[^./\\]*$", "") .. ".vgt")
end

local function read_sidecar_body()
  local file = io.open(sidecar_path(), "r")
  if not file then return nil end
  local body = file:read("*a")
  file:close()
  return body
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

local function find_object_span(haystack, key)
  local key_start = haystack:find('"' .. key .. '"%s*:%s*{')
  if not key_start then return nil end
  local brace_start = haystack:find("{", key_start)
  local depth, in_string, escaped_char = 0, false, false
  for index = brace_start, #haystack do
    local char = haystack:sub(index, index)
    if in_string then
      if escaped_char then escaped_char = false
      elseif char == "\\" then escaped_char = true
      elseif char == '"' then in_string = false end
    elseif char == '"' then in_string = true
    elseif char == "{" then depth = depth + 1
    elseif char == "}" then
      depth = depth - 1
      if depth == 0 then return brace_start, index end
    end
  end
  return nil
end

-- Return the JSON value span belonging to `key` in a known object. The value
-- may itself be an array/object containing strings, so a simple pattern is
-- unsafe for corrected labels such as `Verse {A}`.
local function top_level_value_start(object_text, key)
  local depth, in_string, escaped_char = 0, false, false
  for index = 1, #object_text do
    local char = object_text:sub(index, index)
    if in_string then
      if escaped_char then escaped_char = false
      elseif char == "\\" then escaped_char = true
      elseif char == '"' then in_string = false end
    elseif char == '"' then
      -- A section label is nested inside `value`, at depth two or more. Only
      -- accept a field name directly inside the stage object, so a label such
      -- as `"human_verified"` cannot be mistaken for the real field.
      local finish = index + #key + 1
      if depth == 1 and object_text:sub(index, finish) == '"' .. key .. '"' then
        local cursor = finish + 1
        while object_text:sub(cursor, cursor):match("%s") do cursor = cursor + 1 end
        if object_text:sub(cursor, cursor) == ":" then return cursor + 1 end
      end
      in_string = true
    elseif char == "{" or char == "[" then depth = depth + 1
    elseif char == "}" or char == "]" then depth = depth - 1 end
  end
  return nil
end

local function find_value_span(object_text, key)
  local start = top_level_value_start(object_text, key)
  if not start then return nil end
  while object_text:sub(start, start):match("%s") do start = start + 1 end
  local first = object_text:sub(start, start)
  if first ~= "{" and first ~= "[" and first ~= '"' then
    local finish = object_text:find("[,}]", start)
    return start, (finish or (#object_text + 1)) - 1
  end
  local opening, closing = first, first == "{" and "}" or first == "[" and "]" or '"'
  local depth, in_string, escaped_char = 0, false, false
  for index = start, #object_text do
    local char = object_text:sub(index, index)
    if in_string then
      if escaped_char then escaped_char = false
      elseif char == "\\" then escaped_char = true
      elseif char == '"' then
        in_string = false
        if opening == '"' and depth == 0 then return start, index end
      end
    elseif char == '"' then in_string = true
    elseif opening ~= '"' and char == opening then depth = depth + 1
    elseif opening ~= '"' and char == closing then
      depth = depth - 1
      if depth == 0 then return start, index end
    end
  end
  return nil
end

local function encode_string(value)
  local out = {'"'}
  for index = 1, #value do
    local char, byte = value:sub(index, index), value:byte(index)
    if char == '"' then out[#out + 1] = '\\"'
    elseif char == "\\" then out[#out + 1] = "\\\\"
    elseif char == "\n" then out[#out + 1] = "\\n"
    elseif char == "\r" then out[#out + 1] = "\\r"
    elseif char == "\t" then out[#out + 1] = "\\t"
    elseif byte < 0x20 then out[#out + 1] = string.format("\\u%04x", byte)
    else out[#out + 1] = char end
  end
  out[#out + 1] = '"'
  return table.concat(out)
end

local function round6(value)
  return math.floor(value * 1e6 + 0.5) / 1e6
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

local function encode_sections(sections)
  local encoded = {}
  for _, section in ipairs(sections) do
    encoded[#encoded + 1] = string.format('{"label":%s,"start_seconds":%.6f,"end_seconds":%.6f}',
      encode_string(section.label), section.start_seconds, section.end_seconds)
  end
  return "[" .. table.concat(encoded, ",") .. "]"
end

local function replace_stage_value(stage_text, key, replacement)
  local start, finish = find_value_span(stage_text, key)
  if not start then error("sidecar sections stage has no " .. key .. " field; run `vgt analyze` first.") end
  return stage_text:sub(1, start - 1) .. replacement .. stage_text:sub(finish + 1)
end

local function write_corrected_sections(sections)
  local body = read_sidecar_body()
  if not body then error("No .vgt sidecar found; run vgt_initialize.lua first.") end
  local analysis_start, analysis_end = find_object_span(body, "analysis")
  if not analysis_start then error("sidecar has no analysis block; run `vgt analyze` first.") end
  local analysis_text = body:sub(analysis_start, analysis_end)
  local sections_start, sections_end = find_object_span(analysis_text, "sections")
  if not sections_start then error("sidecar analysis has no sections stage; run `vgt analyze` first.") end
  local stage_text = analysis_text:sub(sections_start, sections_end)
  stage_text = replace_stage_value(stage_text, "value", encode_sections(sections))
  stage_text = replace_stage_value(stage_text, "human_verified", "true")
  stage_text = replace_stage_value(stage_text, "verified_at", encode_string(os.date("!%Y-%m-%dT%H:%M:%SZ")))
  local new_analysis = analysis_text:sub(1, sections_start - 1) .. stage_text .. analysis_text:sub(sections_end + 1)
  local file, error_message = io.open(sidecar_path(), "w")
  if not file then error(error_message) end
  file:write(body:sub(1, analysis_start - 1) .. new_analysis .. body:sub(analysis_end + 1))
  file:close()
end

local function read_sections()
  if project_path() == "" then error("Save the REAPER project before reading sections back.") end
  local sections = managed_regions_as_sections(reference_start(reference_track()))
  write_corrected_sections(sections)
  reaper.ShowConsoleMsg(string.format("vgt: read %d section region(s) into the sidecar as human-verified.\n", #sections))
end

local ok, error_message = xpcall(read_sections, debug.traceback)
if not ok then reaper.ShowMessageBox("vgt: reading sections failed:\n" .. error_message, "vgt", 0) end
