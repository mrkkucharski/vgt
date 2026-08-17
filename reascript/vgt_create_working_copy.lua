-- vgt: create a working copy from selected tracks for REAPER 7.x.
-- Install this file in REAPER's Action List and run it while the target RPP is open.

local source = debug.getinfo(1, "S").source
local directory = source:match("^@(.*/)")
if not directory then error("vgt create working copy must be loaded from a file") end
local actions = dofile(directory .. "vgt_common.lua")

local ok, error_message = xpcall(function() actions.run("create") end, debug.traceback)
if not ok then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("vgt create working copy failed:\n" .. error_message, "vgt", 0)
end
