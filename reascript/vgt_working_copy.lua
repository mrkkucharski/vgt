-- vgt working-copy action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.
--
-- Purpose: vgt recreates every `[vgt]`-managed object on each apply, so edits to
-- a `[vgt] <Target> Ref (MIDI)` (or any other vgt track) never survive the next
-- `vgt_initialize.lua` run. This action makes a *user-owned* copy of the selected
-- track(s) so they can be edited safely side by side with the vgt references.
--
-- The single design invariant that keeps this non-destructive: a working copy is
-- named `[work] ...` (never `[vgt] ...`) and carries no vgt-ownership mark, so
-- vgt_initialize.lua's remove_previous_managed_tracks leaves it untouched forever
-- -- it deletes only tracks that are *both* vgt-owned *and* still `[vgt]`-named.
-- A separate working-copy marker records which `[work]` objects this action
-- created. Names alone are user-controlled and therefore never prove ownership.
-- Finishing an edit promotes the selected `[work]` track into `[clean]`.

local VGT_PREFIX = "[vgt]"
local WORK_PREFIX = "[work]"
local CLEAN_PREFIX = "[clean]"
-- The same durable ownership mark vgt_initialize.lua sets on its tracks. A copy
-- must never carry it, or a future apply could treat the copy as a stale vgt
-- track to reconcile (the name guard already protects it, but clearing the mark
-- makes the copy correct by both of vgt's ownership tests, not just one).
local VGT_EXT_STATE_KEY = "P_EXT:vgt_managed"
-- This is deliberately distinct from vgt_managed: normal vgt reconciliation
-- must continue to ignore working copies and their container.
local WORK_EXT_STATE_KEY = "P_EXT:vgt_working_copy"
local WORK_EXT_STATE_VALUE = "1"
-- Container identity belongs to vgt_initialize.lua. Working-copy provenance
-- remains deliberately separate, so copies stay outside normal reconciliation.
local CONTAINER_EXT_STATE_KEY = "P_EXT:vgt_container"
local WORK_CONTAINER_KIND = "work"
local PROJ_EXT_SECTION = "vgt"
local PROJ_EXT_CLEAN_GUID_KEY = "clean_container"
local PROJ_EXT_WORK_GUID_KEY = "work_container"
local CLEAN_CONTAINER_KIND = "clean"
local WORK_COLOR = {68, 175, 239}
local CLEAN_COLOR = {187, 210, 41}

local function track_name(track)
  local _, name = reaper.GetTrackName(track, "")
  return name
end

local function starts_with(name, prefix)
  return name:sub(1, #prefix) == prefix
end

-- The user-owned name for a working copy. A leading `[vgt]` or `[work]` prefix
-- is stripped first so a copy -- or a copy of a copy -- never re-enters the
-- `[vgt]` ownership namespace and never grows a `[work] [work] ...` pile-up.
local function working_name(source_name)
  local rest = source_name
  rest = rest:gsub("^%[vgt%]%s*", "")
  rest = rest:gsub("^%[work%]%s*", "")
  if rest == "" then rest = "Track" end
  return WORK_PREFIX .. " " .. rest
end

-- Promotion is a reclaim, not a copy.  Strip every namespace this action can
-- encounter so a finished track is neither re-adopted as `[vgt]` nor given a
-- growing stack of `[clean]`/`[work]` prefixes.
local function clean_name(source_name)
  local rest = source_name
  rest = rest:gsub("^%[vgt%]%s*", "")
  rest = rest:gsub("^%[work%]%s*", "")
  rest = rest:gsub("^%[clean%]%s*", "")
  if rest == "" then rest = "Track" end
  return CLEAN_PREFIX .. " " .. rest
end

-- A track-state chunk carries the source's own TRACKID. Stamp a fresh GUID into
-- the copy's chunk before applying it, since REAPER track GUIDs must be unique
-- and vgt tracks ownership by GUID -- two tracks sharing one GUID would make
-- both the copy and its source look like the same managed object.
local function replace_track_guid(chunk, new_guid)
  return (chunk:gsub("TRACKID {[^}]*}", "TRACKID " .. new_guid, 1))
end

-- The index of the last track nested inside the folder that opens at
-- folder_index, found by walking REAPER's folder-depth accumulation: the region
-- ends on the child whose I_FOLDERDEPTH brings the running depth back to zero.
local function folder_last_child_index(folder_index)
  local count = reaper.CountTracks(0)
  local depth = reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, folder_index), "I_FOLDERDEPTH")
  local last = folder_index
  local index = folder_index + 1
  while index < count and depth > 0 do
    depth = depth + reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, index), "I_FOLDERDEPTH")
    last = index
    index = index + 1
  end
  return last
