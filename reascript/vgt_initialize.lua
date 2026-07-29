-- vgt Phase 0 apply action for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.
-- It is the only writer of REAPER projects: the Python CLI intentionally never edits RPP text.

local PREFIX = "[vgt]"
local CHORDS_NAME = PREFIX .. " Chords"
local BEATS_NAME = PREFIX .. " Beats"
local CLICK_NAME = PREFIX .. " Click"
local KEY_NAME = PREFIX .. " Key"
-- The canonical [clean]/[work]/[vgt] container layout (issue #225). [clean]
-- and [work] are the two user-content scaffold containers vgt creates and
-- repositions but never reaches inside of, per the non-destructive
-- invariant in docs/AGENTS.md.
local CLEAN_PREFIX = "[clean]"
local WORK_PREFIX = "[work]"
-- Colour is presentation, never provenance: every ownership decision in this
-- file reads P_EXT marks, GUIDs, and the manifest, never a track's colour.
-- Nothing below may branch on these values.
local CLEAN_COLOR = {187, 210, 41}
local WORK_COLOR = {68, 175, 239}
local VGT_COLOR = {189, 100, 175}
local STEM_TRACKS = {
  {artifact = "vocals", filename = "stems/vocals.wav", name = PREFIX .. " Vocals"},
  {artifact = "instrumental", filename = "stems/instrumental.wav", name = PREFIX .. " Instrumental"},
  {artifact = "bass", filename = "stems/bass.wav", name = PREFIX .. " Bass"},
  {artifact = "drums", filename = "stems/drums.wav", name = PREFIX .. " Drums"},
  {artifact = "guitar", filename = "stems/guitar.wav", name = PREFIX .. " Guitar"},
  {artifact = "backing", filename = "stems/backing-no-guitar.wav", name = PREFIX .. " Backing (no guitar)"},
  {artifact = "strings", filename = "stems/strings.wav", name = PREFIX .. " Strings"},
  {artifact = "piano", filename = "stems/piano.wav", name = PREFIX .. " Keys / Piano"},
}
-- This order and these labels match transcribe.py's VALID_TARGETS and the
-- corresponding stem labels above. It also gives orphaned transcriptions a
-- stable place after the stem block.
local TRANSCRIPTION_TARGETS = {
  {target = "guitar", label = "Guitar"},
  {target = "bass", label = "Bass"},
  {target = "vocals", label = "Vocals"},
  {target = "drums", label = "Drums"},
  {target = "instrumental", label = "Instrumental"},
  {target = "backing", label = "Backing (no guitar)"},
  {target = "strings", label = "Strings"},
  {target = "piano", label = "Keys / Piano"},
  {target = "original", label = "Original"},
}
local STEM_LEASE_TIMEOUT_SECONDS = 30 * 60

local function project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path
end

local function project_dir()
  return project_path():match("^(.*[/\\])") or ""
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

local function starts_with(name, prefix)
  return name:sub(1, #prefix) == prefix
end

local function starts_with_vgt(track)
  return starts_with(track_name(track), PREFIX)
end

-- Ownership must survive a stale or missing sidecar: `managed_track_guids` is
-- regenerated wholesale on every apply and written last, so any error, crash,
-- restored backup, or copied project folder between building the [vgt] block
-- and reaching write_settings leaves it recording nothing about a block that
-- fully exists. A per-track extended-state mark (REAPER's own "P_EXT:" key,
-- persisted in the RPP itself) travels with the track regardless of what the
-- sidecar says, so remove_previous_managed_tracks below never mistakes a
-- stale record for "nothing to replace" and appends a second folder.
local EXT_STATE_KEY = "P_EXT:vgt_managed"
local ROLE_EXT_STATE_KEY = "P_EXT:vgt_role"
local PROJ_EXT_SECTION = "vgt"
local PROJ_EXT_ROOT_MANIFEST_KEY = "managed_root_manifest"

local function mark_track_managed(track, role)
  reaper.GetSetMediaTrackInfo_String(track, EXT_STATE_KEY, "1", true)
  if role then reaper.GetSetMediaTrackInfo_String(track, ROLE_EXT_STATE_KEY, role, true) end
end

local function track_is_marked_managed(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, EXT_STATE_KEY, "", false)
  return value == "1"
end

local function track_role(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, ROLE_EXT_STATE_KEY, "", false)
  return value or ""
end

local function read_root_manifest()
  local _, value = reaper.GetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_ROOT_MANIFEST_KEY)
  return value or ""
end

local function write_root_manifest(root, managed_tracks)
  -- Project extended state is saved in the RPP, independently of the sidecar.
  -- The root GUID authenticates the folder; the roles make this record useful
  -- to people inspecting a recovered project as well as to later reconcilers.
  local entries = {"root=" .. reaper.GetTrackGUID(root)}
  for _, track in ipairs(managed_tracks) do
    local role = track_role(track)
    if role ~= "" then entries[#entries + 1] = reaper.GetTrackGUID(track) .. "=" .. role end
  end
  reaper.SetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_ROOT_MANIFEST_KEY, table.concat(entries, ";"))
end

local function manifest_root_guid()
  return read_root_manifest():match("root=({[%x%-]+})") or ""
end

local function manifest_roles()
  local roles = {}
  for guid, role in read_root_manifest():gmatch("({[%x%-]+})=([^;]+)") do
    roles[guid] = role
  end
  return roles
end

-- Regions have no per-object equivalent of a track's P_EXT mark, but they
-- need the same durability: `managed_region_ids` is regenerated wholesale and
-- persisted last, so the same crash/restored-backup/copied-folder window that
-- motivated mark_track_managed above would otherwise leave a fully-built
-- section block with no sidecar record at all. REAPER's project-scoped
-- ProjExtState is persisted in the RPP itself (independent of the sidecar),
-- so recording each region's ID here as soon as it is created -- rather than
-- waiting for write_settings -- means that record survives even if
-- write_settings itself never runs or fails partway through.
local PROJ_EXT_REGION_KEY = "managed_region_ids"

local function record_region_ids_ext_state(region_ids)
  local ids = {}
  for _, id in ipairs(region_ids) do ids[#ids + 1] = tostring(id) end
  reaper.SetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_REGION_KEY, table.concat(ids, ","))
end

local function read_region_ids_ext_state()
  local ids = {}
  local _, value = reaper.GetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_REGION_KEY)
  if not value or value == "" then return ids end
  for id in value:gmatch("%-?%d+") do ids[tonumber(id)] = true end
  return ids
end

-- How many of a track's items have an active take with a file-backed source.
-- Zero is a track with no real audio (no items, MIDI-only, or a REAPER-native
-- generator such as a count-in click track's <SOURCE CLICK>, which has no
-- file at all); more than one is ambiguous -- see has_file_backed_media.
local function file_backed_item_count(track)
  local count = 0
  for item_index = 0, reaper.CountTrackMediaItems(track) - 1 do
    local item = reaper.GetTrackMediaItem(track, item_index)
    local take = reaper.GetActiveTake(item)
    if take then
      local filename = reaper.GetMediaSourceFileName(reaper.GetMediaItemTake_Source(take), "")
      if filename ~= "" then count = count + 1 end
    end
  end
  return count
end

-- An unambiguous supported reference is exactly one file-backed mix item
-- (see project.track_source_path, which resolves the source path from that
-- same single item). A track with more than one file-backed item is rejected
-- rather than silently analyzing the first RPP FILE while positioning
-- objects over the span of every item on the track.
local function has_file_backed_media(track)
  return file_backed_item_count(track) == 1
end

