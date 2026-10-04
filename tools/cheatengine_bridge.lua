--  Cheat Engine -> Bonsai bridge
--
--  Drop this in Cheat Engine's autorun folder. It exposes the tools that the
--  bundled AITools extension already registers - Dark Byte's own curated set of
--  35 - so an assistant can use them.
--
--  Why files and not a pipe
--  ------------------------
--  The obvious transport is createPipe, and it does not work from another
--  process. Measured, not assumed:
--
--    * the connection completes and Cheat Engine reports Connected=true
--    * a client WriteFile of 48 bytes returns success
--    * readString and readBytes both then return nil, nine attempts over a
--      second, while Connected is still true
--    * giving it eight idle seconds changes nothing
--    * and it is not one-directional either - a frame the bridge writes on
--      connect never arrives at the client
--
--  Both directions are dead while both ends report success, so the pipe CE
--  creates is not a duplex channel to an outside process in this build. Cheat
--  Engine ships luaclient.dll to talk to its own pipes, which suggests the pipe
--  expects that handshake rather than a plain client - but there is no way to
--  inspect the pipe from here, and this works:
--
--  A folder, and a timer. The bridge polls it a few times a second; a request
--  file appears, it runs the tool and writes the answer back next to it. Slower
--  than a pipe by a few hundred milliseconds, and completely legible: the
--  requests and replies are files you can open and read.
--
--  Why this needs a restart
--  -----------------------
--  Cheat Engine has no command line flag to run a script. A file in autorun\ is
--  executed when CE starts, and that is the only way in.
--
--  Two things this does better than the stock AITools extension:
--
--    * Handlers are wrapped in pcall. aibase.lua:560 calls functionToCall bare,
--      and there is no pcall anywhere in aitools.lua, so a Lua error inside a
--      tool - and these tools index CE objects that are nil whenever the wrong
--      process is open - kills the turn and the model never gets a
--      functionResponse at all. Here the error comes back as a value.
--    * Dispatch is a function. The stock version inlines it about five levels
--      deep inside a streaming loop, coupled to UI objects.
--
--  Security, plainly: a connected client can read and write the memory of any
--  process this user can open, which is arbitrary code injection into anything
--  you are running. That is what Cheat Engine is for, but with a file mailbox it
--  also means any other program running as you can drop a request in this folder
--  and drive Cheat Engine. The token guards against accidents, not attacks: it is
--  written next to the folder in your temp directory and any local process can
--  read it. It is there so a stray file cannot drive CE, not to make this safe
--  against a determined local program - and it is only checked once a request is
--  read, because anything can already write the status file.

local BASE = getTempFolder()
if BASE:sub(-1) ~= '\\' and BASE:sub(-1) ~= '/' then
  BASE = BASE .. '\\'
end
-- Flat files in the temp folder, not a subfolder of it. io.open will not create
-- a directory, and createDir(path) did not produce one here either - the folder
-- simply did not exist afterwards, so every write below failed while the bridge
-- still logged that it was watching. The temp folder is the one place that is
-- certain to exist and to be writable without elevation, and a handful of
-- clearly-prefixed files in it needs no directory to be made.
local PREFIX = BASE .. 'bonsai_ce_'
local STATUS_FILE = PREFIX .. 'status.json'
local REQ_FILE = PREFIX .. 'request.json'
local LOG_FILE = PREFIX .. 'log'
local POLL_MS = 250

-- The token. Deliberately trivial: it guards against a stray request, not
-- against anyone attacking, since a local process can read this file - which
-- the note above says plainly. os.time() changes every second and is used all
-- over Cheat Engine's own scripts, so it is the safest thing available.
local TOKEN = 'ce-' .. tostring(os.time())

local jsonparser = require('json')

-- Log to Cheat Engine's output and to a file, because print() goes to the Lua
-- Engine pane, which nobody has open, so everything this script had to say was
-- invisible to the one person who needed it.
local LOG_FILE = PREFIX .. 'log'

local function log(msg)
  local line = '[bonsai] ' .. tostring(msg)
  print(line)
  local fh = io.open(LOG_FILE, 'a')
  if fh then
    fh:write(line .. '\n')
    fh:close()
  end
end

local function writeFile(path, text)
  local fh = io.open(path, 'w')
  if not fh then return false end
  fh:write(text)
  fh:close()
  return true
end

local function readFile(path)
  local fh = io.open(path, 'rb')
  if not fh then return nil end
  local body = fh:read('*a')
  fh:close()
  if body == nil or #body == 0 then return nil end
  return body
end

-- Make sure the AITools extension has registered its tools.
--
-- This script sits in autorun\ and Extensions are not guaranteed to have loaded
-- by the time an autorun script runs, so asking beats trusting the ordering.
-- loadLuaScriptsFromPath honours the extension's own loadOrder.txt, so
-- aibase.lua still goes in before the tools that call registerAITool, and
-- registering the same names twice is harmless - they are table keys.
local AITOOLS_TRIED = false

local function ensureAitools()
  if aitools and next(aitools) ~= nil then return true end
  if AITOOLS_TRIED then return false end
  AITOOLS_TRIED = true
  local dir = getCheatEngineDir()
  if not dir or not loadLuaScriptsFromPath then return false end
  local ok, err = pcall(loadLuaScriptsFromPath, dir .. '\\Extensions\\AITools', false)
  if not ok then log('could not load AITools: ' .. tostring(err)) end
  return (aitools and next(aitools) ~= nil) or false