end

local function is_marked_work_object(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, WORK_EXT_STATE_KEY, "", false)
  return value == WORK_EXT_STATE_VALUE
end

local function is_top_level_track(index)
  local depth = 0
  for previous = 0, index - 1 do
    depth = depth + reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, previous), "I_FOLDERDEPTH")
  end
  return depth == 0
end

-- A workspace made by this action has exactly one folder level and a child
-- which closes it back to the surrounding top-level depth.  Do not try to
-- repair a marked container whose folder depths have been edited or damaged:
-- reusing it could leave later user tracks inside an unclosed folder.  Keeping
-- such a workspace is safer than inferring that its remaining marker permits
-- structural edits.
local function is_complete_work_folder(folder_index, track)
  if reaper.GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH") ~= 1 then return false end
  local last_child = folder_last_child_index(folder_index)
  if last_child <= folder_index then return false end
  for index = folder_index + 1, last_child do
    local depth = reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, index), "I_FOLDERDEPTH")
    -- The action creates a flat list of copies and exactly one -1 closer.
    -- A merely balanced +1/-2 (or any other nested) child shape is user
    -- structural editing, even when every track still has our marker.
    if index == last_child then
      if depth ~= -1 then return false end
    elseif depth ~= 0 then
      return false
    end
  end
  return true
end

-- initialize creates an empty container as an ordinary top-level track. This
-- action is the operation that gives it its first child, so this is the one
-- safe flat shape we may turn into a folder. Any other shape still goes
-- through is_complete_work_folder's exact structural check above.
local function is_empty_work_container(track)
  return reaper.GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH") == 0
end

local function read_work_container_guid()
  -- The guard keeps this helper directly exercisable with the minimal REAPER
  -- stubs used by the offline tests; REAPER itself always provides this API.
  if not reaper.GetProjExtState then return "" end
  local _, value = reaper.GetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_WORK_GUID_KEY)
  return value or ""
end

local function read_clean_container_guid()
  if not reaper.GetProjExtState then return "" end
  local _, value = reaper.GetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_CLEAN_GUID_KEY)
  return value or ""
end

local function find_top_level_track_by_guid(guid)
  if guid == "" then return nil end
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if reaper.GetTrackGUID(track) == guid and is_top_level_track(index) then
      return track, index
    end
  end
  return nil
end

local function is_work_container(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, CONTAINER_EXT_STATE_KEY, "", false)
  return value == WORK_CONTAINER_KIND
end

local function is_clean_container(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, CONTAINER_EXT_STATE_KEY, "", false)
  return value == CLEAN_CONTAINER_KIND
end

-- Resolve the initialize-owned container by its project GUID first, then its
-- durable track mark. Names are intentionally not identity: a hand-made
-- `[work]` folder must never become this action's workspace.
local function find_work_folder()
  local track, index = find_top_level_track_by_guid(read_work_container_guid())
  if track then return track, index end

  local found, found_index
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if is_top_level_track(index) and is_work_container(track) then
      if found then return nil, nil, "multiple [work] containers are marked in this project" end
      found, found_index = track, index
    end
  end
  return found, found_index
end

-- Mirrors initialize's durable resolution order.  Names are deliberately not
-- identity, so a hand-made `[clean]` track is never silently adopted here.
local function find_clean_folder()
  local track, index = find_top_level_track_by_guid(read_clean_container_guid())
  if track then return track, index end

  local found, found_index
  for index = 0, reaper.CountTracks(0) - 1 do
    local candidate = reaper.GetTrack(0, index)
    if is_top_level_track(index) and is_clean_container(candidate) then
      if found then return nil, nil, "multiple [clean] containers are marked in this project" end
      found, found_index = candidate, index
    end
  end
  return found, found_index
end

