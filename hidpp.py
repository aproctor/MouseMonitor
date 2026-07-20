"""
Minimal HID++ 2.0 client for Logitech Unifying / Bolt receivers.

This re-implements just enough of the protocol that tools like Solaar use
(https://github.com/pwr-Solaar/Solaar) to read a paired mouse's battery
level through the USB receiver. It does not use any Logitech software --
it talks to the receiver's raw HID interface directly via hidapi.

Protocol basics:
  - The receiver exposes (at least) two extra HID interfaces beyond the
    normal mouse/keyboard ones, both under vendor ID 0x046D, usage page
    0xFF00:
        usage 0x0001 -> "short" HID++ reports, 7 bytes, report ID 0x10
        usage 0x0002 -> "long"  HID++ reports, 20 bytes, report ID 0x11
  - Every paired device behind the receiver has a device index (1-6).
  - Features (battery, DPI, etc.) are looked up by a 16-bit feature ID via
    the always-present "ROOT" feature (index 0x00), which returns a
    per-device *feature index* used in all subsequent calls.
  - Battery info lives behind one of three possible features depending on
    the mouse's firmware age:
        0x1000 BATTERY_STATUS   (older, discrete levels + %)
        0x1001 BATTERY_VOLTAGE  (older gaming mice, voltage-based)
        0x1004 UNIFIED_BATTERY  (newer, % + status)
"""

import time
import hid

VENDOR_ID_LOGITECH = 0x046D

REPORT_ID_SHORT = 0x10
REPORT_ID_LONG = 0x11
REPORT_LEN_SHORT = 7   # includes report id byte
REPORT_LEN_LONG = 20   # includes report id byte

FEATURE_ROOT = 0x0000
FEATURE_BATTERY_STATUS = 0x1000
FEATURE_BATTERY_VOLTAGE = 0x1001
FEATURE_UNIFIED_BATTERY = 0x1004

BATTERY_FEATURES_TO_TRY = [
    FEATURE_UNIFIED_BATTERY,
    FEATURE_BATTERY_STATUS,
    FEATURE_BATTERY_VOLTAGE,
]

# Receiver-wide device index used for broadcast/short-lived requests.
DEVICE_INDEX_RECEIVER = 0xFF

RECEIVER_VID_PID_HINTS = {
    # Common Logitech receiver product IDs. Not exhaustive -- if yours
    # isn't listed, probe.py will still find it by scanning all Logitech
    # HID++ interfaces.
    0xC52B: "Unifying Receiver",
    0xC52F: "Unifying Receiver",
    0xC532: "Unifying Receiver",
    0xC534: "Unifying Receiver",
    0xC53A: "Lightspeed Receiver",
    0xC539: "Lightspeed Receiver",
    0xC53F: "Bolt Receiver",
    0xC548: "Bolt Receiver",
}


class HidppError(Exception):
    pass


