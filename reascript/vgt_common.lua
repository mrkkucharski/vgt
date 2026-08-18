-- Shared, feature-neutral ReaScript helpers with no reconciliation body of
-- their own. Loaded via dofile by vgt_create_working_copy.lua,
-- vgt_promote_working_copy.lua, vgt_transcribe_track.lua, and
-- vgt_get_transcription.lua (docs/on-demand-track-transcription-plan.md).
--
-- Never dofile vgt_initialize.lua for any of this: it is one large script
-- whose entire reconciliation body runs unconditionally, top to bottom,
-- whenever the file executes as a REAPER action -- Lua/EEL ReaScript has no
-- `if __name__ == "__main__"`-style gate. This module exists so a narrowly
-- scoped action can reach a handful of its helpers (JSON decoding, sidecar
-- reads, locked-track/MIDI-item creation) without also triggering a full
-- apply. Everything below is a *copy* of the corresponding vgt_initialize.lua
-- helper, not a move -- that file keeps its own local definitions for now
-- (see the plan's "Independence from vgt apply" for why folding it in too is
-- a separate, larger, more blast-radius-sensitive change).
--
-- ===== Working-copy actions (vgt_create_working_copy.lua / vgt_promote_working_copy.lua) =====
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

local function starts_with_vgt(track)
  local _, name = reaper.GetTrackName(track, "")
  return starts_with(name, VGT_PREFIX)
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

-- A track-state chunk is a complete statement of the source's identity, not
-- just its contents: its TRACKID, the IGUID/GUID of every item, take, envelope
-- and media source it holds, the item ids, and -- for MIDI -- the POOLEDEVTS
-- GUID naming the pool its notes belong to. Applied verbatim, REAPER resolves
-- the copy's MIDI take back onto the *same* pooled source as its origin, so an
-- edit in the copy silently rewrites whatever else shares that pool (the [vgt]
-- reference, or a sibling copy) -- and a later attempt to split them apart at
-- runtime can orphan the shared source and lose both sides' notes outright.
-- Detaching in the chunk, before REAPER ever sees it, is the only point at
-- which the copy can be made independent without touching the source at all.
--
-- next_guid must return a fresh GUID on every call; it is kept a parameter so
-- this stays a pure text transform.
local function detach_chunk_identity(chunk, next_guid)
  -- The track's own GUID can appear twice -- on the `<TRACK {..}` header and as
  -- TRACKID -- and the two must agree, so both take the *same* new GUID. Only
  -- the first of each is the track's identity; a later literal is content.
  local track_guid = next_guid()
  local detached = chunk:gsub(
    "^(%s*<TRACK) {[^}]*}", function(head) return head .. " " .. track_guid end, 1
  )
  detached = detached:gsub("TRACKID {[^}]*}", "TRACKID " .. track_guid, 1)
  -- `IGUID {..}` matches here too: the pattern starts at its trailing `GUID`,
  -- so the key survives and only the brace body is re-stamped.
  detached = detached:gsub("GUID {[^}]*}", function() return "GUID " .. next_guid() end)
  -- Drop the whole line for the two fields that must not be carried over at
  -- all: the pool the source's notes live in, and REAPER's per-item id (which
  -- it reassigns when absent). The leading newline goes with the line, so the
  -- preceding line keeps the one that terminates it.
  detached = detached:gsub("\n%s*POOLEDEVTS {[^}]*}", "")
  detached = detached:gsub("\n%s*IID %d+", "")
  return detached
end

