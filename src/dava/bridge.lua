-- dava bridge for DaVinci Resolve's Lua console (works with the free edition).
--
-- Start it in Workspace > Console (Lua) with:  dofile(os.getenv("HOME") .. "/.dava/bridge.lua")
-- Stop it with 'dava bridge stop'.
--
-- The console's Lua is sandboxed (no io, no require, no sockets), so the bridge talks through what it has:
--   requests:  dava writes ~/.dava/lua/request.lua (a Lua data table); the loop reads it with dofile.
--   responses: raised as "DAVA-RESP <seq> <part>/<total> <json>" errors inside fusion:Execute, which
--              Resolve writes to its log (and the console); dava reads them from the log.
-- Only the current user can write ~/.dava/lua, so only the user can send requests.

local DIR = os.getenv("HOME") .. "/.dava/lua/"
local REQUEST = DIR .. "request.lua"
local STOP = DIR .. "stop"
local CHUNK = 900
local POLL = 0.05

DAVA_NIL = DAVA_NIL or {}  -- stands for nil inside request tables

local objects = { [0] = resolve }
local next_id = 1

local function register(value)
  local id = next_id
  next_id = next_id + 1
  objects[id] = value
  return id
end

local function is_array(t)
  local n = 0
  for k in pairs(t) do
    if type(k) ~= "number" or k < 1 or k % 1 ~= 0 then
      return false, 0
    end
    n = n + 1
  end
  for i = 1, n do
    if t[i] == nil then
      return false, 0
    end
  end
  return true, n
end

local ESCAPES = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
                  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

local function json_string(s)
  -- Only ASCII control bytes are escaped (not %c, which can match UTF-8 bytes in some locales).
  local escaped = s:gsub('[%z\1-\31\127"\\]', function(c)
    return ESCAPES[c] or string.format("\\u%04x", c:byte())
  end)
  return '"' .. escaped .. '"'
end

local encode
encode = function(v)
  local t = type(v)
  if v == nil then
    return "null"
  elseif t == "boolean" then
    return tostring(v)
  elseif t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then
      return "null"
    end
    if v % 1 == 0 and math.abs(v) < 2 ^ 53 then
      return string.format("%d", v)
    end
    return string.format("%.17g", v)
  elseif t == "string" then
    return json_string(v)
  elseif t == "table" then
    local array, n = is_array(v)
    local parts = {}
    if array then
      for i = 1, n do
        parts[i] = encode(v[i])
      end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    -- Keys keep their types (Resolve uses number keys, e.g. markers by frame).
    for k, value in pairs(v) do
      parts[#parts + 1] = "[" .. encode(k) .. "," .. encode(value) .. "]"
    end
    return '{"$d":[' .. table.concat(parts, ",") .. "]}"
  end
  return '{"$o":' .. register(v) .. "}"
end

local function decode(v)
  if v == DAVA_NIL then
    return nil
  end
  if type(v) ~= "table" then
    return v
  end
  if v["$o"] ~= nil then
    local object = objects[v["$o"]]
    if object == nil then
      error("unknown object id " .. tostring(v["$o"]), 0)
    end
    return object
  end
  local out = {}
  for k, value in pairs(v) do
    out[k] = decode(value)
  end
  return out
end

local function target_of(req)
  local target = objects[req.target]
  if target == nil then
    error("unknown object id " .. tostring(req.target), 0)
  end
  return target
end

local function public(name)
  return type(name) == "string" and name:sub(1, 1) ~= "_"
end

local function handle(req)
  local op = req.op
  if op == "hello" then
    return { protocol = 1, lua = _VERSION }
  elseif op == "call" then
    local target = target_of(req)
    if not public(req.method) then
      error("method " .. tostring(req.method) .. " is not callable through the bridge", 0)
    end
    local method = target[req.method]
    if method == nil then
      error("AttributeError: no method " .. req.method, 0)
    end
    local n = req.n or 0
    local args = {}
    for i = 1, n do
      args[i] = decode(req.args[i])
    end
    return method(target, unpack(args, 1, n))
  elseif op == "getattr" then
    if not public(req.name) then
      error("attribute " .. tostring(req.name) .. " is not readable through the bridge", 0)
    end
    return target_of(req)[req.name]
  elseif op == "dir" then
    local names = {}
    local meta = getmetatable(target_of(req))
    if meta and type(meta.__index) == "table" then
      for k in pairs(meta.__index) do
        if public(k) then
          names[#names + 1] = k
        end
      end
    end
    table.sort(names)
    return names
  elseif op == "release" then
    for _, id in ipairs(req.ids or {}) do
      if id ~= 0 then
        objects[id] = nil
      end
    end
    return nil
  end
  error("unknown op " .. tostring(op), 0)
end

local function emit(seq, body)
  local total = math.max(1, math.ceil(#body / CHUNK))
  for part = 1, total do
    local chunk = body:sub((part - 1) * CHUNK + 1, part * CHUNK)
    local message = "DAVA-RESP " .. seq .. " " .. part .. "/" .. total .. " " .. chunk
    fusion:Execute("error(" .. string.format("%q", message) .. ", 0)")
  end
end

local last = nil
print("dava bridge: running (stop it with 'dava bridge stop')")
while not bmd.fileexists(STOP) do
  if bmd.fileexists(REQUEST) then
    local ok, req = pcall(dofile, REQUEST)
    if ok and type(req) == "table" and req.seq ~= last then
      last = req.seq
      local handled, result = pcall(handle, req)
      local body
      if handled then
        local encoded, payload = pcall(encode, result)
        if encoded then
          body = '{"seq":' .. req.seq .. ',"result":' .. payload .. "}"
        else
          body = '{"seq":' .. req.seq .. ',"error":' .. json_string("encode failed: " .. tostring(payload)) .. "}"
        end
      else
        body = '{"seq":' .. req.seq .. ',"error":' .. json_string(tostring(result)) .. "}"
      end
      emit(req.seq, body)
    end
  end
  bmd.wait(POLL)
end
print("dava bridge: stopped")