local function reference_name_from_container(prefix)
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if is_top_level_track(index) then
      local name = track_name(track)
      local expected_prefix = prefix .. " "
      if starts_with(name, expected_prefix) then
        local reference_name = name:sub(#expected_prefix + 1)
        if reference_name ~= "" then return reference_name end
      end
    end
  end
  return nil
end

local function work_container_name()
  -- This standalone action never reads the sidecar. Reuse a live scaffold
  -- name when available; initialize will adopt and rename a bare fallback.
  local reference_name = reference_name_from_container(VGT_PREFIX)
    or reference_name_from_container(CLEAN_PREFIX)
  if reference_name then return WORK_PREFIX .. " " .. reference_name end
  return WORK_PREFIX
end

local function clean_container_name()
  local reference_name = reference_name_from_container(VGT_PREFIX)
    or reference_name_from_container(WORK_PREFIX)
  if reference_name then return CLEAN_PREFIX .. " " .. reference_name end
  return CLEAN_PREFIX
end

local function vgt_root_index()
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if is_top_level_track(index) and starts_with(track_name(track), VGT_PREFIX) then
      return index
    end
  end
  return nil
end

local function selected_source_tracks()
  local sources = {}
  for index = 0, reaper.CountSelectedTracks(0) - 1 do
    sources[#sources + 1] = reaper.GetSelectedTrack(0, index)
  end
  return sources
end

-- Insert a user-owned copy of source_chunk at insert_index and re-stamp
-- everything that must differ for an editable, non-vgt copy. Returns the track.
local function build_working_copy(insert_index, source_chunk, source_name, folder_depth)
  reaper.InsertTrackAtIndex(insert_index, false)
  local track = reaper.GetTrack(0, insert_index)
  reaper.SetTrackStateChunk(track, replace_track_guid(source_chunk, reaper.genGuid("")), false)
  -- The chunk restored the source's name, mute, ownership mark, folder nesting,
  -- and per-item locks. Override each so the copy is user-owned and editable.
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", working_name(source_name), true)
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", 0)
  reaper.SetMediaTrackInfo_Value(track, "I_FOLDERDEPTH", folder_depth)
  reaper.GetSetMediaTrackInfo_String(track, VGT_EXT_STATE_KEY, "", true)
  reaper.GetSetMediaTrackInfo_String(track, WORK_EXT_STATE_KEY, WORK_EXT_STATE_VALUE, true)
  reaper.SetTrackSelected(track, true)
  -- A vgt reference item is left unlocked already, but a copied label/beat item
  -- can be locked; unlock every item so the whole copy is immediately editable.
  for item_index = 0, reaper.CountTrackMediaItems(track) - 1 do
    reaper.SetMediaItemInfo_Value(reaper.GetTrackMediaItem(track, item_index), "C_LOCK", 0)
  end
  return track
end

local function create()
  local sources = selected_source_tracks()
  if #sources == 0 then
    reaper.ShowMessageBox(
      "Select the track(s) you want a working copy of, then run this action again.",
      "vgt working copy", 0
    )
    return
  end

  -- Read every source chunk up front, before any insertion shifts track indices.
  local jobs = {}
  for _, source in ipairs(sources) do
    local ok, chunk = reaper.GetTrackStateChunk(source, "", false)
    if ok then jobs[#jobs + 1] = {chunk = chunk, name = track_name(source)} end
  end
  if #jobs == 0 then
    reaper.ShowMessageBox("Could not read the selected track(s).", "vgt working copy", 0)
    return
  end

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)

  local folder, folder_index, resolution_error = find_work_folder()
  if resolution_error then
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("vgt: create working copy", -1)
    reaper.ShowMessageBox(resolution_error .. ".", "vgt working copy", 0)
    return
  end
  local empty_container = folder and is_empty_work_container(folder)
  if folder and not empty_container and not is_complete_work_folder(folder_index, folder) then
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("vgt: create working copy", -1)
    reaper.ShowMessageBox(
      "The [work] container has a changed folder structure; leaving it untouched.",
      "vgt working copy", 0
    )
    return
  end
  -- Clear the selection only after every refusal path that must be atomic.
  -- The freshly created copies become the new selection below.
  for _, source in ipairs(sources) do reaper.SetTrackSelected(source, false) end

  local insert_index
  if folder_index and empty_container then
    -- An initialize-created empty scaffold becomes a folder only as its first
    -- copy is inserted, so no empty intermediate folder state can persist.
    reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)
    insert_index = folder_index + 1
  elseif folder_index then
    -- A populated, structurally intact folder (guaranteed by the
    -- is_complete_work_folder check above): reopen its current closing child
    -- (I_FOLDERDEPTH -1 -> 0) so new copies can be appended after it. This
    -- only touches the folder-depth flag, never the child's name, items, FX,
    -- or any other content, so it stays within this action's ownership.
    local last_child_index = folder_last_child_index(folder_index)
    reaper.SetMediaTrackInfo_Value(reaper.GetTrack(0, last_child_index), "I_FOLDERDEPTH", 0)
    insert_index = last_child_index + 1
  else
    -- Create the same scaffold initialize would: directly above its root when
    -- present, otherwise at the end. It becomes a folder immediately below.
    insert_index = vgt_root_index() or reaper.CountTracks(0)
    reaper.InsertTrackAtIndex(insert_index, false)
    local created = reaper.GetTrack(0, insert_index)
    reaper.GetSetMediaTrackInfo_String(created, "P_NAME", work_container_name(), true)
    reaper.SetMediaTrackInfo_Value(created, "B_MUTE", 0)
    reaper.SetMediaTrackInfo_Value(created, "I_FOLDERDEPTH", 1)
    reaper.GetSetMediaTrackInfo_String(created, CONTAINER_EXT_STATE_KEY, WORK_CONTAINER_KIND, true)
    reaper.SetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_WORK_GUID_KEY, reaper.GetTrackGUID(created))
    reaper.SetMediaTrackInfo_Value(
      created, "I_CUSTOMCOLOR",
      reaper.ColorToNative(WORK_COLOR[1], WORK_COLOR[2], WORK_COLOR[3]) | 0x1000000
    )
    insert_index = insert_index + 1
  end

  for position, job in ipairs(jobs) do
    -- The last copy closes the folder (-1); every earlier one stays flat (0).
    local folder_depth = position == #jobs and -1 or 0
    build_working_copy(insert_index, job.chunk, job.name, folder_depth)
    insert_index = insert_index + 1
  end

  reaper.MarkProjectDirty(0)
  reaper.PreventUIRefresh(-1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  reaper.Undo_EndBlock("vgt: create working copy", -1)
end

local function save_selected_tracks()
  local selected = {}
  for index = 0, reaper.CountSelectedTracks(0) - 1 do
    selected[#selected + 1] = reaper.GetSelectedTrack(0, index)
  end
  return selected
end

local function restore_selected_tracks(selected)
  for index = 0, reaper.CountTracks(0) - 1 do
    reaper.SetTrackSelected(reaper.GetTrack(0, index), false)
  end
  for _, track in ipairs(selected) do reaper.SetTrackSelected(track, true) end
end

local function is_promotable(track)
  return is_marked_work_object(track) and starts_with(track_name(track), WORK_PREFIX)
end

local function create_clean_folder(work_index)
  -- A new clean scaffold belongs immediately above work.  In the unusual case
  -- that this action is run before work exists, retain create's safe fallback.
  local insert_index = work_index or vgt_root_index() or reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(insert_index, false)
  local track = reaper.GetTrack(0, insert_index)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", clean_container_name(), true)
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", 0)
  reaper.GetSetMediaTrackInfo_String(track, CONTAINER_EXT_STATE_KEY, CLEAN_CONTAINER_KIND, true)
  reaper.SetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_CLEAN_GUID_KEY, reaper.GetTrackGUID(track))
  reaper.SetMediaTrackInfo_Value(
    track, "I_CUSTOMCOLOR",
    reaper.ColorToNative(CLEAN_COLOR[1], CLEAN_COLOR[2], CLEAN_COLOR[3]) | 0x1000000
  )
  return track, insert_index
end

local function promote()
  local selected = save_selected_tracks()
  local eligible, rejected = {}, 0
  for _, track in ipairs(selected) do
    if is_promotable(track) then eligible[#eligible + 1] = track else rejected = rejected + 1 end
  end
  if #eligible == 0 then
    reaper.ShowMessageBox(
      "Select [work] tracks created by vgt to promote them to [clean].",
      "vgt working copy", 0
    )
    return
  end

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)

  local work, work_index, work_error = find_work_folder()
  if work_error or not work or not is_complete_work_folder(work_index, work) then
    reaper.ShowMessageBox(
      work_error or "The [work] container has a changed folder structure; leaving it untouched.",
      "vgt working copy", 0
    )
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("vgt: promote working copies", -1)
    return
  end

  local clean, clean_index, clean_error = find_clean_folder()
  if clean_error or (clean and not is_empty_work_container(clean) and not is_complete_work_folder(clean_index, clean)) then
    reaper.ShowMessageBox(
      clean_error or "The [clean] container has a changed folder structure; leaving it untouched.",
      "vgt working copy", 0
    )
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("vgt: promote working copies", -1)
    return
  end
  -- Preserve the old work children by identity before the move.  If its
  -- closing child is promoted, folder_last_child_index cannot be used until a
  -- replacement closer has been installed.
  local work_last = folder_last_child_index(work_index)
  local work_children = {}
  for index = work_index + 1, work_last do
    work_children[reaper.GetTrack(0, index)] = true
  end

  if not clean then
    clean, clean_index = create_clean_folder(work_index)
    -- Inserting clean above work shifts the latter's numeric index.
    work, work_index = find_work_folder()
  end

  local clean_last
  if is_empty_work_container(clean) then
    reaper.SetMediaTrackInfo_Value(clean, "I_FOLDERDEPTH", 1)
    clean_last = clean_index
  else
    -- A populated, structurally intact [clean] folder (guaranteed by the
    -- guard above): reopen its current closing child (I_FOLDERDEPTH -1 -> 0)
    -- so the promoted tracks land after the existing ones, mirroring
    -- create()'s append-into-a-populated-container behavior. Only that
    -- folder-depth flag is touched -- the existing child's name, items, FX,
    -- and other content are left alone.
    clean_last = folder_last_child_index(clean_index)
    reaper.SetMediaTrackInfo_Value(reaper.GetTrack(0, clean_last), "I_FOLDERDEPTH", 0)
  end

  -- ReorderSelectedTracks moves the existing track objects: GUIDs, media,
  -- takes, FX, and routing all remain attached to their original track.
  for index = 0, reaper.CountTracks(0) - 1 do reaper.SetTrackSelected(reaper.GetTrack(0, index), false) end
  for _, track in ipairs(eligible) do reaper.SetTrackSelected(track, true) end
  reaper.ReorderSelectedTracks(clean_last + 1, 0)

  local promoted = {}
  for position, track in ipairs(eligible) do
    promoted[track] = true
    reaper.GetSetMediaTrackInfo_String(track, "P_NAME", clean_name(track_name(track)), true)
    reaper.GetSetMediaTrackInfo_String(track, WORK_EXT_STATE_KEY, "", true)
    reaper.GetSetMediaTrackInfo_String(track, VGT_EXT_STATE_KEY, "", true)
    reaper.GetSetMediaTrackInfo_String(track, CONTAINER_EXT_STATE_KEY, "", true)
    reaper.SetMediaTrackInfo_Value(track, "I_FOLDERDEPTH", position == #eligible and -1 or 0)
  end

  local remaining = {}
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if work_children[track] and not promoted[track] then remaining[#remaining + 1] = track end
  end
  if #remaining == 0 then
    reaper.SetMediaTrackInfo_Value(work, "I_FOLDERDEPTH", 0)
  else
    reaper.SetMediaTrackInfo_Value(remaining[#remaining], "I_FOLDERDEPTH", -1)
  end

  restore_selected_tracks(selected)
  reaper.MarkProjectDirty(0)
  reaper.PreventUIRefresh(-1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  reaper.Undo_EndBlock("vgt: promote working copies", -1)
  if rejected > 0 then
    reaper.ShowMessageBox(
      "Some selected tracks were not vgt-created [work] tracks and were not promoted.",
      "vgt working copy", 0
    )
  end
end

-- Automation (and the headless tests) can preselect the branch through the
-- "vgt"/"working_copy_action" ExtState; interactive users get a popup menu.
local function choose_action()
  local forced = reaper.GetExtState("vgt", "working_copy_action")
  if forced ~= "" then return forced end
  gfx.init("vgt working copy", 0, 0)
  gfx.x, gfx.y = gfx.mouse_x, gfx.mouse_y
  local choice = gfx.showmenu("Create working copy from selected tracks|Promote selected [work] tracks to [clean]")
  gfx.quit()
  if choice == 1 then return "create" end
  if choice == 2 then return "promote" end
  return nil
end

local function main()
  local path = select(2, reaper.EnumProjects(-1, ""))
  if path == "" then error("Save the REAPER project before creating a working copy.") end
  local action = choose_action()
  if action == "create" then
    create()
  elseif action == "promote" then
    promote()
  end
end

local ok, error_message = xpcall(main, debug.traceback)
if not ok then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("vgt working copy failed:\n" .. error_message, "vgt", 0)
end