-- A `MIDIPOOL` source stores no events of its own -- its notes live in
-- whichever other item owns the pool. Detaching such a chunk would hand the
-- copy an empty source, so the action refuses the operation rather than
-- produce a silently empty copy.
local function borrows_pooled_midi(chunk)
  return chunk:find("<SOURCE MIDIPOOL", 1, true) ~= nil
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
  local chunk = detach_chunk_identity(source_chunk, function() return reaper.genGuid("") end)
  reaper.SetTrackStateChunk(track, chunk, false)
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
  -- MIDI independence is already settled by detach_chunk_identity above; do not
  -- reintroduce a runtime unpool here, which is what destroyed both sides.
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
  -- Refuse the whole selection before anything is inserted: a copy that cannot
  -- be detached from its source's MIDI pool is not a working copy at all.
  for _, job in ipairs(jobs) do
    if borrows_pooled_midi(job.chunk) then
      reaper.ShowMessageBox(
        '"' .. job.name .. '" has pooled MIDI whose notes are stored in another item, '
          .. "so a copy of it would be empty. Un-pool it first (item properties -> uncheck "
          .. '"MIDI edits are pooled with other media items"), then run this action again.',
        "vgt working copy", 0
      )
      return
    end
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

  -- Promoting [work]'s own closing child while an unselected sibling remains
  -- reassigns that sibling's flag (0 -> -1) to keep [work] closed -- fine on
  -- its own. Appending into a populated [clean] similarly reopens *its*
  -- unselected closing child (-1 -> 0). Doing both at once in a single
  -- promote would edit two different foreign tracks' folder structure for
  -- one user action; refuse atomically instead of guessing which is safe.
  if clean and not is_empty_work_container(clean) then
    local eligible_by_track = {}
    for _, track in ipairs(eligible) do eligible_by_track[track] = true end
    local work_closer = reaper.GetTrack(0, work_last)
    local remaining_work_children = 0
    for index = work_index + 1, work_last do
      local child = reaper.GetTrack(0, index)
      if not eligible_by_track[child] then remaining_work_children = remaining_work_children + 1 end
    end
    if eligible_by_track[work_closer] and remaining_work_children > 0 then
      reaper.ShowMessageBox(
        "Promotion would need to alter both [work]'s and [clean]'s existing closing tracks at once, so nothing was changed.",
        "vgt working copy", 0
      )
      reaper.PreventUIRefresh(-1)
      reaper.Undo_EndBlock("vgt: promote working copies", -1)
      return
    end
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

-- ===== On-demand track transcription actions =====
-- (vgt_transcribe_track.lua / vgt_get_transcription.lua;
-- docs/on-demand-track-transcription-plan.md)
--
-- Everything below is a copy of the corresponding vgt_initialize.lua helper
-- (see that file's decode_json ~L341, read_analysis_block ~L302,
-- read_analysis ~L428, read_generation ~L436, add_locked_track ~L912,
-- set_take_ignores_project_tempo ~L1185), plus new JSON-encoding and
-- sidecar-commit helpers this feature needs that vgt_initialize.lua has no
-- equivalent of (it only ever splices one already-serialized `analysis`
-- object verbatim; nothing in it needs to *encode* JSON from Lua tables).

local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
end

local function project_dir()
  return project_path():match("^(.*[/\\])") or ""
end

local function sidecar_path()
  local path = project_path()
  return (path:gsub("%.[^./\\]*$", "") .. ".vgt")
end

local function read_sidecar_body()
  local file = io.open(sidecar_path(), "r")
  if not file then return nil end
  local body = file:read("*a")
  file:close()
  return body
end

-- Analysis is produced by Python, so use a small JSON reader here instead of
-- trying to scrape individual values with patterns. This intentionally only
-- decodes JSON; the RPP remains exclusively a REAPER API write.
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

-- The raw (still-serialized) text of the first `"key": { ... }` value found
-- in `body`, balanced across nested braces and string contents (a label may
-- itself contain `{`/`}`). Returns the substring from the opening `{` to its
-- matching `}`, or nil if `key` is absent or malformed. Generalizes
-- vgt_initialize.lua's read_analysis_block (originally hard-coded to the
-- `"analysis"` key) so the same balanced scan can also find `"track_jobs"`
-- nested inside an already-extracted analysis block.
local function find_json_object(body, key)
  if not body then return nil end
  local key_start = body:find('"' .. key .. '"%s*:%s*{')
  if not key_start then return nil end
  local brace_start = body:find("{", key_start)
  local depth = 0
  local in_string = false
  local escaped_char = false
  for index = brace_start, #body do
    local char = body:sub(index, index)
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
      if depth == 0 then return body:sub(brace_start, index) end
    end
  end
  return nil