class HidppDevice:
    """Wraps hidapi handles for a receiver's short + long HID++ interfaces."""

    def __init__(self, short_path=None, long_path=None):
        self.short_dev = None
        self.long_dev = None
        if short_path:
            self.short_dev = hid.device()
            self.short_dev.open_path(short_path)
            self.short_dev.set_nonblocking(True)
        if long_path:
            self.long_dev = hid.device()
            self.long_dev.open_path(long_path)
            self.long_dev.set_nonblocking(True)
        if not self.short_dev and not self.long_dev:
            raise HidppError("No HID++ interface handles provided")

    def close(self):
        if self.short_dev:
            self.short_dev.close()
        if self.long_dev:
            self.long_dev.close()

    # -- low level -----------------------------------------------------

    def _write_short(self, device_index, feature_index, function_id, sw_id, params=b""):
        if not self.short_dev:
            raise HidppError("Short HID++ interface not open")
        params = (params + b"\x00" * 3)[:3]
        payload = bytes([
            REPORT_ID_SHORT,
            device_index,
            feature_index,
            (function_id << 4) | (sw_id & 0x0F),
        ]) + params
        assert len(payload) == REPORT_LEN_SHORT
        self.short_dev.write(payload)

    def _read_any(self, timeout=1.0):
        """Poll both interfaces for a reply until timeout (seconds)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.short_dev:
                data = self.short_dev.read(REPORT_LEN_SHORT, timeout_ms=50)
                if data:
                    return bytes(data)
            if self.long_dev:
                data = self.long_dev.read(REPORT_LEN_LONG, timeout_ms=50)
                if data:
                    return bytes(data)
            time.sleep(0.01)
        return None

    def call(self, device_index, feature_index, function_id, params=b"", sw_id=0x1, timeout=1.0, retries=2):
        """Send a request and wait for the matching reply.

        Returns the parameter bytes of the reply (everything after the
        4-byte header), or raises HidppError on timeout / error reply.
        """
        last_exc = None
        for _ in range(retries + 1):
            try:
                self._write_short(device_index, feature_index, function_id, sw_id, params)
                start = time.time()
                while time.time() - start < timeout:
                    reply = self._read_any(timeout=timeout - (time.time() - start))
                    if reply is None:
                        break
                    # HID++ 1.0 error report: 10 <dev> 8F <feature> <func> <err>
                    if reply[0] == REPORT_ID_SHORT and reply[2] == 0x8F:
                        raise HidppError(f"HID++1.0 error reply: {reply.hex()}")
                    if len(reply) >= 4 and reply[1] == device_index and reply[2] == feature_index:
                        want = (function_id << 4) | (sw_id & 0x0F)
                        if reply[3] == want or reply[3] & 0xF0 == function_id << 4:
                            return reply[4:]
                    # else: unrelated notification, keep listening
                raise HidppError("Timed out waiting for HID++ reply")
            except HidppError as exc:
                last_exc = exc
                continue
        raise last_exc

    # -- feature discovery ----------------------------------------------

    def get_feature_index(self, device_index, feature_id):
        """Look up the per-device feature index for a 16-bit feature ID."""
        params = bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF, 0x00])
        reply = self.call(device_index, FEATURE_ROOT, function_id=0x0, params=params)
        feature_index = reply[0]
        if feature_index == 0:
            return None
        return feature_index

    # -- battery reading --------------------------------------------------

    def read_battery(self, device_index):
        """Try each known battery feature in turn. Returns a dict:
        {"feature": <feature id used>, "percent": int or None,
         "level": str or None, "charging": bool or None}
        or None if no battery feature responded.
        """
        for feature_id in BATTERY_FEATURES_TO_TRY:
            try:
                idx = self.get_feature_index(device_index, feature_id)
            except HidppError:
                continue
            if idx is None:
                continue
            try:
                if feature_id == FEATURE_UNIFIED_BATTERY:
                    # Function 0x0 is GET_CAPABILITIES (static info, NOT the
                    # battery reading). The actual reading is function 0x1,
                    # GET_STATUS -- this is the part that's easy to get wrong,
                    # since GET_CAPABILITIES still returns a plausible-looking
                    # byte in params[0] that is NOT a percentage.
                    reply = self.call(device_index, idx, function_id=0x1)
                    percent = reply[0]
                    charging_status = reply[2]
                    # 0=discharging, 1=charging, 2=charging (slow),
                    # 3=charging complete/full, 4=charging error
                    charging = charging_status in (1, 2)
                    full = charging_status == 3
                    return {"feature": feature_id, "percent": percent,
                            "level": None, "charging": charging, "full": full,
                            "charging_status_raw": charging_status}
                elif feature_id == FEATURE_BATTERY_STATUS:
                    reply = self.call(device_index, idx, function_id=0x0)
                    percent = reply[0]
                    status = reply[2]
                    charging = status in (1, 2)  # charging / recharging
                    return {"feature": feature_id, "percent": percent,
                            "level": None, "charging": charging}
                elif feature_id == FEATURE_BATTERY_VOLTAGE:
                    reply = self.call(device_index, idx, function_id=0x0)
                    millivolts = (reply[0] << 8) | reply[1]
                    percent = voltage_to_percent(millivolts)
                    charging = bool(reply[2] & 0x01) if len(reply) > 2 else None
                    return {"feature": feature_id, "percent": percent,
                            "level": None, "charging": charging,
                            "millivolts": millivolts}
            except HidppError:
                continue
        return None


def voltage_to_percent(millivolts):
    """Very rough Li-Po single-cell curve, used only as a fallback for
    mice that only expose BATTERY_VOLTAGE (0x1001). Good enough for
    "getting low" alerts; not lab-accurate.
    """
    curve = [
        (4200, 100), (4100, 95), (4000, 85), (3950, 75), (3900, 65),
        (3850, 55), (3800, 45), (3750, 35), (3700, 25), (3650, 15),
        (3600, 8), (3500, 3), (3300, 0),
    ]
    if millivolts >= curve[0][0]:
        return 100
    if millivolts <= curve[-1][0]:
        return 0
    for (v_hi, p_hi), (v_lo, p_lo) in zip(curve, curve[1:]):
        if v_lo <= millivolts <= v_hi:
            span = v_hi - v_lo
            frac = (millivolts - v_lo) / span if span else 0
            return round(p_lo + frac * (p_hi - p_lo))
    return 0


def find_receiver_interfaces():
    """Scan all HID devices for Logitech (0x046D) HID++ interfaces.

    Returns a list of dicts, one per *receiver* found, each with
    'short_path' and/or 'long_path' set, plus vid/pid/product info.
    """
    receivers = {}
    for info in hid.enumerate(VENDOR_ID_LOGITECH, 0):
        usage_page = info.get("usage_page")
        usage = info.get("usage")
        if usage_page != 0xFF00 or usage not in (0x0001, 0x0002):
            continue
        key = (info["vendor_id"], info["product_id"], info.get("serial_number"))
        entry = receivers.setdefault(key, {
            "vendor_id": info["vendor_id"],
            "product_id": info["product_id"],
            "product_string": info.get("product_string"),
            "short_path": None,
            "long_path": None,
        })
        if usage == 0x0001:
            entry["short_path"] = info["path"]
        elif usage == 0x0002:
            entry["long_path"] = info["path"]
    return list(receivers.values())