end

-- Turn a Lua value into something jsonparser.encode will accept. Tool results
-- are arbitrary tables from CE, and one containing a function or a mixed-key
-- table is not encodable - which would turn a successful call into a broken
-- response.
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
    local out, isArray, n = {}, true, 0
    for k, val in pairs(v) do
      if type(k) ~= 'number' then isArray = false end
      local s = sanitise(val, depth + 1)
      if s ~= nil then out[k] = s end
      if type(k) == 'number' then n = n + 1 end
    end
    if isArray and n == #v then return out end
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
    return { ok = false, error = 'no such tool: ' .. tostring(name) }
  end
  if tool.enabled == false then
    return { ok = false, error = 'tool is disabled: ' .. tostring(name) }
  end
  if not tool.functionToCall then
    return { ok = false, error = 'tool has no handler: ' .. tostring(name) }
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
  ensureAitools()
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
        -- already the OpenAI function shape, so the schema passes straight through
        parameters = t.parameters or { type = 'OBJECT', properties = {}, required = {} },
      }
    end
  end
  return out
end

local function encode(tbl)
  local ok, text = pcall(jsonparser.encode, tbl)
  if ok then return text end
  return nil
end

-- One request, one reply. The id is echoed so a reply can never be mistaken for
-- an answer to something else, and so a client that gave up does not collect a
-- stale answer later.
LAST_ID = nil

local function handle(req)
  if type(req) ~= 'table' then
    return { ok = false, error = 'request was not a JSON object' }
  end
  if req.token ~= TOKEN then
    return { ok = false, error = 'bad token - this reply is from a different ' ..
                               'Cheat Engine session' }
  end
  local op = req.op or 'tools/list'
  if op == 'ping' then
    local tools = listTools()
    return { ok = true, result = 'bonsai ce bridge', tools = #tools }
  elseif op == 'tools/list' then
    local tools = listTools()
    local reply = { ok = true, tools = tools }
    if #tools == 0 then
      -- Empty is a real, fixable state and "0 tools" sends the user hunting.
      reply.note = 'Cheat Engine is running and the bridge is up, but the ' ..
        'AITools extension has registered no tools. Check Options > Settings ' ..
        '> AI Tools > "EnableAITools", restart Cheat Engine after changing it, ' ..
        'and check that Extensions\\AITools\\aitools.lua is present.'
    end
    return reply
  elseif op == 'tools/call' then
    return runTool(req.name, req.arguments)
  end
  return { ok = false, error = 'unknown op: ' .. tostring(op) }
end

local function poll()
  local body = readFile(REQ_FILE)
  if not body then return end
  local ok, req = pcall(jsonparser.decode, body)
  if not ok or type(req) ~= 'table' then
    writeFile(REQ_FILE, '')          -- drop it, so it is not retried forever
    return
  end
  local id = req.id
  if id == nil or id == LAST_ID then return end   -- already answered
  LAST_ID = id
  local reply = handle(req)
  reply.id = id
  local text = encode(reply)
  if text then
    writeFile(PREFIX .. 'reply-' .. tostring(id) .. '.json', text)
  else
    writeFile(PREFIX .. 'reply-' .. tostring(id) .. '.json',
              '{"ok":false,"error":"the reply could not be encoded"}')
  end
  writeFile(REQ_FILE, '')            -- consume it
end

-- Liveness, so the client can tell "Cheat Engine is not running" from "Cheat
-- Engine is running but the bridge is not". Rewritten on every tick.
local function writeStatus()
  local tools = listTools()
  writeFile(STATUS_FILE, encode({
    ok = true,
    pid = 0,
    token = TOKEN,
    tools = #tools,
    poll_ms = POLL_MS,
    folder = BASE,
    prefix = PREFIX,
  }) or '{"ok":false}')
end

log('starting; files are ' .. PREFIX .. '*')
writeStatus()
writeFile(REQ_FILE, '')
if ensureAitools() then
  log('AITools is loaded')
else
  log('AITools has not registered yet; it is loaded on demand')
end
log('watching for requests every ' .. tostring(POLL_MS) .. 'ms, ' ..
    #listTools() .. ' tool(s) available')

-- The timer has to be held in a GLOBAL. Holding it in a file-scope local was not
-- enough: the timer fired once or twice and then stopped for good, with status
-- going stale and every request after that sitting unconsumed and no error
-- anywhere. An autorun chunk's locals do not survive the chunk; a global does.
-- createTimer also returns an object, and an object with no reference at all is
-- simply collected - which looks identical from outside.
bonsai_ce_ticks = 0
bonsai_ce_timer = createTimer(POLL_MS, function()
  bonsai_ce_ticks = bonsai_ce_ticks + 1
  local ok, err = pcall(poll)
  if not ok then log('poll failed: ' .. tostring(err)) end
  -- Also protected: an unprotected throw inside a timer callback is one way for
  -- a timer to die quietly, and the tick count is reported so a stall is visible
  -- rather than inferred from a stale file.
  pcall(writeStatus)
  if bonsai_ce_ticks % 40 == 0 then
    log('alive: ' .. tostring(bonsai_ce_ticks) .. ' ticks, last id ' ..
        tostring(LAST_ID))
  end
end)
if not bonsai_ce_timer then
  log('createTimer returned nothing; the bridge cannot poll for requests')
  return
end
log('timer is running, held in the global bonsai_ce_timer')
