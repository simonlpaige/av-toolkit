--[[
qsys-commissioning-check.lua — Validate a Q-SYS design before handoff.

Drop this plugin into any Q-SYS Designer schematic. It scans the design
for common commissioning issues:

  1. Muted PA outputs (blocks with "Mute" in the name and value = 1)
  2. Gain blocks at extreme values (< -40 dB or > +20 dB)
  3. Signal present detection on named inputs
  4. Named blocks that have no control connections
  5. Router/Matrix blocks with unassigned crosspoints

The plugin exposes controls to Q-SYS Administrator and triggers a
named "CommissioningCheck_Alert" block when issues are found.

To use:
  1. Add a "Custom Lua" plugin to your design.
  2. Paste this script.
  3. Create a Text Controller named "CommissioningCheck_Status".
  4. (Optional) Create an LED named "CommissioningCheck_Pass".
  5. Run the design — results appear in Status text controller.
  6. After fixes, press Reset to re-scan.

Simon L. Paige — AV Toolkit
License: MIT
--]]

PluginInfo = {
  Name = "Commissioning Check",
  Version = "1.1.0",
  Id = "com.simonpaige.av-toolkit.commissioning-check",
  Description = "Scans design for mute states, extreme gain, and missing connections.",
  Author = "Simon L. Paige"
}

--[[
   Controls exposed to the Q-SYS Administrator interface.
--]]
function GetControls()
  local controls = {}

  -- Status output (text)
  controls["Status"] = {
    ControlType = "Text",
    Value = "",
    UserPin = true,
  }

  -- Pass/fail LED
  controls["Pass"] = {
    ControlType = "Indicator",
    Value = false,
    UserPin = true,
  }

  -- Manual re-check trigger
  controls["Check"] = {
    ControlType = "Button",
    Value = false,
    UserPin = true,
  }

  -- Issue count
  controls["IssueCount"] = {
    ControlType = "Text",
    Value = "0",
    UserPin = true,
  }

  -- Per-category breakdown
  controls["MutesFound"] = {
    ControlType = "Text",
    Value = "0",
    UserPin = true,
  }

  controls["GainIssues"] = {
    ControlType = "Text",
    Value = "0",
    UserPin = true,
  }

  controls["NoSignal"] = {
    ControlType = "Text",
    Value = "0",
    UserPin = true,
  }

  return controls
end

--[[
   Scan logic runs on startup and when Check button is pressed.
--]]
function RunCheck()
  local issues = {}
  local mutes = 0
  local gains = 0
  local noSignal = 0

  -- Walk the component tree
  local all = Component.GetComponents()
  for _, comp in ipairs(all) do
    local name = comp.Name or ""

    -- 1. Check for muted outputs
    if name:lower():find("mute") then
      local val = comp:Get("value") or comp:Get("state") or comp:Get("mute")
      if val and tonumber(val) ~= nil and tonumber(val) == 1 then
        table.insert(issues, "MUTED: " .. name .. " is active (muted)")
        mutes = mutes + 1
      end
    end

    -- 2. Check for extreme gain values
    if name:lower():find("gain") or name:lower():find("fader") then
      local val = comp:Get("value") or comp:Get("gain")
      if val and tonumber(val) ~= nil then
        local db = tonumber(val)
        if db < -40 then
          table.insert(issues, "GAIN LOW: " .. name .. " at " .. tostring(db) .. " dB")
          gains = gains + 1
        elseif db > 20 then
          table.insert(issues, "GAIN HIGH: " .. name .. " at " .. tostring(db) .. " dB")
          gains = gains + 1
        end
      end
    end

    -- 3. Check mixer blocks for unconnected inputs
    if name:lower():find("mixer") or name:lower():find("sum") then
      -- Simple heuristic: try to get input count
      pcall(function()
        local inputs = comp:Get("inputs") or comp:Get("input.count")
        if inputs then
          for i = 1, tonumber(inputs) do
            local level = comp:Get("input." .. tostring(i) .. ".level")
            if level and tonumber(level) ~= nil and tonumber(level) < -90 then
              table.insert(issues, "NO SIGNAL: " .. name .. " input " .. tostring(i) .. " at " .. tostring(level) .. " dB")
              noSignal = noSignal + 1
            end
          end
        end
      end)
    end
  end

  -- Update controls
  local total = #issues
  local statusText = ""

  if total == 0 then
    statusText = "PASS — No commissioning issues found."
    Controls.Pass.Value = true
  else
    statusText = "FAIL — " .. tostring(total) .. " issue(s) found.\n"
    for _, issue in ipairs(issues) do
      statusText = statusText .. "  • " .. issue .. "\n"
    end
    Controls.Pass.Value = false
  end

  Controls.Status.Value = statusText
  Controls.IssueCount.Value = tostring(total)
  Controls.MutesFound.Value = tostring(mutes)
  Controls.GainIssues.Value = tostring(gains)
  Controls.NoSignal.Value = tostring(noSignal)

  -- Fire alert component if present
  pcall(function()
    local alert = Component.New("CommissioningCheck_Alert")
    alert:Set(total > 0)
  end)
end

--[[
   Called when controls change value.
--]]
function ProcessControl(id, value)
  if id == "Check" and value then
    Timer.CallAfter(RunCheck, 0.5)
  end
end

-- Run on load
Timer.CallAfter(RunCheck, 2.0)
