"""
Run this first: `python probe.py`

It scans for a Logitech receiver, tries device indices 1-6, and reports
which index answers with battery info. Copy the printed device_index into
config.json so tray_app.py doesn't have to re-probe every time (probing on
every launch also works, but is slower and occasionally flaky).
"""

import sys
import json

import hidpp


def main():
    receivers = hidpp.find_receiver_interfaces()
    if not receivers:
        print("No Logitech HID++ receiver found.")
        print("- Make sure the Unifying/Bolt dongle is plugged in.")
        print("- On some systems another process (e.g. a driver) can hold")
        print("  the interface open; try unplugging/replugging the dongle.")
        sys.exit(1)

    print(f"Found {len(receivers)} Logitech receiver interface(s):\n")
    for r in receivers:
        name = hidpp.RECEIVER_VID_PID_HINTS.get(r["product_id"], "Unknown receiver")
        print(f"  VID=0x{r['vendor_id']:04X} PID=0x{r['product_id']:04X} "
              f"({name}) product_string={r['product_string']!r}")
        print(f"    short_path present: {bool(r['short_path'])}, "
              f"long_path present: {bool(r['long_path'])}")

    found_any = False
    for r in receivers:
        if not r["short_path"]:
            continue  # battery calls in this script use the short interface
        print(f"\nProbing receiver PID=0x{r['product_id']:04X} ...")
        dev = hidpp.HidppDevice(short_path=r["short_path"], long_path=r["long_path"])
        try:
            for device_index in range(1, 7):
                try:
                    battery = dev.read_battery(device_index)
                except hidpp.HidppError:
                    battery = None
                if battery:
                    found_any = True
                    print(f"  Device index {device_index}: BATTERY FOUND -> {battery}")
                    print("\n  Suggested config.json values:")
                    print(json.dumps({
                        "vendor_id": r["vendor_id"],
                        "product_id": r["product_id"],
                        "device_index": device_index,
                    }, indent=2))
                else:
                    print(f"  Device index {device_index}: no response / no battery feature")
        finally:
            dev.close()

    if found_any:
        print("\nNote: if every device index above reported the identical")
        print("battery reading, that's expected for a single-device receiver")
        print("(like the PRO X 2 Lightspeed receiver) -- it only has one")
        print("device paired, so it answers the same way regardless of which")
        print("index you ask. Device index 1 is a safe default to use.")

    if not found_any:
        print("\nNo device on this receiver reported battery info.")
        print("Possible causes:")
        print("  - The mouse is asleep -- move it or click a button, then re-run.")
        print("  - It uses a battery feature this script doesn't try yet.")
        print("  - It's actually connected some other way (check Bluetooth).")


if __name__ == "__main__":
    main()