end

local function read_analysis_block(body)
  return find_json_object(body or read_sidecar_body(), "analysis")
end

local function read_analysis(body)
  local block = read_analysis_block(body)
  return block and decode_json(block) or nil
end

-- Shared sidecar commit protocol (#138): the conflict signal every writer --
-- Python and both ReaScript actions -- bumps on every commit. See
-- sidecar.py's module docstring, schema 12.
local function read_generation(body)
  return tonumber((body or ""):match('"generation"%s*:%s*(%d+)')) or 0
end

-- Minimal JSON-string escaping for values this feature ever writes back
-- (track/job labels, error messages): not a general JSON writer, just enough
-- for the flat records below.
local function encode_json_string(value)
  local escaped = tostring(value):gsub('[\\"\n\r\t]', {
    ["\\"] = "\\\\", ['"'] = '\\"', ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t",
  })
  return '"' .. escaped .. '"'
end

-- A sentinel for an explicit JSON `null`, distinct from an absent key: Lua
-- cannot otherwise tell `t[k] = nil` (key never set) apart from a key whose
-- value is null, but sidecar.py's track_jobs shape has real fields (e.g.
-- `error`) that are meaningfully null on success. Pass this, never plain
-- `nil`, for a field a caller wants serialized as `null`.
local JSON_NULL = {}

-- Encode one Lua scalar (string/number/boolean/JSON_NULL) as a JSON value.
-- Not general-purpose: a track_jobs record is always a flat map of scalars
-- (see sidecar.py schema 19), so this deliberately does not recurse into
-- tables/arrays.
local function encode_json_scalar(value)
  if value == nil or value == JSON_NULL then return "null" end
  if type(value) == "boolean" then return value and "true" or "false" end
  if type(value) == "number" then return tostring(value) end
  return encode_json_string(value)
end

