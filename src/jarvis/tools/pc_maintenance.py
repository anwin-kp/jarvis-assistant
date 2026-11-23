"""Whole-PC control, part 2: system health (startup apps, Windows Update, big and duplicate files, temperatures,
battery health) and files in bulk (organize by type, rename, zip / unzip). Health tools only read. File changes stay
inside the allowed folders, preview what they'll do and ask first, never overwrite, and check the result."""

import hashlib
import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

from jarvis.tools.registry import Risk, ToolRegistry

CATEGORY = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg", ".tif", ".tiff", ".raw"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".m4v"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Installers": {".exe", ".msi", ".msix", ".appx", ".apk", ".dmg"},
    "Code": {".py", ".js", ".ts", ".dart", ".java", ".kt", ".cs", ".cpp", ".c", ".h", ".json", ".yaml", ".yml",
             ".html", ".css", ".sql", ".sh", ".ps1"},
}
PARTIAL = {".crdownload", ".part", ".tmp", ".download"}


def category_of(path: Path) -> str:
    ext = path.suffix.lower()
    return next((c for c, exts in CATEGORY.items() if ext in exts), "Other")


def free_name(target: Path) -> Path:
    """Never overwrite: 'report.pdf' -> 'report (2).pdf'."""
    if not target.exists():
        return target
    for i in range(2, 1000):
        cand = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(target)


def plan_organize(folder: Path) -> dict[Path, Path]:
    moves = {}
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() not in PARTIAL and not p.name.startswith(("~$", ".")) \
                and p.name.lower() != "desktop.ini":
            moves[p] = folder / category_of(p) / p.name
    return moves


def plan_rename(folder: Path, pattern: str, extension: str = "") -> dict[Path, Path]:
    """pattern tokens: {name} (old name), {n} (1, 2, ...), {n:03} (001...), {date} (file's date, 2026-11-23)."""
    if "{n" not in pattern and "{name}" not in pattern and "{date}" not in pattern:
        raise ValueError("the pattern needs {n}, {name} or {date} so the names differ")
    files = sorted((p for p in folder.iterdir() if p.is_file() and (not extension or p.suffix.lower()
                                                                     == "." + extension.lower().lstrip("."))),
                   key=lambda p: p.stat().st_mtime)
    out, taken = {}, set()
    for i, p in enumerate(files, 1):
        date = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
        stem = pattern.replace("{name}", p.stem).replace("{date}", date)
        stem = re.sub(r"\{n(?::0?(\d))?\}", lambda m, i=i: str(i).zfill(int(m.group(1) or 0)), stem)
        stem = re.sub(r'[<>:"/\\|?*]', "_", stem).strip(" .")
        new = folder / f"{stem}{p.suffix}"
        if new.name.lower() in taken or (new.exists() and new != p):
            raise ValueError(f"'{new.name}' would clash with another file; use {{n}} in the pattern")
        taken.add(new.name.lower())
        if new != p:
            out[p] = new
    return out


