# Logi Mouse Battery Tray

Shows your Logitech wireless mouse's battery level in the Windows system
tray and sends a toast notification when it drops past 30% / 15% / 5%
(configurable).

It works by talking directly to your Unifying/Bolt USB receiver using the
HID++ protocol (the same technique the open-source **Solaar** project
uses on Linux) -- no Logitech software required.

## Important caveat, read first

There is no universal Windows API for "battery level of whatever's
plugged into this Logitech dongle." Different mice/receiver firmware
answer slightly different HID++ "features," so this project includes a
**probe script** to figure out the right settings for your specific mouse
before the tray app can read it reliably. Budget 5-10 minutes for that
one-time step.

## 1. Install Python dependencies

You need Python 3.10+ on Windows (from python.org -- check "Add to PATH"
during install).

```powershell
pip install -r requirements.txt
```

## 2. Find your mouse (one-time setup)

With your receiver plugged in and the mouse awake (click a button or move
it right before running this), run:

```powershell
python probe.py
```

It will list any Logitech receiver(s) found and try device indices 1-6
on each, printing which one answers with battery info, e.g.:

```
Device index 2: BATTERY FOUND -> {'feature': 4096, 'percent': 72, 'level': None, 'charging': False}

  Suggested config.json values:
{
  "vendor_id": 1133,
  "product_id": 50733,
  "device_index": 2
}
```

You normally don't need to copy these in by hand -- `tray_app.py` runs the
same discovery automatically on first launch and saves the result into
`config.json` for next time. Run `probe.py` yourself only if the tray app
can't find your mouse (see Troubleshooting).

### If probe.py finds nothing

- Unplug and replug the receiver.
- Move the mouse / click a button -- wireless mice often only answer HID
  requests for a few seconds after activity, then go quiet to save power.
- Confirm it's really on the USB receiver, not paired over Bluetooth
  (check Windows Settings > Bluetooth & devices).
- Some very old mice only speak HID++ 1.0, which uses a different battery
  query than what's implemented here. If your model is more than ~8 years
  old, tell me the exact model and I can add HID++ 1.0 support.

## 3. Run it

```powershell
python tray_app.py
```

A battery icon appears in the system tray (color-coded green/yellow/red,
with a little lightning bolt overlay while charging). Right-click it for
the exact percentage, a manual refresh, and quit. It polls every 15
minutes by default and notifies once each time you cross 30%, 15%, or 5%
(it won't spam you repeatedly at the same level, and re-arms itself once
you charge back up).

Edit `config.json` to change:
- `poll_interval_seconds` -- how often it checks (default 900 = 15 min)
- `notify_thresholds` -- e.g. `[50, 20, 10]`
- `notify_cooldown_hours` -- currently informational; the crossing logic
  already prevents repeat spam at a given level

## 4. Run it automatically at Windows startup

Easiest route once you're happy with it:

```powershell
pyinstaller --onefile --windowed --name "LogiBatteryTray" tray_app.py
```

This produces `dist\LogiBatteryTray.exe` (no console window, no Python
needed to run it). Then:

1. Press `Win+R`, type `shell:startup`, hit Enter.
2. Copy `LogiBatteryTray.exe` (or a shortcut to it) into that folder.
3. Also copy `config.json` next to the `.exe` in `dist\` so it can find
   your saved device settings.

It will now start silently with Windows and sit in the tray.

## Troubleshooting

**Icon shows a red X.** Either the receiver isn't found, or the mouse
didn't answer this poll. Right-click > "Re-run device discovery." If that
still fails, wake the mouse first, then retry.

**Percentage seems off / stuck.** Mice that only expose the older
`BATTERY_VOLTAGE` feature (0x1001) get an estimated percentage from
voltage using a generic Li-Po curve in `hidpp.py`'s `voltage_to_percent()`
-- it's a reasonable approximation for "getting low" alerts, not lab
accurate. If your exact model is known, I can tune that curve.

**Two mice / a keyboard on the same receiver.** `probe.py` will report
every device index that answers; if it finds more than one, pick the one
matching your mouse's actual charge level, and hardcode that
`device_index` in `config.json` so auto-discovery doesn't pick the wrong
one later.

## Files

- `hidpp.py` -- low-level HID++ 2.0 protocol (feature discovery, battery
  reading, hidapi enumeration)
- `probe.py` -- one-time diagnostic CLI
- `tray_app.py` -- the actual tray app (icon, polling, notifications)
- `config.json` -- your settings + saved device info
- `requirements.txt` -- pip dependencies
