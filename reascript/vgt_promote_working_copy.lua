-- vgt: promote selected working copies for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.

local source = debug.getinfo(1, "S").source
local directory = source:match("^@(.*/)")
if not directory then error("vgt promote working copy must be loaded from a file") end
local actions = dofile(directory .. "vgt_working_copy_common.lua")

local ok, error_message = xpcall(function() actions.run("promote") end, debug.traceback)
if not ok then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("vgt promote working copy failed:\n" .. error_message, "vgt", 0)
end