-- A record's field order: `__key_order`, when a caller supplied one (for a
-- freshly built record -- JSON object key order carries no meaning, but a
-- stable order keeps diffs/tests readable), otherwise every key currently on
-- the table (sorted), which is what a plain decode_json result has. Internal
-- `__`-prefixed bookkeeping keys are never themselves serialized.
--
-- Known, accepted limitation: decode_json cannot distinguish a JSON `null`
-- from an absent key (both become "key never set" in the decoded Lua table:
-- Lua has no way to store nil as a present value), so a pre-existing job
-- record's explicitly-null fields silently become absent fields the next
-- time a *different* job's commit re-encodes this whole object. Every reader
-- of `track_jobs[job_id]` treats "absent" and "null" identically (a plain
-- key lookup), so this is cosmetic, not a correctness problem.
local function record_key_order(record)
  if record.__key_order then return record.__key_order end
  local keys = {}
  for key in pairs(record) do
    if key:sub(1, 2) ~= "__" then keys[#keys + 1] = key end
  end
  table.sort(keys)
  return keys
end

-- Encode a flat `{key = scalar, ...}` record as a JSON object.
local function encode_flat_record(record)
  local pieces = {}
  for _, key in ipairs(record_key_order(record)) do
    pieces[#pieces + 1] = encode_json_string(key) .. ": " .. encode_json_scalar(record[key])
  end
  return "{" .. table.concat(pieces, ", ") .. "}"
end

-- Encode the whole `analysis.track_jobs` object (sidecar.py schema 19: a
-- flat map of job_id -> flat record) from a Lua table of
-- `{job_id = {record fields...}, ...}` plus the order to emit job ids in.
local function encode_track_jobs(jobs, job_order)
  local pieces = {}
  for _, job_id in ipairs(job_order) do
    pieces[#pieces + 1] = encode_json_string(job_id) .. ": " .. encode_flat_record(jobs[job_id])
  end
  return "{" .. table.concat(pieces, ", ") .. "}"
end

-- Replace `"key": { ... }`'s value in `container` with `new_object_text`, or
-- append `"key": new_object_text` just inside `container`'s own closing
-- brace if `key` is not yet present. `container` must itself be a raw JSON
-- object's text (e.g. what find_json_object/read_analysis_block returned).
local function splice_json_object(container, key, new_object_text)
  local existing = find_json_object(container, key)
  if existing then
    local key_start = container:find('"' .. key .. '"%s*:%s*{')
    local before = container:sub(1, key_start - 1)
    local after = container:sub(key_start + #(container:sub(key_start, container:find("{", key_start) - 1)) + #existing)
    return before .. '"' .. key .. '": ' .. new_object_text .. after
  end
  -- Insert just before the container's own final closing brace.
  local last_close = container:match(".*()}")
  if not last_close then error("splice_json_object: container is not a JSON object") end
  local before = container:sub(1, last_close - 1)
  local after = container:sub(last_close)
  local trimmed = before:gsub("%s+$", "")
  local needs_comma = trimmed:sub(-1) ~= "{"
  return trimmed .. (needs_comma and ", " or "") .. '"' .. key .. '": ' .. new_object_text .. "\n" .. after
end

local GENERATION_RETRY_LIMIT = 5

-- Commit one `analysis.track_jobs[job_id]` record using the shared sidecar
-- commit protocol (#138), mirroring vgt_initialize.lua's write_settings: this
-- ReaScript action cannot take Python's `fcntl` lock, so it re-reads the
-- sidecar as late as possible on every attempt, splices its own change onto
-- whatever is currently on disk (never touching any other field -- unlike
-- write_settings, which owns the whole document, this only ever rewrites the
-- `analysis.track_jobs` sub-object), and re-checks `generation` immediately
-- before the atomic rename. A mismatch there means another writer committed
-- in the gap; retry against that newer state rather than renaming a stale
-- merge over it. `record` is a flat table of scalars plus `__key_order`
-- (the field names to serialize, in order).
local function commit_track_job(job_id, record)
  for attempt = 1, GENERATION_RETRY_LIMIT do
    local prior_body = read_sidecar_body()
    if not prior_body then error("vgt: no .vgt sidecar found; run vgt_initialize.lua first") end
    local analysis_text = find_json_object(prior_body, "analysis")
    if not analysis_text then error("vgt: sidecar has no analysis block; run vgt analyze first") end
    local track_jobs_text = find_json_object(analysis_text, "track_jobs") or "{}"
    local jobs = decode_json(track_jobs_text) or {}
    local job_order = {}
    for existing_id in pairs(jobs) do job_order[#job_order + 1] = existing_id end
    table.sort(job_order)
    if not jobs[job_id] then job_order[#job_order + 1] = job_id end
    jobs[job_id] = record
    local new_track_jobs_text = encode_track_jobs(jobs, job_order)
    local new_analysis_text = splice_json_object(analysis_text, "track_jobs", new_track_jobs_text)
    local generation = read_generation(prior_body)
    local new_generation_body = splice_json_object(prior_body, "analysis", new_analysis_text)
    local replaced_body, replaced_count = new_generation_body:gsub('("generation"%s*:%s*)%d+', "%1" .. (generation + 1), 1)
    new_generation_body = replaced_body
    if replaced_count == 0 then
      -- No prior sidecar ever lacks this field in practice (vgt_initialize.lua
      -- has always written it since schema 12), but do not silently skip the
      -- bump if one somehow does.
      new_generation_body = new_generation_body:gsub(
        '("schema_version"%s*:%s*%d+,)', '%1\n  "generation": ' .. (generation + 1) .. ",", 1
      )
    end

    local temporary_path = sidecar_path() .. ".tmp"
    local file, error_message = io.open(temporary_path, "w")
    if not file then error(error_message) end
    file:write(new_generation_body)
    file:close()

    if read_generation(read_sidecar_body() or "") == generation then
      local renamed, rename_error = os.rename(temporary_path, sidecar_path())
      if not renamed then
        os.remove(temporary_path)
        error(rename_error)
      end
      return
    end
    os.remove(temporary_path)
  end
  error("vgt: the sidecar changed concurrently " .. GENERATION_RETRY_LIMIT .. " time(s) while importing a track job; run this action again.")
end

-- Reference MIDI is authored at the analyzed tempo, not the project's. There
-- is no ReaScript accessor for a take's IGNTEMPO flag, so this rewrites the
-- item chunk directly (see vgt_initialize.lua's identical helper for the
-- IGNTEMPO chunk-format rationale).
local function set_take_ignores_project_tempo(item, midi_tempo)
  local ok, chunk = reaper.GetItemStateChunk(item, "", false)
  if not ok then return false end
  local tempo = tonumber(midi_tempo)
  if not tempo or tempo <= 0 then tempo = 120.0 end
  local new_chunk, count = chunk:gsub("IGNTEMPO 0 %S+ (%d+) (%d+)", function(num, den)
    return string.format("IGNTEMPO 1 %.6f %s %s", tempo, num, den)
  end, 1)
  if count ~= 1 then return false end
  return reaper.SetItemStateChunk(item, new_chunk, false)
end

-- A track-job result track (named after its source verbatim, e.g. a
-- `[work] Guitar` source produces `[work] Guitar (MT3)` -- see
-- track_job_name) that is deliberately *not* vgt_initialize.lua-managed:
-- unlike add_locked_track's original (P_EXT:vgt_managed + a role tag geared
-- at vgt_initialize.lua's own reconciliation, which recreates every managed
-- track it recognizes from the sidecar on every apply), a track-job result
-- has no persisted recipe vgt_initialize.lua knows how to rebuild -- it is a
-- one-off, imported once by vgt_get_transcription.lua, never reproduced by
-- `vgt apply`. Marking it vgt_managed would make remove_previous_managed_
-- tracks delete it on the very next apply, with nothing to recreate it: a
-- silent, unrecoverable loss of the user's transcription result, exactly
-- what the non-destructive invariant exists to prevent. So this uses its own
-- distinct extended-state key instead, recording which job created it
-- (diagnostic only) without opting into vgt_initialize.lua's reconciliation
-- at all -- the same reasoning that already keeps `[work]`/`[clean]` copies
-- unmarked, now naturally consistent since this track's name inherits
-- whatever namespace prefix its source already had, `[vgt]` included.
local TRACK_JOB_EXT_STATE_KEY = "P_EXT:vgt_track_job"

local function add_track_job_track(index, name, job_id)
  reaper.InsertTrackAtIndex(index, true)
  local track = reaper.GetTrack(0, index)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", 0)
  reaper.GetSetMediaTrackInfo_String(track, TRACK_JOB_EXT_STATE_KEY, job_id, true)
  return track
end

local function is_track_job_track(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, TRACK_JOB_EXT_STATE_KEY, "", false)
  return value ~= ""
end

-- Quote one argument for POSIX shell interpolation (macOS-only, per this
-- project's environment assumption): wrap in single quotes, escaping any
-- single quote in the value as `'\''` (close the quote, emit an escaped
-- quote, reopen). Every path interpolated into a spawned command line must
-- go through this -- project paths routinely contain spaces (see this repo's
-- own `test/Reaper Project/` fixture).
local function shell_quote(value)
  return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

-- ===== Track-job status/import (vgt_get_transcription.lua and
-- vgt_transcribe_track.lua's own bounded defer loop share this: exactly one
-- importer, per docs/on-demand-track-transcription-plan.md's #9) =====

-- A detached job's process outlives the `vgt_transcribe_track.lua` run that
-- spawned it, so nothing before this pinned commit's `--force-program`
-- inference count is possible here -- generous margin over
-- MT3_TIMEOUT_SECONDS = 600 (transcribe.py) for the subprocess itself, plus
-- room for provisioning checks and I/O around it.
local STALE_JOB_SECONDS = 900

-- A genuinely launched job writes `status: "running"` as the very first
-- thing it does (run_track_job's first statement, before any provisioning
-- check or heavy work) -- well under a second on any real machine. A job
-- that still has no `started_at` at all after this much time did not fail
-- partway through; the spawn itself never launched a working process (a bad
-- interpreter path, an argument-parsing error, a Python import failure --
-- see vgt_transcribe_track.lua's `as_integer_program`, a real prior
-- instance of exactly this). That is worth surfacing in seconds, not by
-- waiting out the full STALE_JOB_SECONDS budget meant for a job that is
-- actually running long MT3 inference.
local NEVER_STARTED_SECONDS = 30

local function track_jobs_dir(namespace)
  return project_dir() .. "vgt/" .. namespace .. "/track-jobs"
end

local function list_job_ids(namespace)
  local ids = {}
  local index = 0
  while true do
    local name = reaper.EnumerateSubdirectories(track_jobs_dir(namespace), index)
    if not name then break end
    ids[#ids + 1] = name
    index = index + 1
  end
  return ids
end

local function read_job_status(namespace, job_id)
  local path = track_jobs_dir(namespace) .. "/" .. job_id .. "/status.json"
  local file = io.open(path, "r")
  if not file then return nil end
  local body = file:read("*a")
  file:close()
  local ok, decoded = pcall(decode_json, body)
  if not ok then return nil end
  return decoded
end

local function already_recorded(job_id)
  local analysis = read_analysis()
  local record = analysis and analysis.track_jobs and analysis.track_jobs[job_id]
  return record ~= nil
end

-- The result track's name mirrors the source track's name verbatim --
-- prefix and all -- with " (MT3)" appended, from the name captured at spawn
-- time (the source may no longer exist live by the time the job finishes --
-- see the plan's "user discards/deletes the source track" edge case).
-- Deliberately not re-prefixed with "[vgt]": the result sits next to
-- whatever the user was actually working on (e.g. a `[work] Guitar` source
-- produces `[work] Guitar (MT3)`, not a track that jumps into vgt's own
-- managed namespace) -- and it is safe regardless of what that prefix is,
-- since add_track_job_track deliberately never marks this track
-- vgt_managed (see that function's own comment).
local function track_job_name(source_track_name)
  local name = source_track_name
  if not name or name == "" then name = "Track" end
  return name .. " (MT3)"
end

local function find_track_by_guid(guid)
  if not guid or guid == "" then return nil end
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if reaper.GetTrackGUID(track) == guid then return track, index end
  end
  return nil
end

-- Import one finished job's MIDI result as a new "<source name> (MT3)"
-- track, positioned to match the source track's captured bounds and
-- authored at the analyzed project tempo (see the plan's "Tempo matching",
-- fully reused via set_take_ignores_project_tempo -- the same mechanism
-- every other `[vgt]` reference MIDI track already relies on).
local function import_finished_job(namespace, job_id, status)
  local job_dir = track_jobs_dir(namespace) .. "/" .. job_id
  local midi_path = job_dir .. "/result.mid"
  local source_track, source_index = find_track_by_guid(status.source_track_guid)
  local insert_index = source_track and (source_index + 1) or reaper.CountTracks(0)
  -- A negative I_FOLDERDEPTH means the source track is the *last* child
  -- closing its folder. Inserting the new track right after it (the normal
  -- case, immediately below) would land one level shallower, outside that
  -- folder, rather than nested alongside its source -- the exact "wrong
  -- indentation" this comment exists to prevent. Reopen the source (0) and
  -- let the newly inserted track become the new closer at the source's
  -- former depth instead, mirroring the identical reopen-then-append
  -- pattern vgt_common.lua's own create() already uses for working copies.
  local source_folder_depth = source_track and reaper.GetMediaTrackInfo_Value(source_track, "I_FOLDERDEPTH") or 0

  local pcm_source = reaper.PCM_Source_CreateFromFile(midi_path)
  if not pcm_source then error("REAPER could not open the transcribed MIDI: " .. midi_path) end

  local name = track_job_name(status.source_track_name)
  local track = add_track_job_track(insert_index, name, job_id)
  if source_track and source_folder_depth < 0 then
    reaper.SetMediaTrackInfo_Value(source_track, "I_FOLDERDEPTH", 0)
    reaper.SetMediaTrackInfo_Value(track, "I_FOLDERDEPTH", source_folder_depth)
  end
  local item = reaper.AddMediaItemToTrack(track)
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", tonumber(status.item_start_s) or 0)
  local length = (tonumber(status.item_end_s) or 0) - (tonumber(status.item_start_s) or 0)
  if length <= 0 then length = reaper.GetMediaSourceLength(pcm_source) end
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", length)
  reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 0)
  reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.SetMediaItemTake_Source(take, pcm_source)
  if not set_take_ignores_project_tempo(item, status.midi_tempo) then
    reaper.ShowConsoleMsg("vgt: track job " .. job_id .. ": could not make MIDI take ignore project tempo map\n")
  end

  commit_track_job(job_id, {
    status = "imported", source_track_name = status.source_track_name or "", requested_program = status.requested_program or 0,
    midi_tempo = status.midi_tempo or 120.0, midi_file = "track-jobs/" .. job_id .. "/result.mid",
    notes_file = "track-jobs/" .. job_id .. "/result.csv", note_count = status.note_count or 0,
    imported_at = os.date("!%Y-%m-%dT%H:%M:%SZ"), error = JSON_NULL,
    __key_order = {
      "status", "source_track_name", "requested_program", "midi_tempo", "midi_file", "notes_file",
      "note_count", "imported_at", "error",
    },
  })
  reaper.MarkProjectDirty(0)
  reaper.UpdateArrange()
end

-- Howard Hinnant's days-from-civil calendar algorithm (proleptic Gregorian,
-- no timezone or library dependence at all): the number of days between
-- 1970-01-01 and the given UTC calendar date.
local function days_from_civil(y, m, d)
  y = m <= 2 and y - 1 or y
  local era = math.floor((y >= 0 and y or y - 399) / 400)
  local yoe = y - era * 400
  local doy = math.floor((153 * (m > 2 and m - 3 or m + 9) + 2) / 5) + d - 1
  local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
end

-- Parse a UTC ISO-8601 timestamp (as `_now_iso`/status.json's `started_at`
-- always write it, e.g. "2026-08-17T10:23:45.123456Z") into a Unix epoch
-- second, comparable directly with the bare `os.time()` (always UTC-based,
-- unlike `os.time(table)` which is local-time-only).
--
-- This used to compute a local/UTC offset via os.date("*t")/os.date("!*t")
-- pushed back through os.time() and add it in -- the usual Lua trick for
-- this, but subtly wrong: os.time(table)'s `isdst` disambiguation forces a
-- standard- or daylight-time interpretation of the *literal wall-clock
-- fields* it's given, and a UTC-fields table (isdst always false from
-- os.date("!*t")) does not carry the local zone's actual current DST state,
-- so the computed "offset" silently missed daylight saving by exactly one
-- hour -- caught for real in this project's own dev environment (CEST,
-- UTC+2) while testing the stale-job watchdog below, not a hypothetical
-- edge case. Pure calendar arithmetic has no such ambiguity to get wrong.
local function parse_iso8601_utc(text)
  local year, month, day, hour, min, sec = (text or ""):match("(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
  if not year then return nil end
  local days = days_from_civil(tonumber(year), tonumber(month), tonumber(day))
  return days * 86400 + tonumber(hour) * 3600 + tonumber(min) * 60 + tonumber(sec)
end

-- Check job `job_id` (already known to exist) once: import it if freshly
-- done, report+record it once if it errored or has gone stale, or do
-- nothing if it is still genuinely running. Returns true once this job has
-- reached a settled, recorded state (so a poller can stop watching it).
local function check_and_import_job(job_id)
  local analysis = read_analysis()
  local namespace = analysis and analysis.stems and analysis.stems.artifact_namespace
  if not namespace then return false end
  if already_recorded(job_id) then return true end
  local status = read_job_status(namespace, job_id)
  if not status then return false end

  if status.status == "done" then
    local ok, err = pcall(import_finished_job, namespace, job_id, status)
    if not ok then
      reaper.ShowMessageBox("vgt: importing track job " .. job_id .. " failed:\n" .. tostring(err), "vgt", 0)
      return false -- leave unrecorded so a fixed retry can still import it
    end
    return true
  end
  if status.status == "error" then
    reaper.ShowMessageBox("vgt: track job " .. job_id .. " failed: " .. tostring(status.error), "vgt", 0)
    commit_track_job(job_id, {
      status = "error", source_track_name = status.source_track_name or "", requested_program = status.requested_program or 0,
      midi_tempo = status.midi_tempo, midi_file = JSON_NULL, notes_file = JSON_NULL, note_count = JSON_NULL,
      imported_at = os.date("!%Y-%m-%dT%H:%M:%SZ"), error = status.error or "unknown error",
      __key_order = {
        "status", "source_track_name", "requested_program", "midi_tempo", "midi_file", "notes_file",
        "note_count", "imported_at", "error",
      },
    })
    return true
  end
  -- Still "running" (or, if `started_at` is absent, the trigger script's own
  -- selection-only fields with no job-process status yet -- "never
  -- started", see the plan's failure modes: a spawn that failed outright
  -- before the job process could write anything of its own). Either way,
  -- age against whichever timestamp is on record -- the job's own
  -- `started_at` once it has one, otherwise the trigger script's
  -- `created_at` -- so a spawn that never started is caught by the same
  -- watchdog instead of polling forever.
  local reference_time = status.started_at or status.created_at
  local reference_epoch = reference_time and parse_iso8601_utc(reference_time)
  local threshold = status.started_at and STALE_JOB_SECONDS or NEVER_STARTED_SECONDS
  if reference_epoch and (os.time() - reference_epoch) > threshold then
    local job_dir = track_jobs_dir(namespace) .. "/" .. job_id
    if status.started_at then
      reaper.ShowConsoleMsg(string.format(
        "vgt: track job %s has shown no result for over %ds; it may have crashed partway "
          .. "through (no output ever reaches a detached process). Check %s/status.json, "
          .. "or just re-run the transcription.\n",
        job_id, threshold, job_dir
      ))
    else
      reaper.ShowConsoleMsg(string.format(
        "vgt: track job %s never started -- the spawn itself failed (bad interpreter path, "
          .. "an argument error, or similar; a running job always writes status \"running\" "
          .. "within a second). Check %s/spawn.log, or just re-run the transcription.\n",
        job_id, job_dir
      ))
    end
    return true -- stop polling this one; not recorded in the sidecar -- it never reached a terminal state
  end
  return false
end

local function run(action)
  local path = select(2, reaper.EnumProjects(-1, ""))
  if path == "" then error("Save the REAPER project before creating a working copy.") end
  if action == "create" then
    create()
  elseif action == "promote" then
    promote()
  end
end

return {
  create = create, promote = promote, run = run,
  track_name = track_name, starts_with = starts_with, starts_with_vgt = starts_with_vgt,
  project_path = project_path, project_dir = project_dir, sidecar_path = sidecar_path,
  read_sidecar_body = read_sidecar_body, decode_json = decode_json,
  find_json_object = find_json_object, read_analysis_block = read_analysis_block,
  read_analysis = read_analysis, read_generation = read_generation,
  encode_json_scalar = encode_json_scalar, encode_flat_record = encode_flat_record,
  encode_track_jobs = encode_track_jobs, splice_json_object = splice_json_object,
  commit_track_job = commit_track_job, set_take_ignores_project_tempo = set_take_ignores_project_tempo,
  JSON_NULL = JSON_NULL,
  add_track_job_track = add_track_job_track, is_track_job_track = is_track_job_track,
  shell_quote = shell_quote,
  track_jobs_dir = track_jobs_dir, list_job_ids = list_job_ids, read_job_status = read_job_status,
  already_recorded = already_recorded, check_and_import_job = check_and_import_job,
  STALE_JOB_SECONDS = STALE_JOB_SECONDS, NEVER_STARTED_SECONDS = NEVER_STARTED_SECONDS,
}
