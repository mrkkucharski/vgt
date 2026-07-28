-- Explicitly adopt the live REAPER tempo map as a human-verified vgt tempo.
-- This is deliberately a separate Action: vgt_sync.lua never reads tempo
-- markers, so ordinary chord/section synchronization cannot claim an
-- unrelated pre-existing project map as a correction.

local RETRIES = 5

local function project_path() local _, p = reaper.EnumProjects(-1, ""); return p end
local function sidecar_path() return (project_path():gsub("%.[^./\\]*$", ".vgt")) end
local function read_body()
  local f = io.open(sidecar_path(), "r"); if not f then return nil end
  local v = f:read("*a"); f:close(); return v
end

-- Small complete JSON codec.  The sidecar is vgt-owned JSON, but values such
-- as human chord labels may contain escapes, so regex replacement is unsafe.
local function decode(text)
  local p = 1
  local function ws() while text:sub(p,p):match("%s") do p=p+1 end end
  local value
  local function str()
    p=p+1; local out={}
    while p<=#text do local c=text:sub(p,p); p=p+1
      if c=='"' then return table.concat(out) end
      if c~='\\' then out[#out+1]=c else
        local e=text:sub(p,p); p=p+1
        local map={['"']='"',['\\']='\\',['/']='/',b='\b',f='\f',n='\n',r='\r',t='\t'}
        if e=='u' then local n=tonumber(text:sub(p,p+3),16); p=p+4; out[#out+1]=(n and utf8.char(n)) or '?' elseif map[e] then out[#out+1]=map[e] else error('bad JSON escape') end
      end
    end; error('unterminated JSON string')
  end
  local function arr() local r={}; p=p+1; ws(); if text:sub(p,p)==']' then p=p+1; return r end
    while true do r[#r+1]=value(); ws(); local c=text:sub(p,p); p=p+1; if c==']' then return r end; if c~=',' then error('bad JSON array') end; ws() end end
  local function obj() local r={}; p=p+1; ws(); if text:sub(p,p)=='}' then p=p+1; return r end
    while true do if text:sub(p,p)~='"' then error('bad JSON object') end; local k=str(); ws(); if text:sub(p,p)~=':' then error('bad JSON object') end; p=p+1; r[k]=value(); ws(); local c=text:sub(p,p); p=p+1; if c=='}' then return r end; if c~=',' then error('bad JSON object') end; ws() end end
  value=function() ws(); local c=text:sub(p,p); if c=='"' then return str() elseif c=='{' then return obj() elseif c=='[' then return arr() end
    local n=text:sub(p):match('^-?%d+%.?%d*[eE]?[-+]?%d*'); if n and n~='' then p=p+#n; return tonumber(n) end
    if text:sub(p,p+3)=='null' then p=p+4; return nil elseif text:sub(p,p+3)=='true' then p=p+4; return true elseif text:sub(p,p+4)=='false' then p=p+5; return false end; error('bad JSON value') end
  local r=value(); ws(); if p<=#text then error('trailing JSON') end; return r
end
local function encode(v)
  local t=type(v); if t=='nil' then return 'null' elseif t=='boolean' then return v and 'true' or 'false' elseif t=='number' then return string.format('%.10g',v) elseif t=='string' then return '"'..v:gsub('\\','\\\\'):gsub('"','\\"'):gsub('\n','\\n'):gsub('\r','\\r'):gsub('\t','\\t')..'"' end
  local n=0; local array=true; for k in pairs(v) do n=n+1; if type(k)~='number' or k<1 or k>n or k~=math.floor(k) then array=false end end
  local out={}; if array then for i=1,n do out[#out+1]=encode(v[i]) end; return '['..table.concat(out,',')..']' end
  local keys={}; for k in pairs(v) do keys[#keys+1]=k end; table.sort(keys); for _,k in ipairs(keys) do out[#out+1]=encode(k)..':'..encode(v[k]) end; return '{'..table.concat(out,',')..'}'
end

local function reference_extent(data)
  local guid = data.config and data.config.reference_track_guid
  if not guid then error('sidecar has no reference track; run vgt_initialize.lua first.') end
  local track
  for i=0,reaper.CountTracks(0)-1 do local candidate=reaper.GetTrack(0,i); if reaper.GetTrackGUID(candidate)==guid then track=candidate break end end
  if not track then error('the selected reference track no longer exists.') end
  if reaper.CountTrackMediaItems(track)~=1 then error('the selected reference must have exactly one item.') end
  local item=reaper.GetTrackMediaItem(track,0); local start=reaper.GetMediaItemInfo_Value(item,'D_POSITION'); local length=reaper.GetMediaItemInfo_Value(item,'D_LENGTH')
  if not start or not length or length<=0 then error('the selected reference has an invalid time extent.') end
  return start,start+length
end
local function finite(v) return type(v)=='number' and v<math.huge and v>-math.huge and v==v end
local function finite_positive(v) return finite(v) and v>0 end
local function live_tempo_value(start_time, end_time)
  local count=reaper.CountTempoTimeSigMarkers(0)
  if count<1 then error('the project has no explicit tempo/time-signature markers to adopt.') end
  local base
  local spans={}
  for i=0,count-1 do
    local ok,time,_,_,bpm,num,den,linear=reaper.GetTempoTimeSigMarker(0,i)
    if not ok or not finite(time) or not finite_positive(bpm) or not finite_positive(num) or not finite_positive(den) then error('the project tempo map contains an invalid marker.') end
    if linear then error('the project tempo map contains a linear ramp, which vgt cannot faithfully synchronize.') end
    if time<=start_time then base={time=time,bpm=bpm,num=num,den=den} elseif time<end_time then spans[#spans+1]={start_seconds=math.floor((time-start_time)*1e6+0.5)/1e6,bpm=bpm,num=num,den=den} end
  end
  if not base then error('the reference begins before the first tempo marker; add an explicit marker at or before it.') end
  local signature=string.format('%d/%d',base.num,base.den)
  for _,span in ipairs(spans) do
    span.time_signature=string.format('%d/%d',span.num,span.den); span.num=nil; span.den=nil
  end
  return {bpm=base.bpm,time_signature=signature,mode=(#spans>0 and 'piecewise' or 'constant'),spans=spans,source='reaper-tempo-map'}
end
local function write_value(value)
  for _=1,RETRIES do
    local body=read_body(); if not body then error('No .vgt sidecar found; run vgt_initialize.lua first.') end
    local data=decode(body); if type(data.analysis)~='table' or type(data.analysis.tempo)~='table' then error('sidecar has no analyzed tempo stage; run vgt analyze first.') end
    data.analysis.tempo.value=value; data.analysis.tempo.human_verified=true; data.analysis.tempo.verified_at=os.date('!%Y-%m-%dT%H:%M:%SZ'); data.schema_version=math.max(tonumber(data.schema_version) or 0,16)
    local generation=tonumber(data.generation) or 0; data.generation=generation+1
    local f,err=io.open(sidecar_path()..'.tmp','w'); if not f then error(err) end; f:write(encode(data)); f:close()
    local latest=read_body(); local latest_generation=latest and tonumber(latest:match('"generation"%s*:%s*(%d+)')) or 0
    if latest_generation==generation then local ok,rename_err=os.rename(sidecar_path()..'.tmp',sidecar_path()); if ok then return end; error(rename_err) end
    os.remove(sidecar_path()..'.tmp')
  end; error('vgt: sidecar changed concurrently; run the tempo-map sync action again.')
end
local function sync_tempo_map()
  if project_path()=='' then error('Save the REAPER project before syncing.') end
  local answer=reaper.ShowMessageBox('Adopt the live REAPER tempo map for the selected reference?\n\nThis only updates vgt\'s sidecar. It never edits or claims ownership of the project map.', 'vgt: adopt tempo map', 4)
  if answer~=6 then return end
  local body=read_body(); if not body then error('No .vgt sidecar found; run vgt_initialize.lua first.') end
  local data=decode(body); local start_time,end_time=reference_extent(data); write_value(live_tempo_value(start_time,end_time))
  reaper.ShowConsoleMsg('vgt: adopted the live tempo map for the selected reference as human-verified.\n')
end
local ok,err=xpcall(sync_tempo_map,debug.traceback); if not ok then reaper.ShowMessageBox('vgt: tempo-map sync failed:\n'..err,'vgt',0) end
