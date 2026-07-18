-- vgt Phase 0 apply action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.
-- It is the only writer of REAPER projects: the Python CLI intentionally never edits RPP text.

local PREFIX = "[vgt]"
local MIRROR_NAME = PREFIX .. " Mirror"

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

-- The Python `vgt analyze` stage (schema v2) adds a top-level "analysis"
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
  for index = brace_start, #body do
    local char = body:sub(index, index)
    if char == "{" then
      depth = depth + 1
    elseif char == "}" then
      depth = depth - 1
      if depth == 0 then return body:sub(brace_start, index) end
    end
  end
  return nil
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
      end
    end
  end
end

local function write_settings(folder, mirror, reference)
  -- Preserve any analysis the Python CLI already wrote (schema v2); a fresh
  -- sidecar with no prior analysis stays schema v1, matching Phase 0's
  -- long-standing on-disk format.
  local analysis = read_analysis_block()
  local schema_version = analysis and 2 or 1
  local analysis_field = analysis and ('\n  "analysis": ' .. analysis .. ",") or ""

  local file, error_message = io.open(sidecar_path(), "w")
  if not file then error(error_message) end
  file:write(string.format([[{
  "schema_version": %d,%s
  "managed_track_guids": ["%s", "%s"],
  "config": {"reference_track_name": "%s", "reference_track_guid": "%s", "folder_name": "%s", "mirror_name": "%s"}
}
]],
    schema_version, analysis_field,
    reaper.GetTrackGUID(folder), reaper.GetTrackGUID(mirror),
    escaped(track_name(reference)), reaper.GetTrackGUID(reference),
    escaped(PREFIX .. " " .. track_name(reference)), escaped(MIRROR_NAME)))
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
  remove_previous_managed_tracks()

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
  reaper.SetMediaTrackInfo_Value(mirror, "I_FOLDERDEPTH", -1)

  -- Clone only the chosen reference track's file-backed media. Every other track stays untouched.
  copy_file_backed_items(reference, mirror)

  write_settings(folder, mirror, reference)
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
