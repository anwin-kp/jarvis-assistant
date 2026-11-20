"""System information and settings: CPU/RAM/battery, processes, volume, brightness, lock."""

import ctypes

import psutil

from jarvis.tools.registry import Risk, ToolRegistry

# Processes JARVIS must never terminate.
PROTECTED = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe",
    "fontdrvhost.exe", "memcompression", "secure system", "ntoskrnl.exe",
}


def _speakers_volume():
    from pycaw.pycaw import AudioUtilities

    device = AudioUtilities.GetSpeakers()
    vol = getattr(device, "EndpointVolume", None)  # pycaw >= 2025 API
    if vol is None:  # older pycaw API
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume

        iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
    return vol


def register(reg: ToolRegistry) -> None:
    @reg.tool("get_system_info", "Get current CPU, RAM, disk and battery usage.", {}, read_only=True)
    def get_system_info():
        bat = psutil.sensors_battery()
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "ram_used_gb": round(mem.used / 2**30, 1),
            "ram_total_gb": round(mem.total / 2**30, 1),
            "ram_percent": mem.percent,
            "disk_c_free_gb": round(psutil.disk_usage("C:\\").free / 2**30, 1),
            "battery_percent": bat.percent if bat else None,
            "plugged_in": bat.power_plugged if bat else None,
        }

    @reg.tool(
        "list_processes",
        "List the top running processes by memory or CPU usage.",
        {
            "sort_by": {"type": "string", "enum": ["memory", "cpu"]},
            "limit": {"type": "integer", "description": "How many to return, 1-25"},
        },
        required=["sort_by"],
        group="system",
        read_only=True,
    )
    def list_processes(sort_by: str, limit: int = 10):
        procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
        if sort_by == "cpu":
            for p in procs:  # first call primes the counter
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
            psutil.cpu_percent(interval=0.5)
        rows = []
        for p in procs:
            try:
                mem = p.info["memory_info"]
                rows.append({
                    "pid": p.info["pid"],
                    "name": p.info["name"],
                    "memory_mb": round(mem.rss / 2**20) if mem else 0,
                    "cpu_percent": p.cpu_percent(None) if sort_by == "cpu" else None,
                })
            except psutil.Error:
                continue
        key = "memory_mb" if sort_by == "memory" else "cpu_percent"
        rows.sort(key=lambda r: r[key] or 0, reverse=True)
        return rows[: max(1, min(int(limit), 25))]

    @reg.tool(
        "kill_process",
        "Force-stop a process by its PID. Use only when the user asks to kill a specific process.",
        {"pid": {"type": "integer"}},
        risk=Risk.DANGER,
        group="system",
    )
    def kill_process(pid: int):
        p = psutil.Process(int(pid))
        name = p.name()
        if name.lower() in PROTECTED or int(pid) in (0, 4):
            raise PermissionError(f"{name} is a protected system process")
        p.kill()
        return f"Killed {name} (PID {pid})."

    @reg.tool("lock_screen", "Lock the Windows session.", {}, group="system")
    def lock_screen():
        ctypes.windll.user32.LockWorkStation()
        return "Screen locked."

    @reg.tool("get_volume", "Get the current speaker volume and mute state.", {}, group="system", read_only=True)
    def get_volume():
        vol = _speakers_volume()
        return {"percent": round(vol.GetMasterVolumeLevelScalar() * 100), "muted": bool(vol.GetMute())}

    @reg.tool(
        "set_volume",
        "Set the speaker volume (0-100).",
        {"percent": {"type": "integer"}},
        group="system",
    )
    def set_volume(percent: int):
        percent = max(0, min(int(percent), 100))
        vol = _speakers_volume()
        vol.SetMasterVolumeLevelScalar(percent / 100, None)
        if percent > 0 and vol.GetMute():
            vol.SetMute(0, None)
        return f"Volume set to {percent}%."

    @reg.tool(
        "set_mute",
        "Mute or unmute the speakers.",
        {"muted": {"type": "boolean"}},
        group="system",
    )
    def set_mute(muted: bool):
        _speakers_volume().SetMute(1 if muted else 0, None)
        return "Muted." if muted else "Unmuted."

    @reg.tool(
        "set_brightness",
        "Set the laptop screen brightness (0-100).",
        {"percent": {"type": "integer"}},
        group="system",
    )
    def set_brightness(percent: int):
        import screen_brightness_control as sbc

        percent = max(0, min(int(percent), 100))
        sbc.set_brightness(percent)
        return f"Brightness set to {percent}%."


def register_power(reg: ToolRegistry) -> None:
    import subprocess

    def _shutdown(*args: str) -> None:
        subprocess.run(["shutdown", *args], check=True, capture_output=True)

    @reg.tool(
        "shutdown_pc",
        "Shut down the PC after a delay (can be cancelled with cancel_shutdown).",
        {"delay_minutes": {"type": "integer", "description": "1-120, default 1"}},
        required=[],
        risk=Risk.DANGER,
        group="system",
    )
    def shutdown_pc(delay_minutes: int = 1):
        secs = max(1, min(int(delay_minutes), 120)) * 60
        _shutdown("/s", "/t", str(secs))
        return f"Shutting down in {secs // 60} minute(s). Say 'cancel shutdown' to stop it."

    @reg.tool(
        "restart_pc",
        "Restart the PC after a delay (can be cancelled with cancel_shutdown).",
        {"delay_minutes": {"type": "integer", "description": "1-120, default 1"}},
        required=[],
        risk=Risk.DANGER,
        group="system",
    )
    def restart_pc(delay_minutes: int = 1):
        secs = max(1, min(int(delay_minutes), 120)) * 60
        _shutdown("/r", "/t", str(secs))
        return f"Restarting in {secs // 60} minute(s). Say 'cancel shutdown' to stop it."

    @reg.tool("cancel_shutdown", "Cancel a scheduled shutdown or restart.", {}, group="system")
    def cancel_shutdown():
        try:
            _shutdown("/a")
        except subprocess.CalledProcessError:
            return "There was no shutdown or restart scheduled."
        return "Cancelled the scheduled shutdown/restart."
