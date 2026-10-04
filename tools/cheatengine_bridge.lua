--  Cheat Engine -> Bonsai bridge
--
--  Drop this in Cheat Engine's autorun folder. It exposes the tools that the
--  bundled AITools extension already registers - Dark Byte's own curated set of
--  35 - to any client on this machine, over a Windows named pipe.
--
--  Why a named pipe and not a TCP port: CE 7.7 documents createServerSocket in
--  celua.txt but does not compile it - the symbol is absent from every binary in
--  the install - so CE can connect out to a TCP server but cannot listen on one.
--  createPipe is present and is what CE's own DotNet and mono bridges use
--  (autorun\DotNetInterface.lua, autorun\monoscript.lua). Python opens
--  \\.\pipe\<name> with no dependency at all.
--
--  Why this needs a restart: CE has no command line flag to run a script. A file
--  in autorun\ is executed when CE starts, and that is the only foothold. There
--  is no way to reach an already-running CE that has not been made to cooperate.
--
--  Two things this does better than the stock extension:
--
--    * Handlers are wrapped in pcall. aibase.lua:560 calls functionToCall bare,
--      so a Lua error inside a tool kills the turn and the model never gets a
--      functionResponse at all. Here the error comes back as a value.
--    * Dispatch is a real function. The stock extension inlines it about five
--      levels deep inside a streaming loop, coupled to UI objects.
--
--  Security, plainly: a connected client can read and write the memory of any
--  process this user can open, which is arbitrary code injection into anything
--  you are running. That is what Cheat Engine is for, but it does mean anything
--  else running as you can do it too. The pipe name is not a secret and the
--  token is only a guard against accidents, not an attack - a local process can
--  read the token file. It exists so that a stray connection cannot drive CE by
--  accident, not to make this safe against a determined local process.

local PIPE_NAME = 'bonsai_ce_bridge'
-- The token goes in the temp folder, not beside the executable. Cheat Engine
-- usually runs unelevated and C:\Program Files is not writable without
-- elevation, so a file there would fail with access denied and the bridge would
-- never start - which looks exactly like "Cheat Engine cannot be driven".
-- getTempFolder() is CE's own accessor for that path.
local TOKEN_FILE = getTempFolder()
if TOKEN_FILE:sub(-1) ~= '\\' and TOKEN_FILE:sub(-1) ~= '/' then
  TOKEN_FILE = TOKEN_FILE .. '\\'
end
TOKEN_FILE = TOKEN_FILE .. 'bonsai_ce_token.txt'

-- Declared here, assigned at the bottom. serve() closes over this local, so
-- declaring it after serve would silently make it a nil global instead.
local TOKEN = nil

local jsonparser = require('json')

local function log(msg)
  print('[bonsai] ' .. tostring(msg))
end

-- A token written where the client can find it, so that a connection from a
-- previous Cheat Engine session is not silently accepted by the next one.
--
-- Deliberately trivial. It guards against an accident, not against anyone
-- attacking: a local process can read this file, which the note at the top of
-- this file says plainly. The first version seeded math.random from the address
-- of registerAITool, which meant taking the address of a CELua function to build
-- a string - a compile-time hazard for a value that never needed to be
-- unpredictable. os.time() changes every second and is used all over CE's own
-- scripts, so it is the safest thing available.
local function makeToken()
  return 'ce-' .. tostring(os.time())
end

local function writeToken(token)
  local fh = io.open(TOKEN_FILE, 'w')
  if not fh then
    log('could not write the token file: ' .. tostring(TOKEN_FILE))
    return false
  end
  fh:write(token)
  fh:close()
  return true
end

-- Turn a Lua value into something jsonparser.encode will accept. Tool results
-- come back as arbitrary tables from CE, and a table with mixed key types or a
-- function in it is not encodable - which would otherwise turn a successful tool
-- call into a broken response.
local function sanitise(v, depth)
  depth = depth or 0
  local t = type(v)
  if v == nil then return nil end
  if t == 'string' or t == 'number' or t == 'boolean' then return v end
  if t == 'function' or t == 'userdata' or t == 'thread' then
    return '<a ' .. t .. '>'
  end
  if depth > 6 then return '<too deep>' end
  if t == 'table' then
    local out, isArray = {}, true
    local n = 0
    for k, val in pairs(v) do
      if type(k) ~= 'number' then isArray = false end
      local s = sanitise(val, depth + 1)
      if s ~= nil then out[k] = s end
      if type(k) == 'number' then n = n + 1 end
    end
    if isArray and n == #v then return out end
    -- a mixed or sparse table becomes a string rather than a malformed object
    if not isArray then
      local ok, encoded = pcall(jsonparser.encode, v)
      if ok then return encoded end
      return '<table>'
    end
    return out
  end
  return nil
end