-- Candidate reference tracks are the project's own (non-vgt) tracks that
-- actually have file-backed audio on them, in order.
local function candidate_tracks()
  local candidates = {}
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if not starts_with_vgt(track) and has_file_backed_media(track) then candidates[#candidates + 1] = track end
  end
  return candidates
end

-- Ask which track is the reference. Automation (e.g. the headless
-- verifier) can preselect it via the "vgt"/"reference_index" ExtState, a 0-based
-- index over candidate_tracks(). A lone candidate is used without prompting;
-- otherwise interactive users get a popup menu. Returns the chosen track, or
-- nil if the user dismissed the menu.
local function choose_reference(candidates)
  if #candidates == 0 then error("No non-[vgt] tracks with file-backed media to use as a reference.") end

  local forced = reaper.GetExtState("vgt", "reference_index")
  if forced ~= "" then
    local index = tonumber(forced)
    if not index or index < 0 or index >= #candidates then
      error("vgt reference_index ExtState is out of range: " .. forced)
    end
    return candidates[index + 1]
  end

  if #candidates == 1 then return candidates[1] end

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

-- Guitar type is a declaration, never an inference.  An automation caller
-- may set vgt/guitar_type to electric or acoustic; otherwise first use asks
-- once and persists the answer in the sidecar config.
local function persisted_guitar_type()
  local body = read_sidecar_body() or ""
  local value = body:match('"guitar_type"%s*:%s*"([^"]+)"')
  if value == "electric" or value == "acoustic" then return value end
  return nil
end

local function choose_guitar_type()
  local forced = reaper.GetExtState("vgt", "guitar_type")
  if forced ~= "" then
    if forced ~= "electric" and forced ~= "acoustic" then
      error("vgt guitar_type ExtState must be electric or acoustic: " .. forced)
    end
    return forced
  end
  local existing = persisted_guitar_type()
  if existing then return existing end
  gfx.init("vgt: choose the guitar type", 0, 0)
  gfx.x, gfx.y = gfx.mouse_x, gfx.mouse_y
  local choice = gfx.showmenu("Electric|Acoustic")
  gfx.quit()
  if choice == 1 then return "electric" end
  if choice == 2 then return "acoustic" end
  return nil
end

-- The persisted reference is authoritative once recorded: after first
-- initialization, apply must reuse this track rather than re-prompting or
-- silently substituting another candidate (see apply()'s resolve_reference).
local function persisted_reference_guid()
  local body = read_sidecar_body() or ""
  return body:match('"reference_track_guid"%s*:%s*"([^"]*)"') or ""
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
  -- earlier eligible apply.  We never blindly rewrite that map on re-apply --
  -- see prior_tempo_map_fingerprint / current_tempo_fingerprint below, which
  -- decide whether it is still safe to do so.
  return body:match('"tempo_map_applied"%s*:%s*true') ~= nil
end

local function prior_tempo_map_fingerprint()
  local body = read_sidecar_body() or ""
  return body:match('"tempo_map_fingerprint"%s*:%s*"(.-)"') or ""
end

local function prior_tempo_data_fingerprint()
  local body = read_sidecar_body() or ""
  return body:match('"tempo_data_fingerprint"%s*:%s*"(.-)"') or ""
end

-- A canonical snapshot of the *analyzed* tempo data itself (bpm, downbeat
-- offset, time signature, piecewise spans) -- independent of where the
-- reference track happens to sit in the timeline. Comparing this against the
-- fingerprint recorded for the live map is how a re-apply tells "the
-- detected/corrected tempo actually changed" apart from "nothing changed,
-- don't needlessly rewrite (and thereby re-shift any beat-attached
-- reference items) an already-current map".
local function tempo_data_fingerprint(tempo)
  local parts = {
    string.format("%.6f:%s:%s:%.6f", tonumber(tempo.bpm) or 0, tostring(tempo.time_signature or ""), tostring(tempo.downbeat_detected == true), tonumber(tempo.downbeat_offset_seconds) or 0),
  }
  if type(tempo.spans) == "table" then
    for _, span in ipairs(tempo.spans) do
      parts[#parts + 1] = string.format("%.6f:%.6f:%.6f", tonumber(span.start_seconds) or 0, tonumber(span.end_seconds) or 0, tonumber(span.bpm) or 0)
    end
  end
  return table.concat(parts, ";")
end

-- The Python `vgt analyze` stage adds a top-level "analysis"
-- object to the sidecar. This action is the sole writer of the sidecar's
-- other fields, so it must round-trip that object verbatim on re-apply
-- rather than silently dropping any analysis a user has already run.
-- `body`, when given, is a snapshot the caller already read (see write_settings,
-- which must derive the analysis block and the generation counter it is
-- racing against from the exact same read rather than two separate ones).
local function read_analysis_block(body)
  body = body or read_sidecar_body()
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

local GENERATION_RETRY_LIMIT = 5

local function remove_previous_managed_tracks()
  local managed = read_managed_guids()
  -- The project root manifest is a third, independent ownership channel
  -- (ProjExtState, not the sidecar file or a per-track P_EXT read) -- it must
  -- feed removal too, not just validate_reconciliation_inventory's
  -- authentication check above. Otherwise a manifest-authenticated root whose
  -- sidecar and P_EXT mark are both stale would pass validation as safe to
  -- proceed, and then never actually get deleted here, leaving the old area
  -- in place while apply() built a second one beside it.
  for guid in pairs(manifest_roles()) do managed[guid] = true end
  -- Either the sidecar GUID, the durable per-track mark, or the manifest is
  -- evidence of ownership -- the mark is what keeps this correct when the
  -- sidecar record is stale (see mark_track_managed above). None is enough on
  -- its own: preserve any track whose current name is not vgt-owned, since
  -- the user may have renamed a stale-marked track to make it their own.
  for index = reaper.CountTracks(0) - 1, 0, -1 do
    local track = reaper.GetTrack(0, index)
    if (managed[reaper.GetTrackGUID(track)] or track_is_marked_managed(track)) and starts_with_vgt(track) then
      reaper.DeleteTrack(track)
    end
  end
end

local function table_count(values)
  local count = 0
  for _ in pairs(values) do count = count + 1 end
  return count
end

-- Build this before Undo_BeginBlock: a failed ownership lookup is never a
-- first-run signal.  In particular, a `[vgt]` folder is a collision that
-- blocks creation until a human deliberately recovers it; its name alone is
-- not permission to remove it.
local function reconciliation_inventory(analysis)
  local sidecar_guids = read_managed_guids()
  local manifest_guid = manifest_root_guid()
  local manifest_role_by_guid = manifest_roles()
  local live_guid_count, marked_count = 0, 0
  local roots, roles = {}, {"managed-root"}
  if type(analysis) == "table" then
    if analysis.tempo then roles[#roles + 1] = "beats/click" end
    if analysis.key then roles[#roles + 1] = "key" end
    if analysis.chords then roles[#roles + 1] = "chords" end
    local artifacts = analysis.stems and analysis.stems.artifacts
    if type(artifacts) == "table" then for name in pairs(artifacts) do roles[#roles + 1] = "stem:" .. name end end
    local targets = analysis.transcription and analysis.transcription.targets
    if type(targets) == "table" then
      for name, record in pairs(targets) do
        if type(record) == "table" and type(record.variant_order) == "table" then
          for _, variant_id in ipairs(record.variant_order) do roles[#roles + 1] = "variant:" .. name .. ":" .. tostring(variant_id) end
        else
          roles[#roles + 1] = "variant:" .. name .. ":legacy"
        end
      end
    end
  end
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    local guid = reaper.GetTrackGUID(track)
    if sidecar_guids[guid] then live_guid_count = live_guid_count + 1 end
    if track_is_marked_managed(track) then marked_count = marked_count + 1 end
    local manifest_match = manifest_guid ~= "" and manifest_guid == guid
    -- A root is not always a folder: apply() flattens it back to a plain
    -- track (I_FOLDERDEPTH 0) whenever nothing ends up nested under it, so
    -- FOLDERDEPTH > 0 alone misses a previously-flattened root entirely --
    -- including one the project manifest still names as root=<guid>. Skipping
    -- it here meant that root was never authenticated *or* rejected: apply()
    -- treated its presence as a first run and appended a second one beside it.
    if starts_with_vgt(track)
      and (reaper.GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH") > 0 or track_role(track) == "managed-root" or manifest_match)
    then
      roots[#roots + 1] = {
        track = track, guid = guid, name = track_name(track),
        sidecar_match = sidecar_guids[guid] == true,
        ext_state_match = track_is_marked_managed(track),
        manifest_match = manifest_match,
      }
    end
  end
  return {
    sidecar_guid_count = table_count(sidecar_guids), live_guid_count = live_guid_count,
    marked_count = marked_count, roots = roots, manifest_guid = manifest_guid,
    manifest_role_by_guid = manifest_role_by_guid,
    expected_roles = roles,
  }
end

local function inventory_diagnostic(inventory)
  local root_names = {}
  for _, root in ipairs(inventory.roots) do root_names[#root_names + 1] = root.name .. " (" .. root.guid .. ")" end
  return "project=" .. project_path() .. "; sidecar=" .. sidecar_path()
    .. "; sidecar GUIDs=" .. inventory.sidecar_guid_count
    .. "; live GUID matches=" .. inventory.live_guid_count
    .. "; P_EXT:vgt_managed matches=" .. inventory.marked_count
    .. "; root candidates=" .. #inventory.roots .. " [" .. table.concat(root_names, ", ") .. "]"
    .. "; expected roles=" .. table.concat(inventory.expected_roles, ",")
end

local function validate_reconciliation_inventory(analysis)
  local inventory = reconciliation_inventory(analysis)
  if #inventory.roots > 1 then
    error("ambiguous [vgt] managed-root candidates; no project mutation was made. " .. inventory_diagnostic(inventory)
      .. ". Keep the intended folder and use a deliberate recovery/reclaim action before applying.")
  end
  if #inventory.roots == 1 then
    local root = inventory.roots[1]
    -- Evidence elsewhere in the project cannot authenticate this particular
    -- same-named folder.  Treating it as sufficient was still capable of
    -- appending beside an unauthenticated user folder after deleting an
    -- unrelated old vgt track.
    if inventory.manifest_guid ~= "" and not root.manifest_match then
      error("managed-root manifest disagrees with the live [vgt] root; no project mutation was made. "
        .. inventory_diagnostic(inventory))
    end
    local authenticated = root.sidecar_match or root.ext_state_match or root.manifest_match
    if not authenticated then
      error("existing [vgt] root has no authenticated ownership evidence; no project mutation was made. "
        .. inventory_diagnostic(inventory)
        .. ". This folder may be user-owned. Restore its sidecar/manifest or deliberately reclaim it before applying.")
    end
    -- Roles are an additional consistency check once both representations are
    -- present. Missing roles remain a supported migration/unreadable-marker
    -- state; conflicting durable identities are never guessed through.
    for index = 0, reaper.CountTracks(0) - 1 do
      local track = reaper.GetTrack(0, index)
      local guid, manifest_role, live_role = reaper.GetTrackGUID(track), inventory.manifest_role_by_guid[reaper.GetTrackGUID(track)], track_role(track)
      if manifest_role and live_role ~= "" and manifest_role ~= live_role then
        error("managed track role disagrees with the project manifest; no project mutation was made. "
          .. inventory_diagnostic(inventory) .. "; guid=" .. guid .. "; manifest role=" .. manifest_role .. "; track role=" .. live_role)
      end
    end
  end
  return inventory
end

local function warn(message)
  reaper.ShowConsoleMsg("vgt: " .. message .. "\n")
end

local function find_track_by_guid(guid)
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if reaper.GetTrackGUID(track) == guid then return track end
  end
  return nil
end

local function track_index(track)
  local guid = reaper.GetTrackGUID(track)
  for index = 0, reaper.CountTracks(0) - 1 do
    if reaper.GetTrackGUID(reaper.GetTrack(0, index)) == guid then return index end
  end
  return nil
end

local function is_top_level_track(index)
  local depth = 0
  for previous = 0, index - 1 do
    depth = depth + reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, previous), "I_FOLDERDEPTH")
  end
  return depth == 0
end

-- ---------------------------------------------------------------------------
-- [clean]/[work] container scaffold (issue #225)
--
-- These two containers are user-content: vgt creates, renames, recolours, and
-- repositions the container track itself, but never reads or changes what a
-- user puts inside it (the non-destructive invariant in docs/AGENTS.md).
-- Durable identity is a per-track mark plus a project-scoped GUID, exactly
-- like the [vgt] root -- but deliberately *not* P_EXT:vgt_managed and *not*
-- entered into managed_root_manifest: remove_previous_managed_tracks and
-- reconciliation_inventory both union sidecar GUIDs, the per-track managed
-- mark, and the manifest roles, so keeping containers out of all three (on
-- top of the starts_with_vgt name guard, which already excludes "[clean]"/
-- "[work]" names) means they can never enter a removal or ambiguous-root set.
-- ---------------------------------------------------------------------------
local CONTAINER_EXT_STATE_KEY = "P_EXT:vgt_container"
local PROJ_EXT_CLEAN_GUID_KEY = "clean_container"
local PROJ_EXT_WORK_GUID_KEY = "work_container"

-- Adoption at step 3 below is a deliberate, narrow relaxation of "names are
-- never provenance" that applies only to these two containers, never to the
-- [vgt] root: vgt only ever moves, renames, and recolours the container
-- itself, so mispicking one is recoverable by hand rather than destructive.
-- Real projects (including the author's) already contain hand-made [clean]/
-- [work] folders; refusing to adopt them would silently produce duplicates.
local CONTAINER_DEFS = {
  {kind = "clean", prefix = CLEAN_PREFIX, ext_guid_key = PROJ_EXT_CLEAN_GUID_KEY, color = CLEAN_COLOR, legacy_bare = false},
  {kind = "work", prefix = WORK_PREFIX, ext_guid_key = PROJ_EXT_WORK_GUID_KEY, color = WORK_COLOR, legacy_bare = true},
}

local function track_container_kind(track)
  local _, value = reaper.GetSetMediaTrackInfo_String(track, CONTAINER_EXT_STATE_KEY, "", false)
  return value or ""
end

local function mark_track_container(track, kind)
  reaper.GetSetMediaTrackInfo_String(track, CONTAINER_EXT_STATE_KEY, kind, true)
end

local function read_container_guid(ext_guid_key)
  local _, value = reaper.GetProjExtState(0, PROJ_EXT_SECTION, ext_guid_key)
  return value or ""
end

local function write_container_guid(ext_guid_key, guid)
  reaper.SetProjExtState(0, PROJ_EXT_SECTION, ext_guid_key, guid)
end

local function find_top_level_track_by_guid(guid)
  if guid == "" then return nil end
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if reaper.GetTrackGUID(track) == guid and is_top_level_track(index) then return track end
  end
  return nil
end

local function top_level_tracks_with_container_mark(kind)
  local matches = {}
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if is_top_level_track(index) and track_container_kind(track) == kind then matches[#matches + 1] = track end
  end
  return matches
end

-- A bare legacy "[work]" (no trailing name) is only ever an adoption
-- candidate for the work container -- it is renamed to "[work] <reference
-- name>" like every other resolution path, by the unconditional rename below.
local function top_level_adoption_candidates(def)
  local matches = {}
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    if is_top_level_track(index) then
      local name = track_name(track)
      -- Step 3 is adoption of a *previously unmarked* hand-made container.
      -- Never overwrite the other kind's durable mark because its current
      -- name happens to be misleading: marks, not names, remain identity.
      if track_container_kind(track) == ""
        and (starts_with(name, def.prefix .. " ") or (def.legacy_bare and name == def.prefix))
      then
        matches[#matches + 1] = track
      end
    end
  end
  return matches
end

local function track_names_joined(tracks)
  local names = {}
  for _, track in ipairs(tracks) do names[#names + 1] = track_name(track) end
  return table.concat(names, ", ")
end

-- Resolves, adopts, or creates the container described by `def`, following
-- the four-step order documented in the issue: a recorded GUID, then a
-- durable per-track mark, then name-based adoption, then creation. Returns
-- nil (skipping maintenance for this kind this run) when steps 2 or 3 turn up
-- more than one candidate -- this is intentionally a soft warning, not the
-- hard error the [vgt]-root ambiguity check uses, because a mispicked
-- scaffold container is recoverable by hand.
local function maintain_container(def, reference_name)
  local full_name = def.prefix .. " " .. reference_name

  local track = find_top_level_track_by_guid(read_container_guid(def.ext_guid_key))
  if track then
    -- The project GUID is enough to recover this container after a copied or
    -- partially repaired project, but restore the companion per-track mark
    -- too so future applies retain both durable identity records.
    mark_track_container(track, def.kind)
    -- Re-derive and reset the name on every apply so a renamed reference
    -- track keeps all three folders in sync.
    reaper.GetSetMediaTrackInfo_String(track, "P_NAME", full_name, true)
    return track
  end

  local marked = top_level_tracks_with_container_mark(def.kind)
  if #marked > 1 then
    warn("multiple " .. def.kind .. "-marked containers found (" .. track_names_joined(marked)
      .. "); skipping " .. def.prefix .. " container maintenance this run.")
    return nil
  end
  if #marked == 1 then
    track = marked[1]
    write_container_guid(def.ext_guid_key, reaper.GetTrackGUID(track))
    reaper.GetSetMediaTrackInfo_String(track, "P_NAME", full_name, true)
    return track
  end

  local adoptable = top_level_adoption_candidates(def)
  if #adoptable > 1 then
    warn("multiple unmarked " .. def.prefix .. " candidates found (" .. track_names_joined(adoptable)
      .. "); skipping " .. def.prefix .. " container maintenance this run.")
    return nil
  end
  if #adoptable == 1 then
    track = adoptable[1]
    mark_track_container(track, def.kind)
    write_container_guid(def.ext_guid_key, reaper.GetTrackGUID(track))
    reaper.GetSetMediaTrackInfo_String(track, "P_NAME", full_name, true)
    -- Colour is left exactly as the user set it: this track already existed
    -- as a hand-made folder before vgt ever adopted it.
    return track
  end

  -- Nothing to resolve or adopt: create a fresh one at the end of the
  -- project. Left at the default I_FOLDERDEPTH 0 -- an empty container is a
  -- plain top-level track; it only becomes a folder once it gains its first
  -- child (a later, separate action -- promotion/working-copy placement --
  -- not this one).
  local index = reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(index, true)
  track = reaper.GetTrack(0, index)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", full_name, true)
  mark_track_container(track, def.kind)
  write_container_guid(def.ext_guid_key, reaper.GetTrackGUID(track))
  -- Colour is set once, at creation, and never again -- a user recolouring a
  -- container by hand must keep their choice on every later apply.
  reaper.SetMediaTrackInfo_Value(track, "I_CUSTOMCOLOR", reaper.ColorToNative(def.color[1], def.color[2], def.color[3]) | 0x1000000)
  return track
end

-- Duplicated from vgt_working_copy.lua's folder_last_child_index: these two
-- .lua files are standalone REAPER actions with no shared module. The region
-- ends on the child whose I_FOLDERDEPTH brings the running depth back to zero
-- (0 for an empty/flattened container, which returns folder_index itself).
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
  for _, track in ipairs(selected) do
    reaper.SetTrackSelected(track, true)
  end
end

-- Moves the container block (the container track plus every child, found by
-- walking I_FOLDERDEPTH accumulation) to the end of the project as a single
-- unit. The block is depth-balanced (parent +1 ... last child -1, sum zero),
-- so reordering it as one unit cannot disturb the folder depth of anything
-- around it. The user's track selection is saved and restored around the
-- move, since ReorderSelectedTracks works through the selection and would
-- otherwise leave it silently changed.
local function move_container_block_to_end(track)
  local index = track_index(track)
  if not index then return end
  local last_child = folder_last_child_index(index)
  -- Already last: skip the call entirely so re-apply is a true no-op rather
  -- than relying on REAPER's behavior for a zero-distance reorder.
  if last_child == reaper.CountTracks(0) - 1 then return end

  local saved_selection = save_selected_tracks()
  for i = 0, reaper.CountTracks(0) - 1 do
    reaper.SetTrackSelected(reaper.GetTrack(0, i), false)
  end
  for i = index, last_child do
    reaper.SetTrackSelected(reaper.GetTrack(0, i), true)
  end
  reaper.ReorderSelectedTracks(reaper.CountTracks(0), 0)
  restore_selected_tracks(saved_selection)
end

-- When the two complete blocks already form the final project tail in the
-- canonical order, neither move is needed. This guard is important because
-- moving clean and then work is otherwise a round trip even on re-apply:
-- clean temporarily passes work, then work passes clean again. Checking whole
-- blocks (rather than just their container tracks) preserves the same rule for
-- user-filled folders and keeps a true re-apply free of reorders.
local function containers_are_canonical_tail(clean_container, work_container)
  local clean_index = track_index(clean_container)
  local work_index = track_index(work_container)
  if not clean_index or not work_index then return false end
  local clean_last = folder_last_child_index(clean_index)
  local work_last = folder_last_child_index(work_index)
  return clean_last + 1 == work_index and work_last == reaper.CountTracks(0) - 1
end

-- Once a reference has been persisted, it is authoritative: reuse it without
-- prompting on every later apply, and never silently fall back to a
-- candidate menu while old analysis/stems/tempo data still refers to it.
-- Any problem with the persisted track stops before any mutation with a
-- message the user can act on, rather than guessing at a replacement.
local function resolve_persisted_reference(guid)
  local reference = find_track_by_guid(guid)
  if not reference then
    error(
      "the persisted reference track (" .. guid .. ") no longer exists in this project. "
      .. "Restore it, or remove config.reference_track_guid from the .vgt sidecar to choose a new reference."
    )
  end
  if starts_with_vgt(reference) then
    error("the persisted reference track (" .. guid .. ") is now a [vgt]-managed track, not a valid user reference.")
  end
  local count = file_backed_item_count(reference)
  if count == 0 then
    error("the persisted reference track \"" .. track_name(reference) .. "\" no longer has any file-backed media.")
  end
  if count > 1 then
    error(
      "the persisted reference track \"" .. track_name(reference) .. "\" now has " .. count
      .. " file-backed items, which is ambiguous. A reference must be exactly one file-backed mix item."
    )
  end
  return reference
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
  -- vgt_sync.lua reads those edits back into the sidecar.
  if locked ~= false then reaper.SetMediaItemInfo_Value(item, "C_LOCK", 1) end
  reaper.GetSetMediaItemInfo_String(item, "P_NOTES", label, true)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", label, true)
end

local function add_locked_track(index, name, muted, role)
  reaper.InsertTrackAtIndex(index, true)
  local track = reaper.GetTrack(0, index)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", muted and 1 or 0)
  mark_track_managed(track, role)
  return track
end

-- Derived from the reference's one file-backed item (see has_file_backed_media),
-- never from the span of every item on the track: a reference track that also
-- carries other, non-file-backed items must not have its placements stretched
-- to cover them.
local function reference_start_and_end(reference)
  for index = 0, reaper.CountTrackMediaItems(reference) - 1 do
    local item = reaper.GetTrackMediaItem(reference, index)
    local take = reaper.GetActiveTake(item)
    if take then
      local filename = reaper.GetMediaSourceFileName(reaper.GetMediaItemTake_Source(take), "")
      if filename ~= "" then
        local start = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
        local finish = start + reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
        return start, finish
      end
    end
  end
  return 0, 0
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
  -- On a refresh, clear every marker vgt previously wrote first (index 0 can
  -- only be updated, never deleted) so the new map is built from a clean
  -- slate -- otherwise a stale marker left at the old downbeat/span times
  -- would linger and make the map internally inconsistent.
  for index = reaper.CountTempoTimeSigMarkers(0) - 1, 1, -1 do
    reaper.DeleteTempoTimeSigMarker(0, index)
  end
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

-- A canonical snapshot of every tempo/time-sig marker currently in the
-- project, in REAPER's own marker order. Comparing this against the
-- fingerprint recorded the last time vgt wrote the map is how a re-apply
-- tells "still exactly what vgt left it as" apart from "user has since
-- edited it by hand" -- only the former is safe to overwrite.
local function current_tempo_fingerprint()
  local parts = {}
  for index = 0, reaper.CountTempoTimeSigMarkers(0) - 1 do
    local ok, time, _, _, bpm, numerator, denominator = reaper.GetTempoTimeSigMarker(0, index)
    if ok then
      parts[#parts + 1] = string.format("%.6f:%.3f:%d:%d", time, bpm, numerator, denominator)
    end
  end
  return table.concat(parts, ";")
end

-- Deterministically predicts what current_tempo_fingerprint() would read
-- immediately after a clean apply_tempo_map(tempo, reference_start) call --
-- the same marker time/bpm/timesig values, in the same write order -- without
-- touching REAPER at all. Comparing this against the live fingerprint on a
-- later apply is how an interrupted tempo mutation (#139) is told apart from
-- a genuinely partial write or a user edit made since: see the tempo
-- transaction recovery in apply() below.
local function predicted_tempo_fingerprint(tempo, reference_start)
  local bpm = tonumber(tempo.bpm)
  if not bpm or bpm <= 0 then return nil end
  local numerator, denominator = parse_time_signature(tempo.time_signature)
  local parts = {string.format("%.6f:%.3f:%d:%d", 0, bpm, numerator, denominator)}
  local downbeat = reference_start + (tonumber(tempo.downbeat_offset_seconds) or 0)
  parts[#parts + 1] = string.format("%.6f:%.3f:%d:%d", downbeat, bpm, numerator, denominator)
  if tempo.mode == "piecewise" and type(tempo.spans) == "table" then
    for _, span in ipairs(tempo.spans) do
      local span_bpm = tonumber(span.bpm)
      if span_bpm and span_bpm > 0 then
        parts[#parts + 1] = string.format("%.6f:%.3f:%d:%d", reference_start + (tonumber(span.start_seconds) or 0), span_bpm, numerator, denominator)
      end
    end
  end
  return table.concat(parts, ";")
end

-- Durable (project-scoped, survives a Lua-level crash the same way
-- record_region_ids_ext_state does -- see its comment above) record of an
-- in-flight tempo mutation, written *before* the first marker is touched.
-- `prior_fp` is the live tempo fingerprint immediately before mutation
-- began; `target_data_fp` is the tempo-data fingerprint (tempo.py's
-- detected/corrected values, not marker geometry) this transaction intends
-- to realize; `completed_fp` is filled in once the mutation itself has
-- finished, right before write_settings mirrors the result into the
-- sidecar. A crash at any point leaves exactly one of these three states
-- behind, and apply()'s recovery step below can tell which.
local PROJ_EXT_TEMPO_KEY = "tempo_txn"

local function write_tempo_txn(prior_fp, target_data_fp, completed_fp)
  reaper.SetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_TEMPO_KEY, table.concat({prior_fp or "", target_data_fp or "", completed_fp or ""}, "|"))
end

local function read_tempo_txn()
  local _, value = reaper.GetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_TEMPO_KEY)
  if not value or value == "" then return nil end
  local prior_fp, target_data_fp, completed_fp = value:match("^(.-)|(.-)|(.*)$")
  if not prior_fp then return nil end
  return {prior_fp = prior_fp, target_data_fp = target_data_fp, completed_fp = completed_fp}
end

-- Empty clears the key outright (SetProjExtState's documented behavior for
-- an empty value), matching record_region_ids_ext_state's convention above.
local function clear_tempo_txn()
  reaper.SetProjExtState(0, PROJ_EXT_SECTION, PROJ_EXT_TEMPO_KEY, "")
end

local function add_beat_markers(track, tempo, reference_start, reference_end)
  if type(tempo.beat_times) == "table" then
    for beat, offset in ipairs(tempo.beat_times) do
      local time = reference_start + (tonumber(offset) or -1)
      local next_offset = tonumber(tempo.beat_times[beat + 1])
      local finish = next_offset and reference_start + next_offset or reference_end
      if time >= reference_start and time < reference_end then
        add_labeled_item(track, time, math.min(math.max(finish, time), reference_end), "Beat " .. beat)
      end
    end
    return
  end
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

local function offer_beats_track(index, tempo, reference_start, reference_end, managed_tracks)
  -- This is an item-label-only track, so it does not need muting. Keeping it
  -- unmuted ensures its beat labels remain readable in REAPER.
  local beats = add_locked_track(index, BEATS_NAME, false, "beats")
  add_beat_markers(beats, tempo, reference_start, reference_end)
  managed_tracks[#managed_tracks + 1] = beats
end

-- Key is a silent, unmuted label track. Its single take name is the
-- correction surface read by vgt_sync.lua: `<pitch class> <major|minor>`,
-- for example `E minor`.
local function add_key_track(index, key, reference_start, reference_end, managed_tracks)
  if type(key) ~= "table" then return end
  local root, scale = key.root, key.scale
  if type(root) ~= "string" or root == "" or type(scale) ~= "string" or scale == "" then return end
  if reference_end <= reference_start then return end
  local key_track = add_locked_track(index, KEY_NAME, false, "key")
  -- Unlike beat labels, this item is intentionally unlocked for correction.
  add_labeled_item(key_track, reference_start, reference_end, root .. " " .. scale, false)
  managed_tracks[#managed_tracks + 1] = key_track
end

-- Imports the rendered click WAV (analysis.tempo.value.click_artifact_path,
-- a filename under the project's vgt/<namespace>/ folder) as a muted audio track, so
-- it never surprises the user with playback -- unlike Beats/Chords, which
-- are read rather than heard, this one only needs to exist for the user to
-- unmute it. Absent gracefully if `vgt analyze` has not produced the
-- artifact yet, or if no namespace has been recorded yet.
local function add_click_track(index, tempo, reference_start, managed_tracks, artifact_namespace)
  local filename = tempo.click_artifact_path
  if not filename or filename == "" then return end
  if not artifact_namespace or artifact_namespace == "" then return end
  local click_path = project_dir() .. "vgt/" .. tostring(artifact_namespace) .. "/" .. tostring(filename)
  local probe = io.open(click_path, "rb")
  if not probe then return end
  probe:close()
  local source = reaper.PCM_Source_CreateFromFile(click_path)
  if not source then return end
  local click_track = add_locked_track(index, CLICK_NAME, true, "click")
  local item = reaper.AddMediaItemToTrack(click_track)
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", reference_start)
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.GetMediaSourceLength(source))
  -- Tempo maps must never stretch vgt-owned audio.
  reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.SetMediaItemTake_Source(take, source)
  managed_tracks[#managed_tracks + 1] = click_track
end

-- Separation can take several minutes and is the Python process's exclusive
-- sidecar-writing window.  A current heartbeat (or, for older producers, the
-- start timestamp) means we must leave both the project and sidecar alone.
-- Timestamps are UTC ISO-8601, which is deliberately parsed without relying
-- on a machine-local timezone.
local function utc_timestamp_seconds(value)
  if type(value) ~= "string" then return nil end
  local year, month, day, hour, minute, second = value:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)T(%d%d):(%d%d):(%d%d)Z$")
  if not year then return nil end
  return os.time({year = tonumber(year), month = tonumber(month), day = tonumber(day), hour = tonumber(hour), min = tonumber(minute), sec = tonumber(second), isdst = false})
end

local function stems_lease_is_live(stems)
  local lease = type(stems) == "table" and stems.in_progress or nil
  if type(lease) ~= "table" then return false end
  local timestamp = utc_timestamp_seconds(lease.heartbeat_at or lease.heartbeat or lease.started_at or lease.start)
  -- `os.time(table)` uses the local timezone.  Interpret both ends the same
  -- way so the elapsed-time comparison remains UTC-correct on every host.
  local utc_now = os.time(os.date("!*t"))
  return timestamp ~= nil and utc_now - timestamp >= 0 and utc_now - timestamp < STEM_LEASE_TIMEOUT_SECONDS
end

-- `artifact.file` is stored relative to the song folder -- the same form the
-- separator writes (see separation.py's artifact_path) -- so it always carries
-- its own `vgt/<namespace>/` prefix. Rebuilding that prefix here and demanding
-- an exact match is what keeps the import confined to the committed namespace.
local function valid_stem_artifact(artifact, expected_filename, artifact_namespace)
  if type(artifact) ~= "table" or type(artifact.file) ~= "string" then return nil, "sidecar record is missing file" end
  if not artifact_namespace or artifact_namespace == "" or artifact_namespace:find("[\\/]") or artifact_namespace:find("%.%.") then
    return nil, "sidecar artifact namespace is invalid"
  end
  if artifact.file ~= "vgt/" .. artifact_namespace .. "/" .. expected_filename then
    return nil, "sidecar file is outside the expected stem namespace"
  end
  local path = project_dir() .. artifact.file
  local file = io.open(path, "rb")
  if not file then return nil, "WAV is missing" end
  local size = file:seek("end")
  file:seek("set", 0)
  local header = file:read(12) or ""
  file:close()
  if type(artifact.size_bytes) ~= "number" or artifact.size_bytes <= 0 or size ~= artifact.size_bytes then
    return nil, "WAV size does not match its committed artifact record"
  end
  if type(artifact.duration_seconds) ~= "number" or artifact.duration_seconds <= 0 then
    return nil, "WAV duration is invalid in its committed artifact record"
  end
  if header:sub(1, 4) ~= "RIFF" or header:sub(9, 12) ~= "WAVE" then return nil, "WAV header is invalid" end
  return path
end

-- MIDI records follow the same namespace rule as stems. Unlike WAV stem
-- records they have no committed size/duration metadata; REAPER validates the
-- MIDI data when it opens the source.
local function valid_midi_artifact(record, target, variant_id, artifact_namespace, allow_legacy_path)
  if type(record) ~= "table" or type(record.midi_file) ~= "string" then return nil, "sidecar record is missing midi_file" end
  if not artifact_namespace or artifact_namespace == "" or artifact_namespace:find("[\\/]") or artifact_namespace:find("%.%.") then
    return nil, "sidecar artifact namespace is invalid"
  end
  -- Variant IDs are artifact identities, not labels. Restrict them before
  -- constructing a filename so no sidecar value can introduce traversal.
  if type(variant_id) ~= "string" or not variant_id:match("^[%w_-]+$") then
    return nil, "variant id is invalid"
  end
  local expected_filename = "transcription/" .. target .. "/" .. variant_id .. ".mid"
  -- v13 migration retains this exact old flat path until the next refresh.
  if allow_legacy_path then expected_filename = "transcription/" .. target .. ".mid" end
  if record.midi_file ~= expected_filename then return nil, "sidecar MIDI file is outside the expected transcription namespace" end
  local path = project_dir() .. "vgt/" .. artifact_namespace .. "/" .. record.midi_file
  local file = io.open(path, "rb")
  if not file then return nil, "MIDI is missing" end
  file:close()
  return path
end

local function transcription_definition(target)
  local definition = nil
  for _, candidate in ipairs(TRANSCRIPTION_TARGETS) do
    if candidate.target == target then definition = candidate break end
  end
  return definition
end

local function add_reference_midi_variant(index, target, variant_id, variant, reference_start, reference_end, managed_tracks, artifact_namespace, allow_legacy_path, legacy_track_name)
  if type(variant) ~= "table" or variant.status ~= "transcribed" then return index, false end
  local definition = transcription_definition(target)
  if not definition then return index, true end
  local path, reason = valid_midi_artifact(variant, target, variant_id, artifact_namespace, allow_legacy_path)
  local warning_subject = "skipping transcription " .. target
  if not legacy_track_name then warning_subject = warning_subject .. " variant " .. tostring(variant_id) end
  if not path then
    warn(warning_subject .. ": " .. reason)
    return index, true
  end
  local source = reaper.PCM_Source_CreateFromFile(path)
  if not source then
    warn(warning_subject .. ": REAPER could not open MIDI")
    return index, true
  end
  -- MIDI has no instrument on this track, so it is silent but remains visible
  -- and readable in the arrange view like Chords and Beats.
  local label = type(variant.label) == "string" and variant.label or tostring(variant_id)
  local name = PREFIX .. " " .. definition.label .. " Ref — " .. label .. " (MIDI)"
  if legacy_track_name then name = PREFIX .. " " .. definition.label .. " Ref (MIDI)" end
  local midi_track = add_locked_track(index, name, false, "variant:" .. target .. ":" .. variant_id)
  local item = reaper.AddMediaItemToTrack(midi_track)
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", reference_start)
  -- Span the reference track, exactly like Key and the stems. A MIDI source's
  -- GetMediaSourceLength is *quarter notes*, not the seconds it returns for a
  -- WAV, and it stops at the last note rather than at the end of the song, so
  -- it is the wrong number twice over; it stays only as a last resort for a
  -- caller that has no reference span to give.
  local length = (tonumber(reference_end) or 0) - reference_start
  if length <= 0 then length = reaper.GetMediaSourceLength(source) end
  reaper.SetMediaItemInfo_Value(item, "D_LENGTH", length)
  -- Transcriptions that end before the song must not repeat to fill the item.
  reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", 0)
  reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
  local take = reaper.AddTakeToMediaItem(item)
  reaper.SetMediaItemTake_Source(take, source)
  managed_tracks[#managed_tracks + 1] = midi_track
  return index + 1, true
end

-- Retained variants are imported only in explicit variant_order. A malformed
-- duplicate in that order still creates one generated track, never two.
local function add_reference_midi_tracks(index, target, transcription, reference_start, reference_end, managed_tracks, artifact_namespace)
  local record = type(transcription) == "table" and transcription.targets and transcription.targets[target] or nil
  if type(record) ~= "table" then return index, false end
  local imported = false
  if type(record.variants) == "table" and type(record.variant_order) == "table" then
    local seen = {}
    for _, variant_id in ipairs(record.variant_order) do
      if not seen[variant_id] then
        seen[variant_id] = true
        local variant = record.variants[variant_id]
        -- Only migration-produced records retain flat status and the legacy path.
        local allow_legacy_path = record.status ~= nil and type(variant) == "table"
          and variant.midi_file == "transcription/" .. target .. ".mid"
        local next_index, attempted = add_reference_midi_variant(index, target, variant_id, variant,
          reference_start, reference_end, managed_tracks, artifact_namespace, allow_legacy_path, allow_legacy_path)
        index = next_index
        imported = imported or attempted
      end
    end
    return index, imported
  end
  -- Pre-v13 sidecars retain their old name and path until sidecar upgrade.
  if record.status ~= "transcribed" then return index, false end
  local legacy = {}
  for key, value in pairs(record) do legacy[key] = value end
  legacy.label = "default"
  return add_reference_midi_variant(index, target, "legacy", legacy, reference_start, reference_end,
    managed_tracks, artifact_namespace, true, true)
end

-- Import only records that the separator committed into its own
-- namespace.  The API gets an absolute local path, but saving the project is
-- what causes REAPER to serialize it project-relative; the live verifier
-- checks that persisted behavior separately.
local function add_stem_tracks(index, stems, transcription, reference_start, reference_end, managed_tracks)
  -- Keep the helper callable by older local automation snippets: those pass
  -- no reference_end, or only stems/reference_start/managed_tracks.
  if managed_tracks == nil and type(reference_end) == "table" then
    managed_tracks = reference_end
    reference_end = nil
  end
  if managed_tracks == nil then
    managed_tracks = reference_start
    reference_start = transcription
    transcription = nil
    reference_end = nil
  end
  local artifacts = type(stems) == "table" and stems.artifacts or nil
  local artifact_namespace = type(stems) == "table" and stems.artifact_namespace or nil
  local imported_stems = {}
  for _, definition in ipairs(STEM_TRACKS) do
    -- Omitted optional records are not an error: strings/piano are opt-in.
    if type(artifacts) == "table" and artifacts[definition.artifact] ~= nil then
      local path, reason = valid_stem_artifact(artifacts[definition.artifact], definition.filename, artifact_namespace)
      if not path then
        warn("skipping stem " .. definition.artifact .. ": " .. reason)
      else
        local source = reaper.PCM_Source_CreateFromFile(path)
        if not source then
          warn("skipping stem " .. definition.artifact .. ": REAPER could not open WAV")
        else
          local stem_track = add_locked_track(index, definition.name, false, "stem:" .. definition.artifact)
          local item = reaper.AddMediaItemToTrack(stem_track)
          reaper.SetMediaItemInfo_Value(item, "D_POSITION", reference_start)
          reaper.SetMediaItemInfo_Value(item, "D_LENGTH", reaper.GetMediaSourceLength(source))
          reaper.SetMediaItemInfo_Value(item, "C_BEATATTACHMODE", 0)
          local take = reaper.AddTakeToMediaItem(item)
          reaper.SetMediaItemTake_Source(take, source)
          managed_tracks[#managed_tracks + 1] = stem_track
          index = index + 1
          imported_stems[definition.artifact] = true
          index = add_reference_midi_tracks(index, definition.artifact, transcription, reference_start, reference_end, managed_tracks, artifact_namespace)
        end
      end
    end
  end
  -- `original` and targets whose stem did not import have no adjacent stem.
  -- Keep them anyway, after the stem block, in Python's target-table order.
  for _, definition in ipairs(TRANSCRIPTION_TARGETS) do
    if not imported_stems[definition.target] then
      index = add_reference_midi_tracks(index, definition.target, transcription, reference_start, reference_end, managed_tracks, artifact_namespace)
    end
  end
end

local function remove_previous_managed_regions()
  -- Either the sidecar's list or the durable ProjExtState record is evidence
  -- of ownership -- see record_region_ids_ext_state above for why the latter
  -- is necessary. A region ID is deleted purely by identity: unlike tracks, a
  -- `[vgt]`-prefixed name is never itself required or sufficient, so a region
  -- the user renamed (see vgt_sync.lua, which reads corrected names/geometry
  -- back by ID regardless of prefix) is still reconciled correctly here.
  local managed = read_managed_region_ids()
  for id in pairs(read_region_ids_ext_state()) do managed[id] = true end
  for index = reaper.CountProjectMarkers(0) - 1, 0, -1 do
    local _, is_region, _, _, _, region_id = reaper.EnumProjectMarkers3(0, index)
    if is_region and managed[region_id] then reaper.DeleteProjectMarker(0, region_id, true) end
  end
end

local function add_sections(sections, reference_start)
  local region_ids = {}
  if type(sections) == "table" then
    for _, section in ipairs(sections) do
      local start_time = reference_start + (tonumber(section.start_seconds) or 0)
      local end_time = reference_start + (tonumber(section.end_seconds) or 0)
      local label = tostring(section.label or section.name or "section")
      if end_time > start_time then
        region_ids[#region_ids + 1] = reaper.AddProjectMarker2(0, true, start_time, end_time, PREFIX .. " " .. label, -1, 0)
        -- Recorded immediately after each region is created, well before
        -- write_settings runs -- see record_region_ids_ext_state.
        record_region_ids_ext_state(region_ids)
      end
    end
  end
  -- Refresh unconditionally, including the empty-list case: a re-apply that
  -- ends up with fewer (or zero) sections must not leave a prior run's now-
  -- deleted IDs sitting in ProjExtState.
  record_region_ids_ext_state(region_ids)
  return region_ids
end

local function write_settings(managed_tracks, managed_region_ids, reference, tempo_map_applied, tempo_map_fingerprint, tempo_data_fp, guitar_type)
  local guids = {}
  for _, track in ipairs(managed_tracks) do guids[#guids + 1] = '"' .. reaper.GetTrackGUID(track) .. '"' end
  local region_ids = {}
  for _, region_id in ipairs(managed_region_ids) do region_ids[#region_ids + 1] = tostring(region_id) end

  -- Shared sidecar commit protocol (#138): Python holds its own `fcntl` lock
  -- across a read-merge-write, but this ReaScript action cannot take that
  -- lock. Instead it re-reads the sidecar as late as possible on every
  -- attempt -- so a `vgt analyze` commit that lands mid-apply is what gets
  -- merged rather than silently rolled back -- and re-checks `generation`
  -- one last time right before the atomic rename. A mismatch there means
  -- Python committed in the gap; retry the merge against that newer state
  -- instead of renaming a stale one over it.
  for attempt = 1, GENERATION_RETRY_LIMIT do
    -- Preserve any analysis the Python CLI already wrote (schema v4); a fresh
    -- sidecar with no prior analysis stays schema v1, matching Phase 0's
    -- long-standing on-disk format.
    local prior_body = read_sidecar_body() or ""
    local analysis = read_analysis_block(prior_body)
    local prior_schema = tonumber(prior_body:match('"schema_version"%s*:%s*(%d+)')) or 3
    local schema_version = analysis and math.max(prior_schema, 4) or 1
    local analysis_field = analysis and ('\n  "analysis": ' .. analysis .. ",") or ""
    local generation = read_generation(prior_body)

    -- Write a complete replacement beside the sidecar, then rename it into
    -- place.  Python uses the same replace discipline for paid-operation
    -- checkpoints; a crash can therefore leave either complete version, never
    -- a truncated JSON document.
    local temporary_path = sidecar_path() .. ".tmp"
    local file, error_message = io.open(temporary_path, "w")
    if not file then error(error_message) end
    file:write(string.format([[{
  "schema_version": %d,%s
  "generation": %d,
  "managed_track_guids": [%s],
  "managed_region_ids": [%s],
  "config": {"reference_track_name": "%s", "reference_track_guid": "%s", "folder_name": "%s", "tempo_map_applied": %s, "tempo_map_fingerprint": "%s", "tempo_data_fingerprint": "%s", "guitar_type": "%s"}
}
]],
      schema_version, analysis_field, generation + 1,
      table.concat(guids, ", "), table.concat(region_ids, ", "),
      escaped(track_name(reference)), reaper.GetTrackGUID(reference),
      escaped(PREFIX .. " " .. track_name(reference)), tempo_map_applied and "true" or "false",
      escaped(tempo_map_fingerprint or ""), escaped(tempo_data_fp or ""), escaped(guitar_type)))
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
  error("vgt: the sidecar changed concurrently " .. GENERATION_RETRY_LIMIT .. " time(s) while applying; run vgt_initialize.lua again.")
end

local function apply()
  local path = project_path()
  if path == "" then error("Save the REAPER project before running vgt Phase 0.") end

  local analysis = read_analysis()
  if analysis and stems_lease_is_live(analysis.stems) then
    warn("stem separation is in progress; retry after it finishes")
    return
  end

  -- Ask before touching the project so dismissing either declaration menu
  -- leaves both REAPER and the sidecar unchanged.
  local guitar_type = choose_guitar_type()
  if not guitar_type then return end

  -- Resolve the reference before mutating anything, so cancelling or a stale
  -- persisted reference leaves the project untouched. A previously persisted
  -- GUID is authoritative and reused without prompting; only a project with
  -- no persisted reference yet goes through the interactive/automation pick.
  local persisted_guid = persisted_reference_guid()
  local reference
  if persisted_guid ~= "" then
    reference = resolve_persisted_reference(persisted_guid)
  else
    reference = choose_reference(candidate_tracks())
    if not reference then return end
  end
  local reference_guid = reaper.GetTrackGUID(reference)
  local folder_name = PREFIX .. " " .. track_name(reference)

  -- Choosing a reference may involve an interactive pause. Check again
  -- immediately before the first project mutation so a separator that began
  -- while the menu was open wins cleanly rather than having its sidecar
  -- checkpoint overwritten by this apply.
  analysis = read_analysis()
  if analysis and stems_lease_is_live(analysis.stems) then
    warn("stem separation is in progress; retry after it finishes")
    return
  end

  -- This is deliberately the last operation before Undo_BeginBlock.  It
  -- reads all ownership representations while the project is untouched and
  -- fails closed on a root that cannot be authenticated.
  validate_reconciliation_inventory(analysis)

  reaper.Undo_BeginBlock()
  reaper.PreventUIRefresh(1)
  remove_previous_managed_tracks()
  remove_previous_managed_regions()

  -- Re-resolve the reference by GUID: deleting the previous managed tracks can invalidate the pointer.
  reference = find_track_by_guid(reference_guid)
  if not reference then
    reaper.PreventUIRefresh(-1)
    error("the chosen reference track no longer exists.")
  end

  -- Resolve/adopt/create the [clean] and [work] scaffold containers, then
  -- move each block (container + its contents) to the end of the project in
  -- that order, before the [vgt] root below claims reaper.CountTracks(0) as
  -- its own insertion point. Sequencing is load-bearing: add_stem_tracks,
  -- add_click_track, add_key_track, and the chords track all append at
  -- CountTracks(0), so a container sitting below [vgt] at that moment would
  -- capture every track vgt creates from here on. Skip a kind entirely (do
  -- not move it) when maintain_container found it ambiguous this run.
  local reference_name = track_name(reference)
  local clean_container = maintain_container(CONTAINER_DEFS[1], reference_name)
  local work_container = maintain_container(CONTAINER_DEFS[2], reference_name)
  local canonical_tail = clean_container and work_container
    and containers_are_canonical_tail(clean_container, work_container)
  if not canonical_tail then
    if clean_container then move_container_block_to_end(clean_container) end
    if work_container then move_container_block_to_end(work_container) end
  end

  local insert_at = reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(insert_at, true)
  local folder = reaper.GetTrack(0, insert_at)
  reaper.GetSetMediaTrackInfo_String(folder, "P_NAME", folder_name, true)
  mark_track_managed(folder, "managed-root")
  -- The [vgt] root is deleted and rebuilt on every apply, so -- unlike
  -- [clean]/[work], which are coloured once -- it is coloured on every
  -- creation; that is inherent to it being vgt-owned, not an exception.
  reaper.SetMediaTrackInfo_Value(folder, "I_CUSTOMCOLOR", reaper.ColorToNative(VGT_COLOR[1], VGT_COLOR[2], VGT_COLOR[3]) | 0x1000000)
  -- Persist root identity as soon as it exists. A crash before sidecar commit
  -- can therefore still be recovered without treating the next run as new.
  write_root_manifest(folder, {folder})
  -- Tentatively open a folder; if nothing ends up nested under it (no tempo,
  -- click, stems, chords, or sections to add), it is flattened back to a
  -- plain track below rather than left as a folder with no children.
  reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 1)

  local managed_tracks = {folder}
  local reference_start, reference_end = reference_start_and_end(reference)
  local tempo = analysis and analysis.tempo and analysis.tempo.value
  local artifact_namespace = analysis and analysis.stems and analysis.stems.artifact_namespace
  local tempo_map_applied = prior_tempo_map_applied()
  local tempo_map_fingerprint = ""
  local tempo_data_fp = ""
  if type(tempo) == "table" and tonumber(tempo.bpm) then
    tempo_data_fp = tempo_data_fingerprint(tempo)
    local prior_map_fingerprint = prior_tempo_map_fingerprint()
    local map_untouched = tempo_map_applied and prior_map_fingerprint ~= "" and current_tempo_fingerprint() == prior_map_fingerprint

    -- Recover from a tempo mutation interrupted after write_tempo_txn below
    -- but before this run's own write_settings could mirror the result into
    -- the sidecar (#139). The transaction is proof only of what *this*
    -- vgt run intended and last observed -- it is consumed (cleared) the
    -- moment it is read, whether or not recovery actually applies, so a
    -- second crash never replays a stale decision.
    local recovered = false
    local pending_txn = read_tempo_txn()
    if pending_txn then
      clear_tempo_txn()
      if pending_txn.target_data_fp == tempo_data_fp then
        local live_fp = current_tempo_fingerprint()
        local predicted_fp = predicted_tempo_fingerprint(tempo, reference_start)
        if live_fp == predicted_fp or (pending_txn.completed_fp ~= "" and live_fp == pending_txn.completed_fp) then
          -- The interrupted run's mutation is provably complete (the live
          -- map matches exactly what it must have produced): mirror that
          -- into the sidecar below without touching REAPER again.
          tempo_map_applied = true
          tempo_map_fingerprint = live_fp
          tempo_data_fp = pending_txn.target_data_fp
          recovered = true
        end
        -- Otherwise the live map cannot be proven to belong to this
        -- transaction -- a genuinely partial write, or a user edit made
        -- after the interruption -- so fall through to the normal decision
        -- tree, which treats an unrecognized live map non-invasively.
      end
      -- A mismatched target_data_fp means the analyzed tempo data itself
      -- changed since the interrupted run; same non-invasive fallback.
    end

    if analysis.tempo.human_verified == true then
      -- A dedicated sync action may adopt a user-owned live map as analysis
      -- evidence. That never transfers map ownership to vgt: subsequent
      -- applies must remain read-only with respect to tempo markers.
      tempo_map_applied = false
      tempo_map_fingerprint = ""
      tempo_data_fp = ""
      offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)
    elseif recovered then
      -- Already resolved above; nothing further to do for tempo this run.
    elseif tempo.downbeat_detected ~= true then
      -- A beat-only result has no trustworthy bar phase. Keep the detected
      -- grid visible, but never create or refresh a bar-aligned REAPER map.
      tempo_map_applied = false
      tempo_map_fingerprint = ""
      tempo_data_fp = ""
      offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)
    elseif map_untouched and tempo_data_fp == prior_tempo_data_fingerprint() then
      -- Live map still matches what vgt wrote, and the tempo data hasn't
      -- changed either -- nothing to do. Rewriting an unchanged map would
      -- needlessly re-shift any beat-attached reference items.
      tempo_map_fingerprint = prior_map_fingerprint
    elseif map_untouched then
      -- The live map is byte-for-byte what vgt wrote last time, but the
      -- detected/corrected tempo data has since changed -- safe to refresh.
      -- Record the transaction before touching a single marker, so a crash
      -- between here and this run's own write_settings can be recovered.
      write_tempo_txn(prior_map_fingerprint, tempo_data_fp, "")
      if apply_tempo_map(tempo, reference_start) then
        tempo_map_fingerprint = current_tempo_fingerprint()
        write_tempo_txn(prior_map_fingerprint, tempo_data_fp, tempo_map_fingerprint)
        -- Rewriting the map can shift any beat-attached reference item;
        -- re-read its position so chords/sections/beats placed below land
        -- exactly where the reference audio now actually sits.
        reference_start, reference_end = reference_start_and_end(reference)
      else
        tempo_map_applied = false
        tempo_data_fp = ""
        clear_tempo_txn()
      end
    elseif tempo_map_applied then
      -- Either the user has since edited the map vgt wrote, or an older
      -- sidecar recorded no fingerprint to check against. Either way we
      -- cannot prove the map is still ours, so it is never touched again;
      -- offer the latest tempo data non-invasively instead.
      tempo_data_fp = ""
      offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)
    elseif is_single_default_tempo_marker() then
      local prior_fp = current_tempo_fingerprint()
      write_tempo_txn(prior_fp, tempo_data_fp, "")
      tempo_map_applied = apply_tempo_map(tempo, reference_start)
      if tempo_map_applied then
        tempo_map_fingerprint = current_tempo_fingerprint()
        write_tempo_txn(prior_fp, tempo_data_fp, tempo_map_fingerprint)
        reference_start, reference_end = reference_start_and_end(reference)
      else
        tempo_data_fp = ""
        clear_tempo_txn()
      end
    else
      tempo_data_fp = ""
      offer_beats_track(insert_at + 1, tempo, reference_start, reference_end, managed_tracks)
    end
  end

  if type(tempo) == "table" then
    add_click_track(reaper.CountTracks(0), tempo, reference_start, managed_tracks, artifact_namespace)
  end

  -- `value`, rather than `detected`, intentionally displays the effective
  -- analysis result, including a deliberate human sidecar override.
  add_key_track(reaper.CountTracks(0), analysis and analysis.key and analysis.key.value, reference_start, reference_end, managed_tracks)

  local chords = analysis and analysis.chords and analysis.chords.value
  local segments = type(chords) == "table" and (chords.segments or chords) or nil
  if type(segments) == "table" then
    -- Like Beats, this is an item-label-only track: muting it would only
    -- make the chord labels unreadable in REAPER, since there is no audio
    -- on it to silence.
    local chords_track = add_locked_track(reaper.CountTracks(0), CHORDS_NAME, false, "chords")
    for _, chord in ipairs(segments) do
      -- locked = false: chord items are the editing surface for corrections (see add_labeled_item).
      add_labeled_item(chords_track, reference_start + (tonumber(chord.start_seconds) or 0), reference_start + (tonumber(chord.end_seconds) or 0), tostring(chord.chord or chord.label or "N"), false)
    end
    managed_tracks[#managed_tracks + 1] = chords_track
  end

  add_stem_tracks(reaper.CountTracks(0), analysis and analysis.stems, analysis and analysis.transcription, reference_start, reference_end, managed_tracks)

  local managed_region_ids = add_sections(analysis and analysis.sections and analysis.sections.value, reference_start) or {}

  if #managed_tracks > 1 then
    -- The folder must close after every child we appended.
    reaper.SetMediaTrackInfo_Value(managed_tracks[#managed_tracks], "I_FOLDERDEPTH", -1)
  else
    -- Nothing ended up nested under the folder track -- flatten it back to a
    -- plain track rather than leave a folder with no children.
    reaper.SetMediaTrackInfo_Value(folder, "I_FOLDERDEPTH", 0)
  end

  write_settings(managed_tracks, managed_region_ids, reference, tempo_map_applied, tempo_map_fingerprint, tempo_data_fp, guitar_type)
  write_root_manifest(folder, managed_tracks)
  -- The sidecar now durably records whatever this run decided about tempo;
  -- any transaction it left behind (finalized above, or fresh from this same
  -- run) is stale from here on (#139).
  clear_tempo_txn()
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
