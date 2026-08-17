-- vgt: get transcription (on-demand MT3 track jobs) for REAPER 7.x.
-- Install this file in REAPER's Action List and run it at any time to check
-- on and import any finished on-demand track-transcription jobs (see
-- "vgt: Transcribe selected track (MT3)" / vgt_transcribe_track.lua and
-- docs/on-demand-track-transcription-plan.md). This is a small, standalone
-- action: it only ever scans `track_jobs` and, for each finished/unimported
-- one, adds its single track -- it does NOT run `vgt apply`'s
-- reconciliation pass (tempo/key/section/chord detection, stem separation,
-- the mt3_review refresh, or working-copy reconciliation). See the plan's
-- "Independence from vgt apply" for why that separation is an explicit
-- requirement, not an implementation convenience.

local source = debug.getinfo(1, "S").source
local directory = source:match("^@(.*/)")
if not directory then error("vgt get transcription must be loaded from a file") end
local common = dofile(directory .. "vgt_common.lua")

local function human_size(bytes)
  if bytes >= 1024 * 1024 * 1024 then return string.format("%.1f GB", bytes / (1024 * 1024 * 1024)) end
  if bytes >= 1024 * 1024 then return string.format("%.1f MB", bytes / (1024 * 1024)) end
  return string.format("%.0f KB", bytes / 1024)
end

-- Total on-disk size of every job's directory (source.wav + result.mid/csv
-- accumulate forever in v1 -- see the plan's "Disk growth" failure mode; a
-- purge command is deliberately out of scope here). Reported, not acted on.
local function track_jobs_total_bytes(namespace)
  local total = 0
  for _, job_id in ipairs(common.list_job_ids(namespace)) do
    local job_dir = common.track_jobs_dir(namespace) .. "/" .. job_id
    local index = 0
    while true do
      local name = reaper.EnumerateFiles(job_dir, index)
      if not name then break end
      local path = job_dir .. "/" .. name
      local ok, size = pcall(function()
        local file = io.open(path, "rb")
        if not file then return 0 end
        local n = file:seek("end")
        file:close()
        return n or 0
      end)
      total = total + (ok and size or 0)
      index = index + 1
    end
  end
  return total
end

local function get_transcription()
  local analysis = common.read_analysis()
  if not analysis then
    reaper.ShowConsoleMsg("vgt: no .vgt sidecar found; run vgt_initialize.lua (apply) first.\n")
    return
  end
  local namespace = analysis.stems and analysis.stems.artifact_namespace
  if not namespace then
    reaper.ShowConsoleMsg("vgt: no artifact namespace recorded yet; nothing to check.\n")
    return
  end

  local job_ids = common.list_job_ids(namespace)
  if #job_ids == 0 then
    reaper.ShowConsoleMsg("vgt: no on-demand track-transcription jobs found.\n")
    return
  end

  local imported, running, errored, stale = 0, 0, 0, 0
  for _, job_id in ipairs(job_ids) do
    -- Idempotent: a job already recorded in the sidecar (imported or error)
    -- is never re-reported or re-imported.
    if not common.already_recorded(job_id) then
      local settled = common.check_and_import_job(job_id)
      if settled then
        local record = (common.read_analysis().track_jobs or {})[job_id]
        if record and record.status == "imported" then imported = imported + 1
        elseif record and record.status == "error" then errored = errored + 1
        else stale = stale + 1 end
      else
        running = running + 1
      end
    end
  end

  reaper.ShowConsoleMsg(string.format(
    "vgt: track jobs -- imported: %d, errored: %d, still running: %d, stale/unresponsive: %d (of %d total)\n",
    imported, errored, running, stale, #job_ids
  ))
  local total_bytes = track_jobs_total_bytes(namespace)
  if total_bytes > 200 * 1024 * 1024 then
    reaper.ShowConsoleMsg(string.format(
      "vgt: track-jobs/ is using %s across %d job(s); old jobs are never cleaned up automatically.\n",
      human_size(total_bytes), #job_ids
    ))
  end
end

local ok, error_message = xpcall(get_transcription, debug.traceback)
if not ok then
  reaper.ShowMessageBox("vgt get transcription failed:\n" .. error_message, "vgt", 0)
end