def duplicates(folder: Path, min_size: int = 1024) -> list[list[Path]]:
    by_size: dict[int, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for n in filenames:
            p = Path(dirpath) / n
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size >= min_size:
                by_size.setdefault(size, []).append(p)
    groups = []
    for same in (g for g in by_size.values() if len(g) > 1):
        by_hash: dict[str, list[Path]] = {}
        for p in same:
            try:
                h = hashlib.sha256(p.read_bytes()).hexdigest() if p.stat().st_size < 512 * 2**20 else None
            except OSError:
                h = None
            if h:
                by_hash.setdefault(h, []).append(p)
        groups += [g for g in by_hash.values() if len(g) > 1]
    return sorted(groups, key=lambda g: -g[0].stat().st_size)


def _ps(script: str, timeout: int = 60) -> str:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       text=True, timeout=timeout, encoding="utf-8", errors="replace",
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return (r.stdout or "").strip()


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit in ("B", "KB") else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def register(reg: ToolRegistry, guard, roots: list[Path]) -> None:
    """guard(path) -> Path: the allowed-folders check from file_tools."""

    # -------------------------------------------------------------- health
    @reg.tool("startup_apps", "Which apps start with Windows (and whether each is enabled).", {}, group="system",
              read_only=True)
    def startup_apps():
        import winreg

        rows = []
        for hive, label in ((winreg.HKEY_CURRENT_USER, "you"), (winreg.HKEY_LOCAL_MACHINE, "all users")):
            try:
                with winreg.OpenKey(hive, STARTUP_KEY) as k:
                    names = [winreg.EnumValue(k, i)[0] for i in range(winreg.QueryInfoKey(k)[1])]
            except OSError:
                continue
            for n in names:
                rows.append(f"{n} ({label}, {'enabled' if _startup_enabled(hive, n) else 'disabled'})")
        folder = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
        rows += [f"{p.stem} (startup folder)" for p in folder.glob("*") if p.suffix.lower() in (".lnk", ".url")]
        return rows or "No startup apps."

    def _startup_enabled(hive, name: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(hive, APPROVED_KEY) as k:
                data, _ = winreg.QueryValueEx(k, name)
                return not (data and data[0] % 2 == 1)  # 03... = disabled (as Task Manager writes it)
        except OSError:
            return True

    @reg.tool("set_startup_app", "Enable or disable one of YOUR startup apps (like Task Manager's Startup tab).",
              {"name": {"type": "string"}, "enabled": {"type": "boolean"}}, risk=Risk.CONFIRM, group="system")
    def set_startup_app(name: str, enabled: bool):
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as k:
            names = [winreg.EnumValue(k, i)[0] for i in range(winreg.QueryInfoKey(k)[1])]
        hit = [n for n in names if name.lower() in n.lower()]
        if len(hit) != 1:
            return (f"ERROR: {'no' if not hit else 'more than one'} startup app of yours matches '{name}'. "
                    f"Yours: {', '.join(names) or 'none'} (apps for all users need Task Manager as administrator).")
        data = bytes([2 if enabled else 3]) + bytes(11)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            winreg.SetValueEx(k, hit[0], 0, winreg.REG_BINARY, data)
        if _startup_enabled(winreg.HKEY_CURRENT_USER, hit[0]) != bool(enabled):
            return f"WARNING: tried to {'enable' if enabled else 'disable'} {hit[0]}, but it didn't change."
        return f"{hit[0]} will {'now' if enabled else 'no longer'} start with Windows (checked)."

    @reg.tool("windows_update_status", "Windows Update: when updates were last installed and how many are waiting "
              "(takes up to a minute).", {}, group="system", read_only=True)
    def windows_update_status():
        last = _ps("(Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1).InstalledOn"
                   ".ToString('yyyy-MM-dd')", 60)
        pending = _ps("$s = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher();"
                      "$r = $s.Search('IsInstalled=0 and IsHidden=0'); $r.Updates | ForEach-Object { $_.Title }", 120)
        titles = [t for t in pending.splitlines() if t.strip()]
        return (f"Last update installed: {last or 'unknown'}. Waiting: {len(titles)}"
                + (": " + "; ".join(t[:80] for t in titles[:5]) if titles else "") + ".")

    @reg.tool("large_files", "The biggest files in a folder (default: all allowed folders), to free up space.",
              {"folder": {"type": "string"}, "top": {"type": "integer"}}, required=[], group="files", read_only=True)
    def large_files(folder: str = "", top: int = 15):
        bases = [guard(folder)] if folder else roots
        found = []
        for base in bases:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
                for n in filenames:
                    p = Path(dirpath) / n
                    try:
                        found.append((p.stat().st_size, p))
                    except OSError:
                        pass
        found.sort(reverse=True)
        return [f"{_size(s)}  {p}" for s, p in found[: max(1, min(int(top or 15), 50))]] or "No files found."

    @reg.tool("duplicate_files", "Find identical files (same content) in a folder.", {"folder": {"type": "string"}},
              group="files", read_only=True)
    def duplicate_files(folder: str):
        groups = duplicates(guard(folder))
        if not groups:
            return "No duplicate files found."
        waste = sum(g[0].stat().st_size * (len(g) - 1) for g in groups)
        rows = [f"{_size(g[0].stat().st_size)} x{len(g)}: " + " | ".join(str(p) for p in g[:4]) for g in groups[:15]]
        return f"{len(groups)} sets of duplicates ({_size(waste)} could be freed):\n" + "\n".join(rows)

    @reg.tool("pc_temperatures", "GPU (and CPU, if Windows exposes it) temperature.", {}, group="system",
              read_only=True)
    def pc_temperatures():
        out = []
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            name, temp, util = (x.strip() for x in r.stdout.strip().splitlines()[0].split(","))
            out.append(f"GPU {name}: {temp} °C ({util}% busy)")
        except Exception:
            pass
        cpu = _ps("(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop |"
                  " Select-Object -First 1).CurrentTemperature", 20)
        if cpu.isdigit() and int(cpu) > 2732:
            out.append(f"CPU area: {(int(cpu) - 2732) / 10:.0f} °C")
        else:
            out.append("CPU: Windows doesn't expose it without administrator rights")
        return "; ".join(out)

    @reg.tool("battery_health", "Battery wear: design capacity vs what it holds now, and charge cycles.", {},
              group="system", read_only=True)
    def battery_health():
        import tempfile

        out = Path(tempfile.gettempdir()) / "jarvis-battery.xml"
        subprocess.run(["powercfg", "/batteryreport", "/xml", "/output", str(out)], capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            xml = out.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return "ERROR: no battery report (this PC may have no battery)."
        finally:
            out.unlink(missing_ok=True)
        design = re.search(r"<DesignCapacity>(\d+)<", xml)
        full = re.search(r"<FullChargeCapacity>(\d+)<", xml)
        cycles = re.search(r"<CycleCount>(\d+)<", xml)
        if not (design and full):
            return "ERROR: no battery found in the report."
        pct = 100 * int(full.group(1)) / max(1, int(design.group(1)))
        return (f"Battery health {pct:.0f}%: holds {int(full.group(1)) / 1000:.1f} Wh of the original "
                f"{int(design.group(1)) / 1000:.1f} Wh" + (f", {cycles.group(1)} charge cycles" if cycles else "")
                + (". Worn: consider a replacement." if pct < 70 else "."))

    # -------------------------------------------------------------- files in bulk
    @reg.tool("organize_folder", "Sort a folder's files into subfolders by type (Images, Documents, Videos, "
              "Installers...). Shows what it will do and asks first; never overwrites.",
              {"folder": {"type": "string"}}, group="files")
    def organize_folder(folder: str):
        base = guard(folder)
        moves = plan_organize(base)
        if not moves:
            return f"Nothing to organize in {base}."
        counts: dict[str, int] = {}
        for t in moves.values():
            counts[t.parent.name] = counts.get(t.parent.name, 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        if not reg.ask(f"sort {len(moves)} files in {base.name} into folders: {summary}"):
            return "Not organized: you didn't approve it. Nothing was moved."
        done = 0
        for src, dst in moves.items():
            dst.parent.mkdir(exist_ok=True)
            try:
                shutil.move(str(src), str(free_name(dst)))
                done += 1
            except OSError:
                pass
        left = sum(1 for p in base.iterdir() if p.is_file() and p in moves)
        return f"Organized {done} files into {summary} (checked: {left} left in place, e.g. in use)."

    @reg.tool("bulk_rename", "Rename many files in a folder by a pattern: {name} old name, {n} or {n:03} number, "
              "{date} file date. e.g. 'Goa trip {n:03}'. Optional extension filter. Shows examples and asks first.",
              {"folder": {"type": "string"}, "pattern": {"type": "string"}, "extension": {"type": "string"}},
              required=["folder", "pattern"], group="files")
    def bulk_rename(folder: str, pattern: str, extension: str = ""):
        base = guard(folder)
        try:
            plan = plan_rename(base, pattern, extension)
        except ValueError as e:
            return f"ERROR: {e}."
        if not plan:
            return "Nothing to rename."
        examples = "; ".join(f"{a.name} -> {b.name}" for a, b in list(plan.items())[:3])
        if not reg.ask(f"rename {len(plan)} files in {base.name}, e.g. {examples}"):
            return "Not renamed: you didn't approve it."
        tmp = {}
        for a in plan:  # two steps, so names that swap never collide
            t = a.with_name(f".jarvis-rename-{time.time_ns()}{a.suffix}")
            a.rename(t)
            tmp[t] = plan[a]
        for t, b in tmp.items():
            t.rename(b)
        ok = sum(1 for b in plan.values() if b.exists())
        return f"Renamed {ok} of {len(plan)} files (checked), e.g. {examples}."

    @reg.tool("zip_files", "Make a .zip of a file or folder (next to it).", {"path": {"type": "string"}},
              group="files")
    def zip_files(path: str):
        src = guard(path)
        if not src.exists():
            return f"ERROR: {src} doesn't exist."
        dst = free_name(src.with_suffix(".zip") if src.is_file() else src.parent / f"{src.name}.zip")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
            items = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
            for p in items:
                z.write(p, p.name if src.is_file() else p.relative_to(src.parent))
        n = len(zipfile.ZipFile(dst).namelist())
        return f"Created {dst.name} ({n} files, {_size(dst.stat().st_size)}) (checked)."

    @reg.tool("unzip_file", "Extract a .zip into a folder next to it (never overwrites existing files).",
              {"path": {"type": "string"}}, group="files")
    def unzip_file(path: str):
        src = guard(path)
        if not zipfile.is_zipfile(src):
            return f"ERROR: {src.name} isn't a zip file."
        dest = free_name(src.with_suffix(""))
        dest.mkdir()
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                target = (dest / info.filename).resolve()
                if not target.is_relative_to(dest.resolve()):  # "zip slip": ../../ paths inside the archive
                    return f"ERROR: {src.name} contains unsafe paths ({info.filename}); nothing more extracted."
                z.extract(info, dest)
        n = sum(1 for p in dest.rglob("*") if p.is_file())
        return f"Extracted {n} files to {dest} (checked)."
