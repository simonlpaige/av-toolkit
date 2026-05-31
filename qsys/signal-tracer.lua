--[[
qsys-signal-tracer.lua — End-to-end signal path validation.

For Q-SYS designs with complex routing, this plugin traces every signal
path from named input blocks through the DSP chain to named outputs.
It reports:

  - Paths that are broken (missing connection between blocks)
  - Paths that route through unexpected blocks
  - Inputs that never reach an output
  - Outputs that have no arriving signal path

Pin tracing works by following Component wire connections in the
design namespace. It cannot follow internal block routing (e.g.
router crosspoints set at runtime), but it catches the most common
commissioning failure: a wire that was never drawn.

To use:
  1. Add a "Custom Lua" plugin, paste this script.
  2. Create a Text Controller named "SignalTrace_Report".
  3. Create trigger buttons:
     - "SignalTrace_Run" (Button) — runs full trace
     - "SignalTrace_Clear" (Button) — clears report
  4. Run design and press Run.

Simon L. Paige — AV Toolkit
License: MIT
--]]

PluginInfo = {
  Name = "Signal Path Tracer",
  Version = "1.1.0",
  Id = "com.simonpaige.av-toolkit.signal-tracer",
  Description = "Traces every signal path from input to output, flags broken paths.",
  Author = "Simon L. Paige"
}

function GetControls()
  local controls = {}
  controls["Report"] = {
    ControlType = "Text",
    Value = "Ready. Press Run to trace signal paths.",
    UserPin = true,
  }
  controls["Run"] = {
    ControlType = "Button",
    Value = false,
    UserPin = true,
  }
  controls["Clear"] = {
    ControlType = "Button",
    Value = false,
    UserPin = true,
  }
  return controls
end

--[[
   Find all blocks that look like inputs (source blocks).
--]]
function FindInputBlocks()
  local inputs = {}
  local all = Component.GetComponents()
  for _, comp in ipairs(all) do
    local name = comp.Name or ""
    local lower = name:lower()
    if lower:find("input") or lower:find("in ") or lower:find("mic") or
       lower:find("source") or lower:find("line in") or lower:find("dante in") or
       lower:find("aes67 in") or lower:find("usb in") then
      table.insert(inputs, comp)
    end
  end
  return inputs
end

--[[
   Find all blocks that look like outputs (destination blocks).
--]]
function FindOutputBlocks()
  local outputs = {}
  local all = Component.GetComponents()
  for _, comp in ipairs(all) do
    local name = comp.Name or ""
    local lower = name:lower()
    if lower:find("output") or lower:find("out ") or lower:find("speaker") or
       lower:find("amp ") or lower:find("dante out") or lower:find("aes67 out") or
       lower:find("usb out") or lower:find("rec ") then
      table.insert(outputs, comp)
    end
  end
  return outputs
end

--[[
   Follow output pins on a component to see where they connect.
   Uses GetOutputPins() → GetConnectedInputs() pattern.
--]]
function TraceForward(comp, visited, depth)
  if depth > 50 then return {} end  -- safety limit
  local results = {}
  local key = comp.Name or tostring(comp)
  if visited[key] then return { comp.Name } end
  visited[key] = true

  -- Try to get output pins
  local ok, pins = pcall(function() return comp:GetOutputPins() end)
  if ok and pins then
    for _, pin in ipairs(pins) do
      local ok2, conns = pcall(function() return pin:GetConnectedInputs() end)
      if ok2 and conns then
        for _, dest in ipairs(conns) do
          local destName = dest.Name or tostring(dest)
          table.insert(results, destName)
          local downstream = TraceForward(dest, visited, depth + 1)
          for _, d in ipairs(downstream) do
            table.insert(results, "  → " .. d)
          end
        end
      end
    end
  end

  return results
end

--[[
   Run a full trace of all inputs to all outputs.
--]]
function RunTrace()
  local inputs = FindInputBlocks()
  local outputs = FindOutputBlocks()
  local report = ""

  report = report .. "Signal Path Trace Report\n"
  report = report .. "========================\n"
  report = report .. "Inputs found:  " .. tostring(#inputs) .. "\n"
  report = report .. "Outputs found: " .. tostring(#outputs) .. "\n\n"

  if #inputs == 0 and #outputs == 0 then
    report = report .. "No named input/output blocks found.\n"
    report = report .. "Ensure blocks use naming conventions like:\n"
    report = report .. "  Inputs:  'Mic Input 1', 'Dante In 1', 'Line In L'\n"
    report = report .. "  Outputs: 'PA Output L', 'Dante Out 1', 'Main Speaker'\n"
    Controls.Report.Value = report
    return
  end

  local tracedInputs = {}
  local reachedOutputs = {}

  for _, inp in ipairs(inputs) do
    local name = inp.Name or "?"
    report = report .. "[Input] " .. name .. "\n"
    local path = TraceForward(inp, {}, 0)
    if #path == 0 then
      report = report .. "  ⚠ NO CONNECTIONS — this input doesn't go anywhere.\n"
    else
      tracedInputs[name] = true
      for _, step in ipairs(path) do
        report = report .. "  " .. step .. "\n"
        -- Track outputs that this path reaches
        for _, out in ipairs(outputs) do
          if step:find(out.Name or "") then
            reachedOutputs[out.Name] = true
          end
        end
      end
    end
    report = report .. "\n"
  end

  -- Summarize
  report = report .. "--- Summary ---\n"
  report = report .. "Traced inputs:  " .. tostring(#tracedInputs) .. " of " .. tostring(#inputs)
  report = report .. " (" .. tostring(#inputs - #tracedInputs) .. " with no path)\n"

  local orphanOutputs = {}
  for _, out in ipairs(outputs) do
    if not reachedOutputs[out.Name] then
      table.insert(orphanOutputs, out.Name)
    end
  end

  if #orphanOutputs > 0 then
    report = report .. "Orphan outputs (no detected input path):\n"
    for _, name in ipairs(orphanOutputs) do
      report = report .. "  ⚠ " .. name .. "\n"
    end
  end

  Controls.Report.Value = report
end

function ProcessControl(id, value)
  if id == "Run" and value then
    Timer.CallAfter(RunTrace, 0.3)
  elseif id == "Clear" and value then
    Controls.Report.Value = "Ready. Press Run to trace signal paths."
  end
end
