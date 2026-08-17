-- Consolidate two selected tracks' MIDI notes onto one target track.
-- Select exactly 2 tracks and run this action; it asks which one is the
-- TARGET. Every MIDI note from the other (source) track moves onto the
-- target, and the target's MIDI item is rebuilt to span the full time range
-- of both tracks' original items, so every note keeps its absolute project
-- time. This is a general-purpose editing action: it has no vgt ownership
-- rules and works on any two selected tracks.
--
-- Notes are captured as absolute project time (not raw PPQ) before anything
-- is resized or deleted, so the result is correct regardless of whether the
-- target's item needs to extend earlier, later, or both, and regardless of
-- any tempo changes between the two tracks' items. Only MIDI notes move;
-- CC/text/sysex events and non-MIDI (audio) items are left untouched on
-- their original track. The source track itself is not deleted or renamed
-- -- it is simply left with no MIDI items once its notes have moved.

local function track_name(track)
  local _, name = reaper.GetTrackName(track, "")
  return name
end

-- Every MIDI item (item+take pair) on a track, in position order. Non-MIDI
-- items are skipped and left untouched on their track.
local function midi_items(track)
  local items = {}
  for index = 0, reaper.CountTrackMediaItems(track) - 1 do
    local item = reaper.GetTrackMediaItem(track, index)
    local take = reaper.GetActiveTake(item)
    if take and reaper.TakeIsMIDI(take) then
      items[#items + 1] = {item = item, take = take}
    end
  end
  table.sort(items, function(a, b)
    return reaper.GetMediaItemInfo_Value(a.item, "D_POSITION") < reaper.GetMediaItemInfo_Value(b.item, "D_POSITION")
  end)
  return items
end

-- Every note on a MIDI item, captured as absolute project time so it
-- survives the item being deleted and rebuilt elsewhere.
local function notes_at_project_time(entry)
  local notes = {}
  local _, note_count = reaper.MIDI_CountEvts(entry.take)
  for index = 0, note_count - 1 do
    local ok, selected, muted, start_ppq, end_ppq, chan, pitch, vel = reaper.MIDI_GetNote(entry.take, index)
    if ok then
      notes[#notes + 1] = {
        selected = selected, muted = muted,
        start_time = reaper.MIDI_GetProjTimeFromPPQPos(entry.take, start_ppq),
        end_time = reaper.MIDI_GetProjTimeFromPPQPos(entry.take, end_ppq),
        chan = chan, pitch = pitch, vel = vel,
      }
    end
  end
  return notes
end

local function item_bounds(entry)
  local start = reaper.GetMediaItemInfo_Value(entry.item, "D_POSITION")
  local length = reaper.GetMediaItemInfo_Value(entry.item, "D_LENGTH")
  return start, start + length
end

local function consolidate()
  if reaper.CountSelectedTracks(0) ~= 2 then
    reaper.ShowMessageBox("Select exactly 2 tracks, then run this action again.", "vgt: consolidate MIDI", 0)
    return
  end
  local track_a = reaper.GetSelectedTrack(0, 0)
  local track_b = reaper.GetSelectedTrack(0, 1)
  local name_a, name_b = track_name(track_a), track_name(track_b)

  local answer = reaper.ShowMessageBox(
    'Which track should be the consolidation TARGET (keeps all notes)?\n\n'
      .. 'Yes = "' .. name_a .. '"\nNo = "' .. name_b .. '"\nCancel = abort',
    "vgt: consolidate MIDI", 3
  )
  if answer ~= 6 and answer ~= 7 then return end
  local target, source = track_a, track_b
  if answer == 7 then target, source = track_b, track_a end

  local target_items = midi_items(target)
  local source_items = midi_items(source)
  if #source_items == 0 and #target_items == 0 then
    reaper.ShowMessageBox("Neither selected track has any MIDI items; nothing to consolidate.", "vgt: consolidate MIDI", 0)
    return
  end
  if #source_items == 0 then
    reaper.ShowMessageBox('"' .. track_name(source) .. '" has no MIDI items; nothing to move.', "vgt: consolidate MIDI", 0)
    return
  end

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)

  -- Capture every note as absolute project time, and the full span to cover,
  -- before anything is deleted or resized.
  local notes = {}
  local range_start, range_end
  local function absorb(entry)
    local item_start, item_end = item_bounds(entry)
    range_start = range_start and math.min(range_start, item_start) or item_start
    range_end = range_end and math.max(range_end, item_end) or item_end
    for _, note in ipairs(notes_at_project_time(entry)) do notes[#notes + 1] = note end
  end
  for _, entry in ipairs(target_items) do absorb(entry) end
  for _, entry in ipairs(source_items) do absorb(entry) end

  -- Preserve the target's own take name when it had exactly one MIDI item;
  -- anything more ambiguous (zero, or several) falls back to the default.
  local keep_take_name
  if #target_items == 1 then
    local _, take_name = reaper.GetSetMediaItemTakeInfo_String(target_items[1].take, "P_NAME", "", false)
    if take_name ~= "" then keep_take_name = take_name end
  end

  -- Remove every MIDI item this action is consolidating, from both tracks.
  for _, entry in ipairs(target_items) do reaper.DeleteTrackMediaItem(target, entry.item) end
  for _, entry in ipairs(source_items) do reaper.DeleteTrackMediaItem(source, entry.item) end

  -- Build one fresh MIDI item on the target spanning the full range, then
  -- reinsert every captured note converted to this item's own PPQ timeline.
  local new_item = reaper.CreateNewMIDIItemInProj(target, range_start, range_end, false)
  local new_take = reaper.GetActiveTake(new_item)
  if keep_take_name then reaper.GetSetMediaItemTakeInfo_String(new_take, "P_NAME", keep_take_name, true) end

  for _, note in ipairs(notes) do
    local start_ppq = reaper.MIDI_GetPPQPosFromProjTime(new_take, note.start_time)
    local end_ppq = reaper.MIDI_GetPPQPosFromProjTime(new_take, note.end_time)
    reaper.MIDI_InsertNote(new_take, note.selected, note.muted, start_ppq, end_ppq, note.chan, note.pitch, note.vel, true)
  end
  reaper.MIDI_Sort(new_take)

  reaper.SetTrackSelected(target, true)
  reaper.SetTrackSelected(source, false)

  reaper.MarkProjectDirty(0)
  reaper.PreventUIRefresh(-1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  reaper.Undo_EndBlock("vgt: consolidate MIDI tracks", -1)
  reaper.ShowConsoleMsg(string.format('vgt: consolidated %d note(s) onto "%s".\n', #notes, track_name(target)))
end

local ok, err = xpcall(consolidate, debug.traceback)
if not ok then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("vgt: consolidate MIDI failed:\n" .. err, "vgt", 0)
end
