"""
System tray app that shows a Logitech mouse's battery level (read directly
from the USB receiver via HID++) and sends Windows toast notifications
when it drops past configured thresholds.

Run `python probe.py` first if config.json doesn't already have
vendor_id/product_id/device_index filled in -- this app will also try to
auto-discover them on first launch and save the result.
"""

import json
import os
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw
from winotify import Notification, audio

import hidpp

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
APP_NAME = "Logi Mouse Battery"

DEFAULT_CONFIG = {
    "vendor_id": None,
    "product_id": None,
    "device_index": None,
    "poll_interval_seconds": 900,
    "notify_thresholds": [30, 15, 5],
    "notify_cooldown_hours": 6,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as exc:
        print(f"Warning: couldn't save config.json: {exc}")


def notify(title, message, urgent=False):
    try:
        toast = Notification(
            app_id=APP_NAME,
            title=title,
            msg=message,
            duration="short",
        )
        toast.set_audio(audio.Default if not urgent else audio.Reminder, loop=False)
        toast.show()
    except Exception as exc:  # noqa: BLE001 - notifications must never crash the poll loop
        print(f"Notification failed: {exc}")


class BatteryState:
    def __init__(self, thresholds, hysteresis=5):
        self.thresholds = sorted(thresholds, reverse=True)
        self.hysteresis = hysteresis
        self.notified = set()
        self.last_percent = None
        self.last_charging = None
        self.last_error = None
        self.consecutive_failures = 0

    def update(self, percent, charging):
        self.last_percent = percent
        self.last_charging = charging
        self.last_error = None
        self.consecutive_failures = 0
        to_notify = []
        for t in self.thresholds:
            if percent <= t and t not in self.notified:
                self.notified.add(t)
                to_notify.append(t)
            elif percent > t + self.hysteresis:
                self.notified.discard(t)
        return to_notify

    def record_failure(self, message):
        self.last_error = message
        self.consecutive_failures += 1
        return self.consecutive_failures


# How many misses in a row before we show the red error icon, instead of
# just quietly keeping the last known-good reading on screen. Wireless
# mice often don't answer a poll if they've been idle and the radio has
# gone to sleep -- that's not really an "error," it just needs a nudge.
FAILURE_THRESHOLD = 3

# Within a single poll, retry this many times (with a short pause) before
# giving up for this cycle. Often enough on its own to catch a mouse that
# wakes up a second or two after the first attempt.
RETRY_ATTEMPTS_PER_POLL = 3
RETRY_DELAY_SECONDS = 2


def discover_device(cfg):
    """Try to find a receiver + device index that reports battery info.
    Returns True and mutates cfg in place if successful.
    """
    receivers = hidpp.find_receiver_interfaces()
    for r in receivers:
        if not r["short_path"]:
            continue
        dev = hidpp.HidppDevice(short_path=r["short_path"], long_path=r["long_path"])
        try:
            for device_index in range(1, 7):
                try:
                    battery = dev.read_battery(device_index)
                except hidpp.HidppError:
                    battery = None
                if battery:
                    cfg["vendor_id"] = r["vendor_id"]
                    cfg["product_id"] = r["product_id"]
                    cfg["device_index"] = device_index
                    save_config(cfg)
                    return True
        finally:
            dev.close()
    return False


def open_configured_device(cfg):
    receivers = hidpp.find_receiver_interfaces()
    for r in receivers:
        if r["vendor_id"] == cfg["vendor_id"] and r["product_id"] == cfg["product_id"]:
            return hidpp.HidppDevice(short_path=r["short_path"], long_path=r["long_path"])
    return None


def make_icon_image(percent, charging, error=False):
    """Draw a simple battery glyph, filled according to percent."""
    w, h = 64, 64
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    body = (4, 14, 52, 50)
    tip = (52, 24, 60, 40)
    draw.rounded_rectangle(body, radius=6, outline=(230, 230, 230, 255), width=4)
    draw.rectangle(tip, fill=(230, 230, 230, 255))

    if error:
        draw.line((16, 20, 44, 44), fill=(200, 60, 60, 255), width=6)
        draw.line((44, 20, 16, 44), fill=(200, 60, 60, 255), width=6)
        return img

    if percent is None:
        return img

    if percent <= 15:
        color = (220, 60, 60, 255)
    elif percent <= 30:
        color = (230, 180, 40, 255)
    else:
        color = (90, 200, 100, 255)

    inner = (8, 18, 48, 46)
    inner_w = inner[2] - inner[0]
    fill_w = max(2, int(inner_w * (percent / 100)))
    draw.rectangle((inner[0], inner[1], inner[0] + fill_w, inner[3]), fill=color)

    if charging:
        draw.polygon([(30, 12), (22, 32), (30, 32), (26, 52), (42, 26), (34, 26), (38, 12)],
                     fill=(255, 240, 120, 255), outline=(80, 80, 80, 255))

    return img


class TrayApp:
    def __init__(self):
        self.cfg = load_config()
        self.state = BatteryState(
            self.cfg.get("notify_thresholds", DEFAULT_CONFIG["notify_thresholds"])
        )
        self.icon = pystray.Icon(
            APP_NAME,
            make_icon_image(None, False),
            "Logi mouse battery: unknown",
            menu=self._build_menu(),
        )
        self._stop = threading.Event()

    def _build_menu(self):
        def status_text(item):
            if self.state.consecutive_failures >= FAILURE_THRESHOLD:
                return f"Error: {self.state.last_error}"
            if self.state.last_percent is None:
                return "Battery: reading..."
            charging = " (charging)" if self.state.last_charging else ""
            suffix = " [retrying...]" if self.state.consecutive_failures else ""
            return f"Battery: {self.state.last_percent}%{charging}{suffix}"

        return pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.MenuItem("Refresh now", lambda icon, item: self.poll_once()),
            pystray.MenuItem("Re-run device discovery", lambda icon, item: self.rediscover()),
            pystray.MenuItem("Quit", self.quit),
        )

    def rediscover(self):
        self.cfg["vendor_id"] = None
        self.cfg["product_id"] = None
        self.cfg["device_index"] = None
        ok = discover_device(self.cfg)
        if ok:
            notify(APP_NAME, "Found your mouse. Battery tracking resumed.")
        else:
            notify(APP_NAME, "Couldn't find the mouse. Move it to wake it, then try again.")
        self.poll_once()

    def poll_once(self):
        cfg = self.cfg
        if cfg.get("vendor_id") is None or cfg.get("device_index") is None:
            if not discover_device(cfg):
                self._handle_failure("device not found")
                return

        dev = open_configured_device(cfg)
        if dev is None:
            self._handle_failure("receiver unplugged?")
            return

        battery = None
        last_exc = None
        try:
            for attempt in range(RETRY_ATTEMPTS_PER_POLL):
                try:
                    battery = dev.read_battery(cfg["device_index"])
                except hidpp.HidppError as exc:
                    last_exc = exc
                    battery = None
                if battery:
                    break
                if attempt < RETRY_ATTEMPTS_PER_POLL - 1:
                    time.sleep(RETRY_DELAY_SECONDS)
        finally:
            dev.close()

        if not battery:
            reason = str(last_exc) if last_exc else "no battery reply (mouse asleep?)"
            self._handle_failure(reason)
            return

        percent = battery["percent"]
        charging = bool(battery.get("charging"))
        crossed = self.state.update(percent, charging)

        self.icon.icon = make_icon_image(percent, charging)
        self.icon.title = f"Logi mouse battery: {percent}%" + (" (charging)" if charging else "")
        self.icon.menu = self._build_menu()

        for threshold in sorted(crossed):
            urgent = threshold <= 15
            notify(
                APP_NAME,
                f"Mouse battery at {percent}%. Consider charging it soon."
                if not urgent else
                f"Mouse battery critically low ({percent}%). Charge it now.",
                urgent=urgent,
            )

    def _handle_failure(self, message):
        """Record a failed poll. Only switches the tray icon to the error
        state after several misses in a row -- a single miss usually just
        means the mouse's radio was asleep, and the next scheduled poll
        (or the next time you touch the mouse) will likely succeed.
        """
        count = self.state.record_failure(message)
        if count >= FAILURE_THRESHOLD:
            self.icon.icon = make_icon_image(None, False, error=True)
            self.icon.title = f"Logi mouse battery: no response ({message})"
        self.icon.menu = self._build_menu()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
                print(f"Poll error: {exc}")
            self._stop.wait(self.cfg.get("poll_interval_seconds", 900))

    def quit(self, icon, item):
        self._stop.set()
        icon.stop()

    def run(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.icon.run()


def main():
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()