-- The one function the stock extension does not have: run a registered tool by
-- name, safely, and always come back with something.
local function runTool(name, args)
  local tool = aitools and aitools[name]
  if not tool then
    return { ok = false, error = "no such tool: " .. tostring(name) }
  end
  if tool.enabled == false then
    return { ok = false, error = "tool is disabled: " .. tostring(name) }
  end
  if not tool.functionToCall then
    return { ok = false, error = "tool has no handler: " .. tostring(name) }
  end
  args = args or {}
  local res = { pcall(tool.functionToCall, args) }
  if not res[1] then
    -- This is the case the stock extension loses entirely.
    return { ok = false, error = 'tool raised: ' .. tostring(res[2]) }
  end
  local value = sanitise(res[2])
  if value == nil then
    return { ok = true, result = 'done (no value returned)' }
  end
  return { ok = true, result = value }
end

local function listTools()
  local out = {}
  if not aitools then return out end
  local names = {}
  for name in pairs(aitools) do names[#names + 1] = name end
  table.sort(names)
  for _, name in ipairs(names) do
    local t = aitools[name]
    if t.enabled ~= false then
      out[#out + 1] = {
        name = t.name or name,
        description = t.description or '',
        -- the extension already builds this in the OpenAI function shape, so
        -- the schema can be passed straight through
        parameters = t.parameters or { type = 'OBJECT', properties = {}, required = {} },
      }
    end
  end
  return out
end

-- Length-prefixed framing. readString's delimiter behaviour is not documented,
-- so the size is sent explicitly and the payload is read as bytes. A named pipe
-- is a byte stream and not a message stream, so this is the only framing that
-- cannot be ambiguous.
local function readExactly(pipe, n)
  local chunks, got = {}, 0
  while got < n do
    local chunk = pipe:readBytes(n - got)
    if not chunk or #chunk == 0 then return nil end
    chunks[#chunks + 1] = chunk
    got = got + #chunk
  end
  return table.concat(chunks)
end

local function readFrame(pipe)
  local raw = readExactly(pipe, 4)
  if not raw or #raw < 4 then return nil end
  local n = 0
  for i = 1, 4 do
    n = n + raw:byte(i) * (256 ^ (i - 1))
  end
  if n <= 0 or n > 8 * 1024 * 1024 then return nil end
  return readExactly(pipe, n)
end

local function writeFrame(pipe, text)
  local payload = text or ''
  local n = #payload
  local head = string.char(n % 256, math.floor(n / 256) % 256,
                           math.floor(n / 65536) % 256,
                           math.floor(n / 16777216) % 256)
  pipe:writeBytes(head .. payload)
end

local function send(pipe, tbl)
  local ok, encoded = pcall(jsonparser.encode, tbl)
  if not ok then
    ok, encoded = pcall(jsonparser.encode,
                        { ok = false, error = 'response could not be encoded' })
  end
  writeFrame(pipe, encoded)
end

local function serve(client)
  -- No lock() here on purpose. CELua rejects `obj.method and obj.method()` as a
  -- syntax error - a method reference is not a value in CELua, which is what
  -- stopped this script loading at all - and there is nothing to contend with
  -- anyway: one connection is accepted at a time and the client takes a new
  -- connection per request.
  local greeted = false
  while true do
    local frame = readFrame(client)
    if not frame then break end
    local ok, req = pcall(jsonparser.decode, frame)
    if not ok or type(req) ~= 'table' then
      send(client, { ok = false, error = 'malformed request' })
    else
      if not greeted then
        greeted = true
        if req.token ~= TOKEN then
          send(client, { ok = false, error = 'bad token' })
          break
        end
      end
      local op = req.op or 'tools/list'
      if op == 'ping' then
        send(client, { ok = true, result = 'bonsai ce bridge',
                       tools = #listTools() })
      elseif op == 'tools/list' then
        send(client, { ok = true, tools = listTools() })
      elseif op == 'tools/call' then
        send(client, runTool(req.name, req.arguments))
      else
        send(client, { ok = false, error = 'unknown op: ' .. tostring(op) })
      end
    end
  end
end

local TOKEN_ACTUAL = makeToken()
if not writeToken(TOKEN_ACTUAL) then
  log('no token file, not starting the bridge')
  return
end
TOKEN = TOKEN_ACTUAL

local pipe = createPipe(PIPE_NAME, 1024 * 1024, 1024 * 1024, 4)
if not pipe or not pipe.valid then
  log('createPipe failed; Cheat Engine cannot be driven from outside')
  return
end
log('listening on \\\\.\\pipe\\' .. PIPE_NAME .. ' with ' ..
    #listTools() .. ' tool(s)')

createThread(function()
  while true do
    -- acceptConnection(true) is required: without split it does not hand back a
    -- LuaPipe to talk on, and every read after that would be on the wrong object.
    local client = pipe:acceptConnection(true)
    if client then
      local ok, err = pcall(serve, client)
      if not ok then log('client error: ' .. tostring(err)) end
      -- LuaPipe inherits Object, which has destroy(). There is no close() on it.
      pcall(function() client:destroy() end)
    end
    sleep(120)
  end
end)
