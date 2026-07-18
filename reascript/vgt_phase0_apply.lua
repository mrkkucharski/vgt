-- vgt Phase 0 apply action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.
-- It is the only writer of REAPER projects: the Python CLI intentionally never edits RPP text.

local PREFIX = "[vgt]"
local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
end

local function sidecar_path()
  local path = project_path()
  return path:match("^(.*[/\\])") .. "vgt.json"
end

local function escaped(value)
  return value:gsub("\\", "\\\\"):gsub('"', '\\"')
end

local function read_managed_guids()
  local file = io.open(sidecar_path(), "r")
  if not file then return {} end
  local body = file:read("*a")
  file:close()
  local guids = {}
  -- GUIDs are deliberately read only from our schema's managed_track_guids array.
  local array = body:match('"managed_track_guids"%s*:%s*%[(.-)%]') or ""
  for guid in array:gmatch("{[%x%-]+}") do guids[guid] = true end
  return guids
end

local function starts_with_vgt(track)
  local _, name = reaper.GetTrackName(track, "")
  return name:sub(1, #PREFIX) == PREFIX
end

local function remove_previous_managed_tracks()
  local managed = read_managed_guids()
  -- A GUID in vgt.json alone is not enough: preserve any track whose current name is not vgt-owned.
  for index = reaper.CountTracks(0) - 1, 0, -1 do
    local track = reaper.GetTrack(0, index)
    if managed[reaper.GetTrackGUID(track)] and starts_with_vgt(track) then
      reaper.DeleteTrack(track)
    end
  end
end

local function copy_file_backed_items(source, destination)
  for item_index = 0, reaper.CountTrackMediaItems(source) - 1 do
    local source_item = reaper.GetTrackMediaItem(source, item_index)
    local source_take = reaper.GetActiveTake(source_item)
    if source_take then
      local source_media = reaper.GetMediaItemTake_Source(source_take)
      local ok, filename = reaper.GetMediaSourceFileName(source_media, "")
      if ok and filename ~= "" then
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

local function write_settings(folder, mirror)
  local file, error_message = io.open(sidecar_path(), "w")
  if not file then error(error_message) end
  file:write(string.format([[{
  "schema_version": 1,
  "managed_track_guids": ["%s", "%s"],
  "config": {"folder_name": "%s", "mirror_name": "%s"}
}
]], reaper.GetTrackGUID(folder), reaper.GetTrackGUID(mirror), escaped(PREFIX .. " Practice"), escaped(PREFIX .. " Mirror")))
  file:close()
end

local function apply()
  local path = project_path()
  if path == "" then error("Save the REAPER project before running vgt Phase 0.") end
  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)
  remove_previous_managed_tracks()

  local insert_at = reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(insert_at, true)
  local folder = reaper.GetTrack(0, insert_at)
  reaper.GetSetMediaTrackInfo_String(folder, "P_NAME", PREFIX .. " Practice", true)
  reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)

  reaper.InsertTrackAtIndex(insert_at + 1, true)
  local mirror = reaper.GetTrack(0, insert_at + 1)
  reaper.GetSetMediaTrackInfo_String(mirror, "P_NAME", PREFIX .. " Mirror", true)
  reaper.SetMediaTrackInfo_Value(mirror, "I_FOLDERDEPTH", -1)

  -- Clone only file-backed media onto a new vgt track. Existing tracks and their routing stay untouched.
  for index = 0, insert_at - 1 do
    local track = reaper.GetTrack(0, index)
    if not starts_with_vgt(track) then copy_file_backed_items(track, mirror) end
  end
  write_settings(folder, mirror)
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
