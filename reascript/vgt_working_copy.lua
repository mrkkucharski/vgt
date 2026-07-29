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
-- Finishing an edit is a manual drag of the `[work]` track wherever you want it;
-- rename it away from the `[work]` prefix to keep it past a discard.

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
local PROJ_EXT_WORK_GUID_KEY = "work_container"
local WORK_COLOR = {68, 175, 239}

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

local function is_discardable_work_object(track)
  return is_marked_work_object(track) and starts_with(track_name(track), WORK_PREFIX)
end

-- A name outside the scratch namespace is the user's durable reclaim signal.
-- Forget our private provenance as soon as a later invocation observes that
-- signal, so even a subsequent rename back to `[work] ...` can never make the
-- track eligible for automated discard again.  This is intentionally the only
-- metadata vgt changes on a reclaimed copy; its media, routing and placement
-- are left entirely alone.
local function forget_reclaimed_work_objects()
  local reclaimed = 0
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if is_marked_work_object(track) and not starts_with(track_name(track), WORK_PREFIX) then
      reaper.GetSetMediaTrackInfo_String(track, WORK_EXT_STATE_KEY, "", true)
      reclaimed = reclaimed + 1
    end
  end
  return reclaimed
end

local function is_top_level_track(index)
  local depth = 0
  for previous = 0, index - 1 do
    depth = depth + reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, previous), "I_FOLDERDEPTH")
  end
  return depth == 0
end

-- Never extend or remove a marked container that now contains a user-owned
-- object (including a marked copy reclaimed by renaming). Doing so could change
-- that user's folder depth. It remains visible but is no longer this action's
-- reusable/discardable scratch area.
local function workspace_has_only_discardable_children(folder_index)
  local last_child = folder_last_child_index(folder_index)
  for index = folder_index + 1, last_child do
    if not is_discardable_work_object(reaper.GetTrack(0, index)) then return false end
  end
  return true
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
  return workspace_has_only_discardable_children(folder_index)
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

  -- Clear the selection so only the new copies end up selected.
  for _, source in ipairs(sources) do reaper.SetTrackSelected(source, false) end

  -- Persist a user's prior rename before looking for a reusable workspace.
  -- This makes reclamation permanent rather than depending on its current name.
  forget_reclaimed_work_objects()

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

  local insert_index
  if folder_index and empty_container then
    -- An initialize-created empty scaffold becomes a folder only as its first
    -- copy is inserted, so no empty intermediate folder state can persist.
    reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)
    insert_index = folder_index + 1
  elseif folder_index then
    -- Extend the existing folder: its current last child closes it (depth -1),
    -- so demote it to a plain child and let the last new copy become the closer.
    local last_child = folder_last_child_index(folder_index)
    reaper.SetMediaTrackInfo_Value(reaper.GetTrack(0, last_child), "I_FOLDERDEPTH", 0)
    insert_index = last_child + 1
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

local function discard()
  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)
  local removed = 0
  local reclaimed = forget_reclaimed_work_objects()
  local removable_tracks = {}
  -- Decide the whole workspace before deleting any child.  A folder's closing
  -- child carries its balancing -1 depth; deleting that child while preserving
  -- an unmarked user track in the same folder would accidentally pull later
  -- tracks into the folder.  Therefore a mixed workspace is preserved as a
  -- unit. We only remove copies from a complete, resolved workspace whose
  -- children are all still in the scratch namespace.
  local folder, folder_index = find_work_folder()
  if folder and is_complete_work_folder(folder_index, folder) then
    local last_child = folder_last_child_index(folder_index)
    for child_index = folder_index + 1, last_child do
      removable_tracks[reaper.GetTrack(0, child_index)] = true
    end
  end
  -- Delete only objects belonging to one of those complete workspaces.  This
  -- additionally prevents a user-moved marked track, or a mixed workspace,
  -- from being guessed at just because it retains a `[work]` name.
  for index = reaper.CountTracks(0) - 1, 0, -1 do
    local track = reaper.GetTrack(0, index)
    if removable_tracks[track] and is_discardable_work_object(track) then
      reaper.DeleteTrack(track)
      removed = removed + 1
    end
  end
  -- The initialize-owned container remains as an empty, flat scaffold after
  -- discarding its copies. It is not a working-copy object itself.
  if removed > 0 and folder then
    reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 0)
  end
  reaper.PreventUIRefresh(-1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  reaper.Undo_EndBlock("vgt: discard working copies", -1)
  if removed > 0 or reclaimed > 0 then
    reaper.MarkProjectDirty(0)
  else
    reaper.ShowMessageBox(
      "No [work] tracks to discard. Rename a copy so it no longer starts with [work] to keep it.",
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
  local choice = gfx.showmenu("Create working copy from selected tracks|Discard all [work] copies")
  gfx.quit()
  if choice == 1 then return "create" end
  if choice == 2 then return "discard" end
  return nil
end

local function main()
  local path = select(2, reaper.EnumProjects(-1, ""))
  if path == "" then error("Save the REAPER project before creating a working copy.") end
  local action = choose_action()
  if action == "create" then
    create()
  elseif action == "discard" then
    discard()
  end
end

local ok, error_message = xpcall(main, debug.traceback)
if not ok then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("vgt working copy failed:\n" .. error_message, "vgt", 0)
end
