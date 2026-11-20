"""Quick PC toggles: dark mode, Wi-Fi/Bluetooth radios, screenshots, recycle bin, Settings pages."""

import asyncio
import ctypes
import os
import winreg
from datetime import datetime
from pathlib import Path

from jarvis.tools.registry import Risk, ToolRegistry

SETTINGS = {"wifi": "network-wifi", "bluetooth": "bluetooth", "display": "display", "sound": "sound",
            "windows update": "windowsupdate", "apps": "appsfeatures", "battery": "batterysaver",
            "notifications": "notifications", "personalization": "personalization", "privacy": "privacy",
            "storage": "storagesense", "network": "network-status", "power": "powersleep", "mouse": "mousetouchpad"}


def set_dark_mode(on: bool) -> None:
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                         0, winreg.KEY_SET_VALUE)
    for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if on else 1)
    winreg.CloseKey(key)
    # tell open apps the theme changed
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0x2, 1000, None)


async def _set_radio(kind: str, on: bool) -> str:
    from winrt.windows.devices.radios import Radio, RadioKind, RadioState

    await Radio.request_access_async()
    want = RadioKind.WI_FI if kind == "wifi" else RadioKind.BLUETOOTH
    radios = [r for r in await Radio.get_radios_async() if r.kind == want]
    if not radios:
        return f"ERROR: no {kind} adapter found."
    for r in radios:
        await r.set_state_async(RadioState.ON if on else RadioState.OFF)
    return f"{'Wi-Fi' if kind == 'wifi' else 'Bluetooth'} turned {'on' if on else 'off'}."


def screenshot_dir() -> Path:
    from jarvis.tools.file_tools import resolve_root

    d = resolve_root("pictures") / "Screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def take_screenshot(all_screens: bool = True) -> Path:
    from PIL import ImageGrab

    path = screenshot_dir() / f"JARVIS {datetime.now():%Y-%m-%d %H.%M.%S}.png"
    ImageGrab.grab(all_screens=all_screens).save(path)
    return path


def register(reg: ToolRegistry) -> None:
    @reg.tool("set_dark_mode", "Switch Windows between dark mode (on=true) and light mode (on=false).",
              {"on": {"type": "boolean"}}, group="pc")
    def _dark(on: bool):
        set_dark_mode(bool(on))
        return f"{'Dark' if on else 'Light'} mode on."

    @reg.tool("set_radio", "Turn Wi-Fi or Bluetooth on or off.",
              {"kind": {"type": "string", "enum": ["wifi", "bluetooth"]}, "on": {"type": "boolean"}},
              risk=Risk.CONFIRM, group="pc")
    def _radio(kind: str, on: bool):
        return asyncio.run(_set_radio(kind, bool(on)))

    @reg.tool("take_screenshot", "Take a screenshot and save it to Pictures\\Screenshots.", {}, group="pc")
    def _shot():
        p = take_screenshot()
        return f"Saved screenshot: {p}"

    @reg.tool("empty_recycle_bin", "Permanently empty the Recycle Bin.", {}, risk=Risk.DANGER, group="pc")
    def _empty():
        res = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)  # no confirm, progress or sound
        return "Recycle Bin emptied." if res in (0, -2147418113) else f"Recycle Bin: nothing to empty (code {res})."

    @reg.tool("open_settings", f"Open a Windows Settings page: {', '.join(SETTINGS)}.",
              {"page": {"type": "string", "enum": list(SETTINGS)}}, group="pc")
    def _settings(page: str):
        os.startfile(f"ms-settings:{SETTINGS[page]}")
        return f"Opened {page} settings."
