"""Biblioteca 360 - jogos de Xbox no PC, no estilo do app Xbox.

- Biblioteca com os jogos instalados no PC (Games on Demand, Arcade, pastas com default.xex e imagens .iso)
- Le o HD/pendrive do Xbox 360 (FATX) e instala os jogos no PC com um clique
- Roda os jogos de Xbox 360 no Xenia Canary e os do Xbox original no xemu, embutidos na propria janela
- Mostra os jogos de Xbox One/Series para PC instalados pelo app Xbox e abre o Xbox Cloud Gaming
- Pagina "Este PC" verifica se o computador aguenta o emulador
- Portatil: tudo fica na pasta do app; o Xenia pode ser baixado pelo proprio app

Execucao: python xbox360_launcher.py   (ou Biblioteca360.exe, quando empacotado)
"""
import base64
import ctypes
import io
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import webbrowser
import zipfile
from tkinter import filedialog, messagebox

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageTk

import fatx

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "xbox360_launcher.json")
DATA_DIR = os.path.join(APP_DIR, "dados")
COVERS_DIR = os.path.join(APP_DIR, "covers")
XENIA_DIR = os.path.join(APP_DIR, "xenia")
XEMU_DIR = os.path.join(APP_DIR, "xemu")
XENIA_RELEASES = "https://github.com/xenia-canary/xenia-canary-releases/releases"
XENIA_API = "https://api.github.com/repos/xenia-canary/xenia-canary-releases/releases/latest"
XEMU_API = "https://api.github.com/repos/xemu-project/xemu/releases/latest"
XEMU_HDD_URL = "https://github.com/xemu-project/xemu-hdd-image/releases/latest/download/xbox_hdd.qcow2.zip"
CLOUD_URL = "https://www.xbox.com/play"
XBOX_APP_AUMID = "Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"
COMPAT_URL = "https://github.com/xenia-canary/game-compatibility/issues?q="
COVER_URL = "http://download.xbox.com/content/images/66acd000-77fe-1000-9115-d802{tid}/1033/boxartlg.jpg"
NO_WINDOW = 0x08000000

DEFAULT_CONFIG = {
    "xenia_path": "",
    "xemu_path": "",
    "xbox_bios": "",
    "xbox_mcpx": "",
    "xbox_hdd": "",
    "gamertag": "",
    "game_dirs": [],
    "install_dir": "",
    "fullscreen": False,
    "recent": [],
    "video_modes": {},
    "last_played": {},
}

# Cores no estilo do app Xbox (tema escuro)
BG = "#141414"
SIDEBAR = "#1c1c1c"
SURFACE = "#262626"
SURFACE_HI = "#333333"
FG = "#ffffff"
MUTED = "#a6a6a6"
ACCENT = "#107c10"
ACCENT_HI = "#169c16"
WARN = "#f2c200"
BAD = "#e5484d"
RING = "#ffffff"

CARD_W, CARD_H = 156, 208
SIDEBAR_W = 236
PAD_X = 44
KINECT_HINTS = ("kinect",)
# Modos de video do Xenia (por jogo): nome, descricao e opcoes de linha de comando
VIDEO_MODES = (
    ("padrao", "Padrão", []),
    ("vulkan", "Vulkan", ["--gpu=vulkan"]),
    ("d3d12_rov", "D3D12 (ROV)", ["--gpu=d3d12", "--render_target_path_d3d12=rov"]),
    ("d3d12_rtv", "D3D12 (RTV)", ["--gpu=d3d12", "--render_target_path_d3d12=rtv"]),
)
GAME_TYPE_DIRS = ("00007000", "000D0000")
XISO_MAGIC = b"MICROSOFT*XBOX*MEDIA"
# Onde fica a particao do jogo numa imagem de disco: Xbox original (XGD1), Xbox 360 (XGD2, XGD3) e
# imagem ja recortada (xiso), que pode ser de qualquer um dos dois.
XISO_PARTITIONS = ((0x18300000, "XBOX"), (0xFD90000, "ISO"), (0x2080000, "ISO"), (0, None))


def is_kinect(g):
    return any(h in (g["title"] + g["group"]).lower() for h in KINECT_HINTS)


def platform_name(g):
    return {"XBOX": "Xbox original", "PC": "Xbox One / Series"}.get(g["kind"], "Xbox 360")


def fmt_size(n):
    return f"{n / 2**30:.1f} GB".replace(".", ",") if n >= 2**30 else f"{n / 2**20:.0f} MB"


def safe_name(s):
    return "".join(c for c in s if c not in '<>:"/\\|?*').strip(" .") or "Jogo"


def powershell_json(cmd, timeout=40):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd + " | ConvertTo-Json -Compress"],
                           capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        data = json.loads(r.stdout) if r.stdout.strip() else []
        return data if isinstance(data, list) else [data]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


# ---------------------------------------------------------------- jogos


def read_stfs(path):
    """Titulo, title id e icone PNG de um pacote STFS, ou None."""
    try:
        with open(path, "rb") as f:
            head = f.read(0xB000)
    except OSError:
        return None
    if len(head) < 0x1720 or head[:4] not in (b"LIVE", b"PIRS", b"CON "):
        return None
    title = head[0x411:0x491].decode("utf-16-be", "ignore").split("\x00")[0].strip()
    if not title:
        title = head[0x1691:0x1711].decode("utf-16-be", "ignore").split("\x00")[0].strip()
    icon = None
    size = struct.unpack(">I", head[0x1712:0x1716])[0]
    if 0 < size <= 0x4000 and head[0x171A:0x171E] == b"\x89PNG":
        icon = head[0x171A:0x171A + size]
    return {"title": title or os.path.basename(path), "title_id": head[0x360:0x364].hex().upper(), "icon": icon}


def _xdvdfs_find(table, name):
    """Setor e tamanho de um arquivo na tabela de diretorio XDVDFS, ou None."""
    i = 0
    while i + 14 <= len(table):
        if table[i:i + 4] == b"\xff\xff\xff\xff":  # enchimento ate o fim do setor
            i = (i // 2048 + 1) * 2048
            continue
        sector, size = struct.unpack("<II", table[i + 4:i + 12])
        nl = table[i + 13]
        if table[i + 14:i + 14 + nl].lower() == name:
            return sector, size
        i += (14 + nl + 3) & ~3
    return None


def read_xbe_title(xbe):
    """Titulo e title id do certificado de um default.xbe (Xbox original)."""
    if len(xbe) < 0x178 or xbe[:4] != b"XBEH":
        return {}
    base, cert = struct.unpack("<I", xbe[0x104:0x108])[0], struct.unpack("<I", xbe[0x118:0x11C])[0]
    off = cert - base
    if not 0 < off <= len(xbe) - 0x5C:
        return {}
    tid = struct.unpack("<I", xbe[off + 8:off + 12])[0]
    title = xbe[off + 12:off + 92].decode("utf-16-le", "ignore").split("\x00")[0].strip()
    return {"title": title, "title_id": f"{tid:08X}"} if title else {"title_id": f"{tid:08X}"}


def read_xiso(path):
    """Plataforma (XBOX = Xbox original, ISO = Xbox 360), titulo e title id de uma imagem de disco."""
    try:
        with open(path, "rb") as f:
            for base, kind in XISO_PARTITIONS:
                f.seek(base + 0x10000)
                hdr = f.read(28)
                if hdr[:20] == XISO_MAGIC:
                    break
            else:
                return None
            root, root_size = struct.unpack("<II", hdr[20:28])
            f.seek(base + root * 2048)
            table = f.read(min(root_size, 1 << 20))
            if kind is None:
                if _xdvdfs_find(table, b"default.xbe"):
                    kind = "XBOX"
                elif _xdvdfs_find(table, b"default.xex"):
                    kind = "ISO"
                else:
                    return None
            info = {"title": os.path.splitext(os.path.basename(path))[0], "title_id": "", "kind": kind}
            entry = _xdvdfs_find(table, b"default.xbe") if kind == "XBOX" else None
            if entry:
                f.seek(base + entry[0] * 2048)
                info.update(read_xbe_title(f.read(min(entry[1], 0x10000))))
            return info
    except OSError:
        return None


def _publisher_id(publisher):
    """Hash do publicador que o Windows usa no nome da familia do pacote (ex.: 8wekyb3d8bbwe)."""
    import hashlib
    digest = hashlib.sha256(publisher.encode("utf-16-le")).digest()[:8]
    bits = "".join(f"{b:08b}" for b in digest) + "0"
    return "".join("0123456789abcdefghjkmnpqrstvwxyz"[int(bits[i:i + 5], 2)] for i in range(0, 65, 5))


def scan_store_games():
    """Jogos de Xbox One/Series para PC instalados pelo app Xbox (pastas X:\\XboxGames)."""
    import xml.etree.ElementTree as ET

    def find(el, tag):
        return next((x for x in el.iter() if x.tag.rsplit("}", 1)[-1] == tag), None)

    games = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\XboxGames"
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") not in (2, 3) or not os.path.isdir(root):
                continue
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            content = os.path.join(root, name, "Content")
            try:
                game = ET.parse(os.path.join(content, "MicrosoftGame.config")).getroot()
            except (OSError, ET.ParseError):
                continue
            ident, exe, shell = find(game, "Identity"), find(game, "Executable"), find(game, "ShellVisuals")
            if ident is None or exe is None or not ident.get("Name") or not exe.get("Id"):
                continue
            title = shell.get("DefaultDisplayName", "") if shell is not None else ""
            if not title or title.startswith("ms-resource:"):
                title = name
            icon = None
            logo = (shell.get("Square150x150Logo") or shell.get("StoreLogo")) if shell is not None else None
            if logo:
                try:
                    with open(os.path.join(content, logo), "rb") as f:
                        icon = f.read(1 << 20)
                except OSError:
                    pass
            tid = find(game, "TitleId")
            games.append({"title": title, "title_id": (tid.text or "").strip().upper() if tid is not None else "",
                          "icon": icon, "path": os.path.join(root, name), "kind": "PC",
                          "group": "Xbox One / Series (PC)",
                          "aumid": f"{ident.get('Name')}_{_publisher_id(ident.get('Publisher', ''))}!{exe.get('Id')}"})
    return games


def scan_games(dirs, max_depth=5):
    games = []
    seen = set()
    for root_dir in dirs:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        base_depth = root_dir.rstrip("\\/").count(os.sep)
        for cur, subdirs, files in os.walk(root_dir):
            if cur.count(os.sep) - base_depth >= max_depth:
                subdirs[:] = []
            subdirs[:] = [d for d in subdirs if not d.lower().endswith(".data")]
            # jogo extraido (pasta com default.xex)
            lower = {f.lower(): f for f in files}
            # imagens de disco (.iso): Xbox original vai para o xemu, Xbox 360 para o Xenia
            for name in files:
                full = os.path.join(cur, name)
                if not name.lower().endswith((".iso", ".xiso")) or full in seen:
                    continue
                info = read_xiso(full)
                if info:
                    seen.add(full)
                    info.update({"icon": None, "path": full,
                                 "group": os.path.basename(cur) or root_dir})
                    games.append(info)
            if "default.xex" in lower:
                p = os.path.join(cur, lower["default.xex"])
                if p not in seen:
                    seen.add(p)
                    games.append({"title": os.path.basename(cur), "title_id": "", "icon": None, "path": p,
                                  "kind": "XEX", "group": os.path.basename(os.path.dirname(cur)) or root_dir})
                subdirs[:] = []
                continue
            # Games on Demand (00007000) e Arcade (000D0000)
            if os.path.basename(cur).upper() in GAME_TYPE_DIRS:
                for name in files:
                    full = os.path.join(cur, name)
                    if "." in name or full in seen:
                        continue
                    info = read_stfs(full)
                    if not info:
                        continue
                    seen.add(full)
                    info.update({"path": full, "kind": "GOD" if cur.endswith("7000") else "XBLA",
                                 "group": os.path.basename(os.path.dirname(os.path.dirname(cur)))})
                    games.append(info)
    # jogos de varios discos com o mesmo title id
    by_id = {}
    for g in games:
        if g["title_id"]:
            by_id.setdefault(g["title_id"], []).append(g)
    for lst in by_id.values():
        if len(lst) > 1 and not any("Disc" in g["title"] or "Disco" in g["title"] for g in lst):
            for i, g in enumerate(sorted(lst, key=lambda x: x["path"]), 1):
                g["title"] = f"{g['title']} (Disco {i})"
    games.sort(key=lambda g: (g["group"].lower(), g["title"].lower()))
    return games


# ---------------------------------------------------------------- configuracao


def default_install_dir():
    best, free = "C:\\", -1
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # so discos fixos
                continue
            f = shutil.disk_usage(root).free
        except OSError:
            continue
        if f > free:
            best, free = root, f
    return os.path.join(best, "Biblioteca360", "Jogos")


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg["xenia_path"] or not os.path.isfile(cfg["xenia_path"]):
        cfg["xenia_path"] = find_xenia()
    if not cfg["xemu_path"] or not os.path.isfile(cfg["xemu_path"]):
        cfg["xemu_path"] = find_xemu()
    if not cfg["install_dir"]:
        cfg["install_dir"] = default_install_dir()
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError as e:
        messagebox.showerror("Erro", f"Não foi possível salvar a configuração:\n{e}")


def find_xenia():
    home = os.path.expanduser("~")
    for base in (XENIA_DIR, APP_DIR, "C:\\Xenia", "C:\\xenia_canary", "D:\\Xenia",
                 os.path.join(home, "Desktop"), os.path.join(home, "Downloads")):
        if not os.path.isdir(base):
            continue
        for cur, subdirs, files in os.walk(base):
            if cur.count(os.sep) - base.count(os.sep) >= 2:
                subdirs[:] = []
            for name in ("xenia_canary.exe", "xenia.exe"):
                if name in files:
                    return os.path.join(cur, name)
    return ""


def find_xemu():
    for base in (XEMU_DIR, "C:\\xemu", os.path.join(os.path.expanduser("~"), "Downloads")):
        if not os.path.isdir(base):
            continue
        for cur, subdirs, files in os.walk(base):
            if cur.count(os.sep) - base.count(os.sep) >= 2:
                subdirs[:] = []
            if "xemu.exe" in files:
                return os.path.join(cur, "xemu.exe")
    return ""


def toml_set(text, section, values):
    """Troca (ou acrescenta) chaves de uma secao [secao] de um arquivo TOML, mantendo o resto."""
    out, done, in_sec, seen = [], set(), False, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_sec:
                out += [f"{k} = {v}" for k, v in values.items() if k not in done]
                done |= set(values)
            in_sec = s == f"[{section}]"
            seen |= in_sec
        elif in_sec and s.split("=", 1)[0].strip() in values:
            k = s.split("=", 1)[0].strip()
            out.append(f"{k} = {values[k]}")
            done.add(k)
            continue
        out.append(line)
    rest = [f"{k} = {v}" for k, v in values.items() if k not in done]
    if rest:
        out += rest if in_sec else ([""] if out else []) + ([f"[{section}]"] if not seen else []) + rest
    return "\n".join(out) + "\n"


def write_xemu_config(cfg):
    """Aponta o xemu (modo portatil: xemu.toml ao lado do xemu.exe) para os arquivos do console."""
    path = os.path.join(os.path.dirname(cfg["xemu_path"]), "xemu.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    text = toml_set(text, "sys.files", {"bootrom_path": json.dumps(cfg["xbox_mcpx"]),
                                        "flashrom_path": json.dumps(cfg["xbox_bios"]),
                                        "hdd_path": json.dumps(cfg["xbox_hdd"])})
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- este PC


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def pc_info():
    k32 = ctypes.windll.kernel32
    info = {"avx": bool(k32.IsProcessorFeaturePresent(39)), "avx2": bool(k32.IsProcessorFeaturePresent(40)),
            "cpu": platform.processor(), "ram_gb": 0, "gpus": [],
            "windows": f"{platform.system()} {platform.release()} (build {platform.version()})",
            "x64": platform.machine().endswith("64")}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0") as k:
            info["cpu"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except OSError:
        pass
    ms = _MemStatus()
    ms.dwLength = ctypes.sizeof(ms)
    if k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        info["ram_gb"] = round(ms.ullTotalPhys / 2**30)
    info["gpus"] = [g.get("Name", "")
                    for g in powershell_json("Get-CimInstance Win32_VideoController | Select-Object Name")
                    if g.get("Name") and "Basic" not in g.get("Name", "") and "Virtual" not in g.get("Name", "")]
    return info


def gpu_tier(name):
    n = name.lower()
    if any(k in n for k in ("rtx", "gtx 1", "gtx 9", "rx 5", "rx 6", "rx 7", "rx 9", "arc a", "arc b", "radeon pro")):
        return 3
    if any(k in n for k in ("gtx", "rx 4", "rx 5", "radeon r9", "radeon r7", "vega 56", "vega 64", "iris xe",
                            "radeon 6", "radeon 7", "radeon 8")):
        return 2
    return 1


def pc_verdict(info):
    """(nivel 0-3, titulo, lista de observacoes)"""
    notes = []
    if not info["x64"]:
        return 0, "Este PC não roda o Xenia", ["O Xenia precisa de Windows 64 bits."]
    if not info["avx"]:
        return 0, "Este PC não roda o Xenia", ["O processador não tem a instrução AVX, que o Xenia exige."]
    tier = max([gpu_tier(g) for g in info["gpus"]] or [1])
    if not info["avx2"]:
        notes.append("Processador sem AVX2: o desempenho fica bem menor.")
    if info["ram_gb"] and info["ram_gb"] < 8:
        notes.append(f"Só {info['ram_gb']} GB de RAM: recomendado 8 GB ou mais.")
    if tier == 1:
        notes.append("Placa de vídeo integrada/antiga: jogos leves (Arcade, LEGO, Sonic, Cars) podem rodar; "
                     "jogos pesados vão ficar lentos ou não abrir.")
        level = 1
    elif tier == 2:
        notes.append("Placa de vídeo intermediária: muitos jogos rodam, alguns com lentidão.")
        level = 2
    else:
        notes.append("Placa de vídeo dedicada moderna: boa experiência na maioria dos jogos compatíveis.")
        level = 3
    if not info["avx2"] or (info["ram_gb"] and info["ram_gb"] < 8):
        level = min(level, 1)
    title = {1: "Roda, mas com limitações", 2: "Roda bem jogos leves e médios", 3: "Pronto para jogar"}[level]
    notes.append("A primeira vez de cada jogo é mais lenta (o Xenia prepara os gráficos).")
    return level, title, notes


# ---------------------------------------------------------------- HD do Xbox


def detect_xbox_disks():
    """Discos sem particao do Windows (RAW): candidatos a HD/pendrive do Xbox 360."""
    rows = powershell_json(
        "Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' -and $_.Size -gt 1GB } | Select-Object Number, FriendlyName, Size, SerialNumber, BusType")
    disks = []
    for r in rows:
        try:
            disks.append({"number": int(r["Number"]), "name": (r.get("FriendlyName") or "Disco").strip(),
                          "size": int(r.get("Size") or 0), "serial": (r.get("SerialNumber") or "").strip(),
                          "bus": str(r.get("BusType") or "")})
        except (KeyError, TypeError, ValueError):
            continue
    return disks


def disk_key(d):
    return safe_name((d["serial"] or d["name"]) + f"_{d['size']}")


def run_elevated_helper(job_path):
    """Roda o leitor FATX como administrador (o Windows mostra o pedido de permissao)."""
    if FROZEN:
        exe, params = sys.executable, f'--hd-helper "{job_path}"'
    else:
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw
        params = f'"{os.path.abspath(__file__)}" --hd-helper "{job_path}"'
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    r = shell32.ShellExecuteW(None, "runas", exe, params, APP_DIR, 0)
    return (r or 0) > 32


# ---------------------------------------------------------------- Win32 (Xenia dentro da janela)

user32 = ctypes.windll.user32
HWND = ctypes.c_void_p
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINMAXBOX = 0x00030000
WM_CLOSE = 0x0010
WM_CHAR = 0x0102

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetParent.argtypes = [HWND, HWND]
user32.SetParent.restype = HWND
user32.SetMenu.argtypes = [HWND, HWND]
user32.MoveWindow.argtypes = [HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_bool]
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(ctypes.c_ulong)]
user32.IsWindowVisible.argtypes = [HWND]
user32.GetWindow.argtypes = [HWND, ctypes.c_uint]
user32.GetWindow.restype = HWND
user32.PostMessageW.argtypes = [HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.SetFocus.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, HWND, ctypes.c_ssize_t)


def win32_find_main_window(pid):
    found = []

    def cb(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(_ENUM_PROC(cb), 0)
    return found[0] if found else None


def win32_embed(hwnd, parent):
    user32.SetMenu(hwnd, None)
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINMAXBOX)
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style | WS_CHILD)
    user32.SetParent(hwnd, parent)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0024)  # SWP_NOZORDER | SWP_FRAMECHANGED
    user32.ShowWindow(hwnd, 5)


def win32_move(hwnd, w, h):
    user32.MoveWindow(hwnd, 0, 0, max(1, w), max(1, h), True)


# ---------------------------------------------------------------- controle (XInput)


class _XGamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _XState(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint), ("pad", _XGamepad)]


class Gamepad:
    """Le controles Xbox via XInput (slots 0-3) e devolve botoes recem-pressionados."""
    BUTTONS = {0x0001: "UP", 0x0002: "DOWN", 0x0004: "LEFT", 0x0008: "RIGHT",
               0x0010: "START", 0x0020: "BACK", 0x0100: "LB", 0x0200: "RB",
               0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y"}
    STICK = 16000

    def __init__(self):
        self.xinput = None
        for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.xinput = getattr(ctypes.windll, dll)
                break
            except (OSError, AttributeError):
                continue
        self.held = {}
        self.down = set()

    def poll(self):
        if self.xinput is None:
            return False, []
        connected = False
        current = set()
        for slot in range(4):
            st = _XState()
            if self.xinput.XInputGetState(slot, ctypes.byref(st)) != 0:
                continue
            connected = True
            p = st.pad
            for mask, name in self.BUTTONS.items():
                if p.buttons & mask:
                    current.add(name)
            if p.lx < -self.STICK: current.add("LEFT")
            if p.lx > self.STICK: current.add("RIGHT")
            if p.ly > self.STICK: current.add("UP")
            if p.ly < -self.STICK: current.add("DOWN")
        self.down = current
        now = time.monotonic()
        pressed = []
        for name in current:
            if name not in self.held:
                self.held[name] = now + 0.35
                pressed.append(name)
            elif name in ("UP", "DOWN", "LEFT", "RIGHT") and now >= self.held[name]:
                self.held[name] = now + 0.12
                pressed.append(name)
        for name in list(self.held):
            if name not in current:
                del self.held[name]
        return connected, pressed


# ---------------------------------------------------------------- arte (capas)


def _font(size, bold=True):
    for name in (("segoeuib.ttf", "seguisb.ttf") if bold else ("segoeui.ttf",)):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _rounded(img, radius, bg):
    w, h = img.size
    mask = Image.new("L", (w * 3, h * 3), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * 3 - 1, h * 3 - 1], radius * 3, fill=255)
    out = Image.new("RGB", (w, h), _hex(bg))
    out.paste(img, (0, 0), mask.resize((w, h), Image.LANCZOS))
    return out


def _badge(img, text, x, y, fill, fg=(0, 0, 0)):
    d = ImageDraw.Draw(img)
    f = _font(11)
    tw = d.textlength(text, font=f)
    d.rounded_rectangle([x, y, x + tw + 12, y + 18], 4, fill=fill)
    d.text((x + 6, y + 1), text, font=f, fill=fg)


class Art:
    def __init__(self):
        self.cache = {}

    def clear(self):
        self.cache.clear()

    def base(self, g):
        if g["title_id"]:
            cover = os.path.join(COVERS_DIR, g["title_id"] + ".jpg")
            if os.path.isfile(cover):
                try:
                    return Image.open(cover).convert("RGB"), True
                except OSError:
                    pass
        if g["icon"]:
            try:
                return Image.open(io.BytesIO(g["icon"])).convert("RGB"), False
            except OSError:
                pass
        # sem capa nem icone: gradiente verde com as iniciais
        img = Image.new("RGB", (256, 256))
        top, bottom = _hex("#1f5f1f"), _hex("#0a1a0a")
        d = ImageDraw.Draw(img)
        for y in range(256):
            t = y / 255
            d.line([(0, y), (255, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
        words = [w for w in g["title"].replace("-", " ").split() if w[:1].isalnum()]
        if words and len(words[0]) <= 3 and words[0].isupper():
            initials = words[0]
        else:
            initials = "".join(w[0] for w in words[:2]).upper() or "360"
        f = _font(96)
        bb = d.textbbox((0, 0), initials, font=f)
        d.text(((256 - bb[2] - bb[0]) / 2, (256 - bb[3] - bb[1]) / 2), initials, font=f, fill=(255, 255, 255))
        return img, False

    def card(self, g, focused, state=""):
        key = ("card", g["path"], focused, state)
        if key in self.cache:
            return self.cache[key]
        img, cover = self.base(g)
        W, H = CARD_W, CARD_H
        if cover:
            art = ImageOps.fit(img, (W, H), Image.LANCZOS)
        else:
            bg = img.resize((H, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(14))
            art = ImageEnhance.Brightness(bg).enhance(0.55).crop(((H - W) // 2, 0, (H - W) // 2 + W, H))
            mask = Image.new("L", (100, 100), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, 99, 99], 12, fill=255)
            art.paste(img.resize((100, 100), Image.LANCZOS), ((W - 100) // 2, (H - 100) // 2 - 6), mask)
        if is_kinect(g):
            _badge(art, "KINECT", 8, 8, _hex(WARN))
        if state == "hd":
            _badge(art, "NO HD", 8, H - 28, (20, 20, 20), (255, 255, 255))
        elif state == "installed":
            _badge(art, "INSTALADO", 8, H - 28, _hex(ACCENT), (255, 255, 255))
        elif state == "queued":
            _badge(art, "INSTALANDO…", 8, H - 28, (255, 255, 255))
        art = _rounded(art, 8, BG)
        if focused:
            ImageDraw.Draw(art).rounded_rectangle([1, 1, W - 2, H - 2], 8, outline=_hex(RING), width=3)
        photo = ImageTk.PhotoImage(art)
        self.cache[key] = photo
        return photo

    def hero(self, g, W, H):
        key = ("hero", g["path"], W, H)
        if key in self.cache:
            return self.cache[key]
        img, cover = self.base(g)
        src = img if cover or g["icon"] else img.crop((0, 0, 8, 256))
        bg = ImageOps.fit(src, (max(64, W // 8), max(64, H // 8)), Image.BICUBIC)
        bg = bg.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(6))
        bg = ImageEnhance.Color(ImageEnhance.Brightness(bg).enhance(0.6)).enhance(1.3)
        shade = ImageOps.invert(Image.linear_gradient("L").rotate(90).resize((W, H))).point(lambda v: int(v * 0.85))
        bg = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), bg, shade)
        fade_h = min(110, H // 3)
        bg.paste(Image.new("RGB", (W, fade_h), _hex(BG)), (0, H - fade_h),
                 Image.linear_gradient("L").resize((W, fade_h)))
        if cover:
            ph = int(H * 0.72)
            pw, r = int(ph * 0.73), 10
            panel = ImageOps.fit(img, (pw, ph), Image.LANCZOS)
        else:
            pw = ph = int(H * 0.5)
            r = 16
            panel = img.resize((pw, ph), Image.LANCZOS)
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1], r, fill=255)
        if W > 760:
            bg.paste(panel, (W - pw - 72, (H - ph) // 2 - 10), mask)
        photo = ImageTk.PhotoImage(bg)
        self.cache[key] = photo
        return photo

    def small(self, g, size=30):
        key = ("small", g["path"], size)
        if key not in self.cache:
            img, _ = self.base(g)
            self.cache[key] = ImageTk.PhotoImage(_rounded(ImageOps.fit(img, (size, size), Image.LANCZOS), 5, SIDEBAR))
        return self.cache[key]


class NavItem:
    def __init__(self, widget, focus, activate, hcanvas=None):
        self.widget = widget
        self.focus = focus
        self.activate = activate
        self.hcanvas = hcanvas


# ---------------------------------------------------------------- app


class Launcher(tk.Tk):
    PAGES = ("home", "library", "hd", "pc", "settings")

    def __init__(self):
        super().__init__()
        self.title("Biblioteca 360")
        self.geometry("1280x760")
        self.minsize(900, 560)
        self.configure(bg=BG)
        fams = set(tkfont.families(self))
        self.F = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI"
        self.FT = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"

        os.makedirs(DATA_DIR, exist_ok=True)
        self.cfg = load_config()
        save_config(self.cfg)
        self.games = []
        self.art = Art()
        self.proc = None
        self.in_game = False
        self.game_hwnd = None
        self.current = ("home", {})
        self.history = []
        self.nav_rows = []
        self.nav_pos = None
        self.lib_filter = "Todos"
        self.hd_filter = "Todos"
        self._last_w = 0
        self._resize_job = None
        self._toast_job = None

        # HD do Xbox / instalacao
        self.hd_disks = None
        self.hd_disk = None
        self.hd_list = None
        self.hd_reading = False
        self.queue = []
        self.job = None
        self.download = None
        self.pc = None

        self._build_shell()
        self._build_game_view()

        self.bind_all("<MouseWheel>", self._on_wheel)
        for key, a in (("<Left>", (0, -1)), ("<Right>", (0, 1)), ("<Up>", (-1, 0)), ("<Down>", (1, 0))):
            self.bind(key, lambda e, a=a: self._key_nav(*a))
        self.bind("<Return>", self._key_activate)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Escape>", self._on_escape)
        self.bind("<Alt-Left>", lambda e: self.go_back())
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.pad = Gamepad()
        self.pad_connected = None
        self.after(30, self.refresh)
        self.after(200, self._poll_pad)
        self.after(400, self.detect_hd)
        self.after(600, lambda: threading.Thread(target=self._load_pc_info, daemon=True).start())

    # ------------------------------------------------------------ janela principal

    def _build_shell(self):
        self.shell = tk.Frame(self, bg=BG)
        self.shell.pack(fill="both", expand=True)

        sb = tk.Frame(self.shell, bg=SIDEBAR, width=SIDEBAR_W)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        brand = tk.Frame(sb, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(20, 18))
        logo = tk.Canvas(brand, width=30, height=30, bg=SIDEBAR, highlightthickness=0)
        logo.create_oval(1, 1, 29, 29, fill=ACCENT, outline="")
        logo.create_text(15, 15, text="360", fill=FG, font=(self.F, 8, "bold"))
        logo.pack(side="left")
        tk.Label(brand, text="Biblioteca 360", fg=FG, bg=SIDEBAR, font=(self.F, 13, "bold")).pack(side="left", padx=10)

        self.side_items = {}
        for key, icon, text in (("home", "⌂", "Início"), ("library", "▦", "Minha biblioteca"),
                                ("hd", "⛁", "HD do Xbox"), ("pc", "🖥", "Este PC"),
                                ("settings", "⚙", "Configurações")):
            self.side_items[key] = self._side_item(sb, icon, text, lambda k=key: self.show_page(k))

        tk.Frame(sb, bg=SURFACE_HI, height=1).pack(fill="x", padx=18, pady=(14, 12))
        tk.Label(sb, text="JOGADOS RECENTEMENTE", fg=MUTED, bg=SIDEBAR, anchor="w",
                 font=(self.FT, 8, "bold")).pack(fill="x", padx=20, pady=(0, 6))
        self.recent_box = tk.Frame(sb, bg=SIDEBAR)
        self.recent_box.pack(fill="x")

        self.side_status = tk.Label(sb, text="", fg=MUTED, bg=SIDEBAR, anchor="w", justify="left",
                                    font=(self.FT, 9), wraplength=SIDEBAR_W - 36)
        self.side_status.pack(side="bottom", fill="x", padx=18, pady=(6, 16))
        # progresso da instalacao (aparece so durante a copia)
        self.job_box = tk.Frame(sb, bg=SURFACE)
        self.job_lbl = tk.Label(self.job_box, text="", fg=FG, bg=SURFACE, anchor="w", justify="left",
                                font=(self.FT, 9), wraplength=SIDEBAR_W - 48)
        self.job_lbl.pack(fill="x", padx=12, pady=(10, 6))
        self.job_bar = tk.Canvas(self.job_box, height=4, bg=SURFACE_HI, highlightthickness=0)
        self.job_bar.pack(fill="x", padx=12, pady=(0, 12))

        main = self.main = tk.Frame(self.shell, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        top = tk.Frame(main, bg=BG, height=60)
        top.pack(fill="x")
        top.pack_propagate(False)
        self.back_btn = tk.Label(top, text="←", fg=MUTED, bg=BG, font=(self.F, 16), cursor="hand2")
        self.back_btn.pack(side="left", padx=(18, 8))
        self.back_btn.bind("<Button-1>", lambda e: self.go_back())
        search = tk.Frame(top, bg=SURFACE)
        search.pack(side="left", pady=12, ipady=2)
        tk.Label(search, text="⌕", fg=MUTED, bg=SURFACE, font=(self.F, 13)).pack(side="left", padx=(12, 4))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(search, textvariable=self.search_var, bg=SURFACE, fg=FG,
                                     insertbackground=FG, relief="flat", font=(self.FT, 11), width=38)
        self.search_entry.pack(side="left", padx=(0, 12), ipady=3)
        self._placeholder(self.search_entry, "Pesquisar jogos")
        self.search_var.trace_add("write", lambda *a: self._on_search())
        self.pad_lbl = tk.Label(top, text="", fg=MUTED, bg=BG, font=(self.FT, 10))
        self.pad_lbl.pack(side="right", padx=20)

        wrap = tk.Frame(main, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.vcanvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, yscrollincrement=40)
        self.vcanvas.pack(fill="both", expand=True)
        self.page = tk.Frame(self.vcanvas, bg=BG)
        self._page_win = self.vcanvas.create_window((0, 0), window=self.page, anchor="nw")
        self.page.bind("<Configure>", lambda e: self.vcanvas.configure(scrollregion=self.vcanvas.bbox("all")))
        self.vcanvas.bind("<Configure>", self._on_canvas_resize)
        self.toast_lbl = tk.Label(main, text="", fg=FG, bg=SURFACE_HI, font=(self.FT, 10), padx=16, pady=8)

    def _side_item(self, parent, icon, text, cmd, image=None):
        row = tk.Frame(parent, bg=SIDEBAR, cursor="hand2")
        row.pack(fill="x", padx=10, pady=1)
        bar = tk.Frame(row, bg=SIDEBAR, width=3)
        bar.pack(side="left", fill="y", pady=8)
        if image is not None:
            ic = tk.Label(row, image=image, bg=SIDEBAR)
            ic.pack(side="left", padx=(9, 10), pady=5)
        else:
            ic = tk.Label(row, text=icon, fg=FG, bg=SIDEBAR, font=(self.F, 12), width=2)
            ic.pack(side="left", padx=(8, 6), pady=7)
        lb = tk.Label(row, text=text, fg=FG, bg=SIDEBAR, anchor="w", font=(self.FT, 10))
        lb.pack(side="left", fill="x", expand=True)
        parts = (row, ic, lb)
        state = {"active": False}

        def paint(color):
            for w in parts:
                w.configure(bg=color)

        def set_active(on):
            state["active"] = on
            paint(SURFACE if on else SIDEBAR)
            bar.configure(bg=ACCENT if on else SIDEBAR)

        for w in parts:
            w.bind("<Button-1>", lambda e: cmd())
            w.bind("<Enter>", lambda e: None if state["active"] else paint(SURFACE))
            w.bind("<Leave>", lambda e: None if state["active"] else paint(SIDEBAR))
        return set_active

    def _placeholder(self, entry, text):
        def show(_=None):
            if not self.search_var.get():
                entry._ph = True
                entry.configure(fg=MUTED)
                entry.insert(0, text)

        def hide(_=None):
            if getattr(entry, "_ph", False):
                entry._ph = False
                entry.delete(0, "end")
                entry.configure(fg=FG)

        entry.bind("<FocusIn>", hide)
        entry.bind("<FocusOut>", show)
        entry._ph = False
        show()

    def _query(self):
        return "" if getattr(self.search_entry, "_ph", False) else self.search_var.get().strip()

    def _ring_frame(self, parent):
        pbg = parent.cget("bg")
        f = tk.Frame(parent, bg=pbg)
        f._pbg = pbg
        return f

    def _button(self, parent, text, cmd, primary=False, bg=None, state="normal"):
        base = ACCENT if primary else (bg or SURFACE_HI)
        hover = ACCENT_HI if primary else "#404040"
        f = self._ring_frame(parent)
        b = tk.Button(f, text=text, command=cmd, bg=base, fg=FG, relief="flat", bd=0, state=state,
                      activebackground=hover, activeforeground=FG, cursor="hand2", disabledforeground="#cfcfcf",
                      font=(self.F, 11, "bold") if primary else (self.FT, 10),
                      padx=22 if primary else 16, pady=8 if primary else 6)
        b.pack(padx=2, pady=2)
        b.bind("<Enter>", lambda e: b.configure(bg=hover) if str(b["state"]) == "normal" else None)
        b.bind("<Leave>", lambda e: b.configure(bg=base))
        f.inner = b
        return f

    def _btn_nav(self, f):
        return NavItem(f, lambda on: f.configure(bg=RING if on else f._pbg), f.inner.invoke)

    # ------------------------------------------------------------ dados

    def scan_dirs(self):
        return list(dict.fromkeys(self.cfg["game_dirs"] + [self.cfg["install_dir"]]))

    def refresh(self):
        self.games = scan_games(self.scan_dirs()) + scan_store_games()
        self.art.clear()
        self.refresh_sidebar()
        if not self.in_game:
            self.render_current()

    def refresh_sidebar(self):
        for w in self.recent_box.winfo_children():
            w.destroy()
        recent = self.recent_games()[:5]
        if not recent:
            tk.Label(self.recent_box, text="Nenhum jogo ainda", fg=MUTED, bg=SIDEBAR, anchor="w",
                     font=(self.FT, 9)).pack(fill="x", padx=20)
        for g in recent:
            title = g["title"] if len(g["title"]) <= 24 else g["title"][:23] + "…"
            self._side_item(self.recent_box, "", title, lambda g=g: self.open_game(g), image=self.art.small(g))
        ok = self.xenia_ok()
        hd = ""
        if self.hd_disk:
            n = len(self.hd_list["games"]) if self.hd_list else 0
            hd = f"\n● HD do Xbox: {n} jogos" if n else "\n● HD do Xbox conectado"
        self.side_status.configure(
            text=(f"● Xenia pronto\n● {len(self.games)} jogos instalados" if ok
                  else "● Xenia não instalado\nVeja em Configurações") + hd,
            fg=MUTED if ok else WARN)

    def xenia_ok(self):
        x = self.cfg.get("xenia_path")
        return bool(x and os.path.isfile(x))

    def xemu_ok(self):
        x = self.cfg.get("xemu_path")
        return bool(x and os.path.isfile(x))

    def xemu_missing(self):
        """Arquivos do Xbox original que ainda faltam para o xemu ligar."""
        return [label for key, label in (("xbox_bios", "BIOS (flash)"), ("xbox_mcpx", "MCPX (boot ROM)"),
                                         ("xbox_hdd", "HD (.qcow2)"))
                if not (self.cfg.get(key) and os.path.isfile(self.cfg[key]))]

    def recent_games(self):
        by_path = {g["path"]: g for g in self.games}
        return [by_path[p] for p in self.cfg.get("recent", []) if p in by_path]

    def installed_pkgs(self):
        return {os.path.basename(g["path"]).upper() for g in self.games}

    def hd_games(self):
        """Jogos do HD do Xbox no formato usado pela UI."""
        if not self.hd_list:
            return []
        inst = self.installed_pkgs()
        queued = {q["path"] for q in self.queue}
        if self.job and self.job["kind"] == "copy":
            queued |= {it["path"] for it in self.job["items"]}
        out = []
        for h in self.hd_list["games"]:
            icon = base64.b64decode(h["icon"]) if h.get("icon") else None
            state = "installed" if h["pkg"].upper() in inst else "queued" if h["path"] in queued else "hd"
            out.append({"title": h["title"], "title_id": h["title_id"], "icon": icon, "path": "hd:" + h["path"],
                        "kind": "HD", "group": "HD do Xbox", "size": h["size"], "hd": h, "state": state,
                        "type": h["type"]})
        return out

    def _load_pc_info(self):
        info = pc_info()
        self.after(0, lambda: self._set_pc(info))

    def _set_pc(self, info):
        self.pc = info
        if self.current[0] == "pc" and not self.in_game:
            self.render_current()

    # ------------------------------------------------------------ navegacao entre paginas

    def show_page(self, name, push=True, **kw):
        if self.in_game:
            return
        if push and (name, kw) != self.current:
            self.history = (self.history + [self.current])[-30:]
        self.current = (name, kw)
        if name not in ("library", "hd") and self._query():
            self.search_entry.delete(0, "end")
            self.focus_set()
            self.search_entry.event_generate("<FocusOut>")
        self.render_current()

    def go_back(self):
        if not self.in_game and self.history:
            self.current = self.history.pop()
            self.render_current()

    def render_current(self):
        name, kw = self.current
        for w in self.page.winfo_children():
            w.destroy()
        self.vcanvas.yview_moveto(0)
        for key, set_active in self.side_items.items():
            set_active(key == name)
        self.back_btn.configure(fg=FG if self.history else SURFACE_HI)
        self._last_w = self.content_w()
        self.set_nav(getattr(self, "page_" + name)(**kw))

    def content_w(self):
        w = self.vcanvas.winfo_width()
        return w if w > 50 else self.winfo_width() - SIDEBAR_W

    # ------------------------------------------------------------ blocos de interface

    def _title(self, parent, text, sub=""):
        tk.Label(parent, text=text, fg=FG, bg=BG, anchor="w", font=(self.F, 24, "bold")).pack(
            fill="x", padx=PAD_X, pady=(18, 0))
        if sub:
            tk.Label(parent, text=sub, fg=MUTED, bg=BG, anchor="w", font=(self.FT, 10)).pack(
                fill="x", padx=PAD_X, pady=(2, 0))

    def _section(self, parent, title, desc="", pad_top=24):
        tk.Label(parent, text=title, fg=FG, bg=BG, anchor="w", font=(self.F, 15, "bold")).pack(
            fill="x", padx=PAD_X, pady=(pad_top, 10))
        box = tk.Frame(parent, bg=SURFACE)
        box.pack(fill="x", padx=PAD_X)
        if desc:
            tk.Label(box, text=desc, fg=MUTED, bg=SURFACE, anchor="w", justify="left", wraplength=780,
                     font=(self.FT, 10)).pack(fill="x", padx=20, pady=(16, 8))
        return box

    def _btnbar(self, box, buttons):
        bar = tk.Frame(box, bg=box.cget("bg"))
        bar.pack(fill="x", padx=18, pady=(8, 16))
        items = []
        for spec in buttons:
            text, cmd = spec[0], spec[1]
            primary = len(spec) > 2 and spec[2]
            b = self._button(bar, text, cmd, primary=primary)
            b.pack(side="left", padx=(0, 6))
            items.append(self._btn_nav(b))
        return items

    def _kv(self, box, fields):
        grid = tk.Frame(box, bg=SURFACE)
        grid.pack(fill="x", padx=20, pady=14)
        for i, (k, v, *color) in enumerate(fields):
            tk.Label(grid, text=k, fg=MUTED, bg=SURFACE, anchor="w", font=(self.FT, 10)).grid(
                row=i, column=0, sticky="nw", padx=(0, 30), pady=3)
            tk.Label(grid, text=v, fg=color[0] if color else FG, bg=SURFACE, anchor="w", font=(self.FT, 10),
                     wraplength=680, justify="left").grid(row=i, column=1, sticky="w", pady=3)

    # ------------------------------------------------------------ pagina: inicio

    def page_home(self):
        p = self.page
        hd = [g for g in self.hd_games() if g["state"] != "installed"]
        if not self.games and not hd:
            return self._empty_state(p)
        recent = self.recent_games()
        rows = []
        if recent or self.games:
            g = recent[0] if recent else self.games[0]
            rows.append(self._hero(p, g, label="CONTINUAR JOGANDO" if recent else "EM DESTAQUE"))
        else:
            rows.append(self._hero(p, hd[0], label="NO SEU HD DO XBOX"))
        if recent:
            rows.append(self._row(p, "Jogados recentemente", recent[:12]))
        if hd:
            rows.append(self._row(p, "No HD do Xbox · prontos para instalar", hd))
        groups = {}
        for g in self.games:
            groups.setdefault(g["group"], []).append(g)
        for name, lst in groups.items():
            rows.append(self._row(p, name, lst))
        tk.Frame(p, bg=BG, height=40).pack()
        return rows

    def _empty_state(self, p):
        box = tk.Frame(p, bg=BG)
        box.pack(pady=110)
        tk.Label(box, text="Sua biblioteca está vazia", fg=FG, bg=BG, font=(self.F, 20, "bold")).pack()
        tk.Label(box, text="Conecte o HD do Xbox 360 na USB para instalar jogos,\n"
                           "ou adicione uma pasta com jogos nas configurações.",
                 fg=MUTED, bg=BG, font=(self.FT, 11), justify="center").pack(pady=(6, 18))
        bar = tk.Frame(box, bg=BG)
        bar.pack()
        b1 = self._button(bar, "Abrir HD do Xbox", lambda: self.show_page("hd"), primary=True)
        b1.pack(side="left", padx=4)
        b2 = self._button(bar, "Configurações", lambda: self.show_page("settings"))
        b2.pack(side="left", padx=4)
        return [[self._btn_nav(b1), self._btn_nav(b2)]]

    def _hero(self, parent, g, label="EM DESTAQUE", detail=False):
        W = max(600, self.content_w())
        H = 400 if detail else 360
        c = tk.Canvas(parent, width=W, height=H, bg=BG, highlightthickness=0)
        c.pack(fill="x")
        c.create_image(0, 0, image=self.art.hero(g, W, H), anchor="nw")
        x = PAD_X + 8
        t = c.create_text(x, H - 132, text=g["title"], fill=FG, anchor="sw",
                          width=int(W * 0.5) if W > 760 else W - 2 * x,
                          font=(self.F, 30 if len(g["title"]) < 26 else 24, "bold"))
        c.create_text(x, c.bbox(t)[1] - 6, text=label, fill=MUTED, anchor="sw", font=(self.FT, 9, "bold"))
        if g["kind"] == "HD":
            sub = f"Xbox 360  ·  {fatx.GAME_TYPES.get(g['type'], '')}  ·  {fmt_size(g['size'])}"
        else:
            sub = f"{platform_name(g)}  ·  {g['group']}"
        if is_kinect(g):
            sub += "  ·  Requer Kinect (não suportado no PC)"
        c.create_text(x, H - 112, text=sub, fill=WARN if is_kinect(g) else "#d0d0d0", anchor="w",
                      font=(self.FT, 10))
        btns = self._game_buttons(c, g, detail)
        bx = x
        for b in btns:
            c.create_window(bx, H - 66, window=b, anchor="w")
            c.update_idletasks()
            bx += b.winfo_reqwidth() + 8
        return [self._btn_nav(b) for b in btns if str(b.inner["state"]) == "normal"]

    def _game_buttons(self, parent, g, detail):
        if g["kind"] == "HD":
            st = g["state"]
            if st == "installed":
                local = self.local_for(g)
                btns = [self._button(parent, "▶   Jogar", lambda: self.launch(local), primary=True)]
            elif st == "queued":
                pct = self.job_percent() if self.job and self.job["kind"] == "copy" else 0
                btns = [self._button(parent, f"Instalando…  {pct}%", None, primary=True, state="disabled"),
                        self._button(parent, "Cancelar", lambda: self.cancel_install(g))]
            else:
                btns = [self._button(parent, f"⭳   Instalar  ({fmt_size(g['size'])})",
                                     lambda: self.install(g), primary=True)]
            if not detail:
                btns.append(self._button(parent, "Detalhes do jogo", lambda: self.open_game(g)))
            else:
                btns.append(self._button(parent, "Compatibilidade", lambda: self.open_compat(g)))
            return btns
        btns = [self._button(parent, "▶   Jogar", lambda: self.launch(g), primary=True)]
        if detail:
            if g["kind"] not in ("XBOX", "PC"):
                btns.append(self._button(parent, f"Vídeo: {self.video_mode(g)[1]}", lambda: self.next_video_mode(g)))
                btns.append(self._button(parent, "Compatibilidade", lambda: self.open_compat(g)))
            btns.append(self._button(parent, "Abrir local", lambda: self.open_folder(g)))
        else:
            btns.append(self._button(parent, "Detalhes do jogo", lambda: self.open_game(g)))
        return btns

    def _row(self, parent, title, games):
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", padx=PAD_X, pady=(26, 10))
        tk.Label(head, text=title, fg=FG, bg=BG, font=(self.F, 15, "bold")).pack(side="left")
        tk.Label(head, text=f"  {len(games)}", fg=MUTED, bg=BG, font=(self.FT, 10)).pack(side="left", pady=(4, 0))
        hc = tk.Canvas(parent, bg=BG, highlightthickness=0, height=CARD_H + 50, xscrollincrement=CARD_W + 14)
        hc.pack(fill="x", padx=(PAD_X, 0))
        inner = tk.Frame(hc, bg=BG)
        hc.create_window(0, 0, window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: hc.configure(scrollregion=hc.bbox("all")))
        hc._hrow = True
        hc._inner = inner
        for sym, step in (("›", 3), ("‹", -3)):
            a = tk.Label(head, text=sym, fg=FG, bg=SURFACE, font=(self.F, 14), width=2, cursor="hand2")
            a.pack(side="right", padx=(6, 0))
            a.bind("<Button-1>", lambda e, s=step: hc.xview_scroll(s, "units"))
        items = []
        for g in games:
            item = self._card(inner, g)
            item.widget.pack(side="left", padx=(0, 14), anchor="n")
            item.hcanvas = hc
            items.append(item)
        return items

    def _card(self, parent, g):
        f = tk.Frame(parent, bg=BG)
        state = g.get("state", "") if g["kind"] == "HD" else ""
        img = tk.Label(f, image=self.art.card(g, False, state), bg=BG, bd=0, cursor="hand2")
        img.pack()
        title = g["title"] if len(g["title"]) <= 44 else g["title"][:43] + "…"
        t = tk.Label(f, text=title, fg=FG, bg=BG, font=(self.FT, 10), wraplength=CARD_W,
                     justify="left", anchor="nw", height=2)
        t.pack(fill="x", pady=(7, 0))
        nav = {"on": False}

        def show(on):
            img.configure(image=self.art.card(g, on, state))

        def nav_focus(on):
            nav["on"] = on
            show(on)

        for w in (img, t):
            w.bind("<Enter>", lambda e: show(True))
            w.bind("<Leave>", lambda e: show(nav["on"]))
            w.bind("<Button-1>", lambda e: self.open_game(g))
            if g["kind"] != "HD":
                w.bind("<Double-Button-1>", lambda e: self.launch(g))
        return NavItem(f, nav_focus, lambda: self.open_game(g))

    def _grid(self, parent, games):
        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="x", padx=PAD_X)
        cols = max(1, (self.content_w() - 2 * PAD_X + 14) // (CARD_W + 14))
        rows, row = [], []
        for i, g in enumerate(games):
            item = self._card(grid, g)
            item.widget.grid(row=i // cols, column=i % cols, padx=(0, 14), pady=(0, 14), sticky="n")
            row.append(item)
            if len(row) == cols:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        tk.Frame(parent, bg=BG, height=30).pack()
        return rows

    def _chips(self, parent, names, selected, on_pick):
        chips = tk.Frame(parent, bg=BG)
        chips.pack(fill="x", padx=PAD_X, pady=(12, 4))
        items = []
        for name in names:
            sel = name == selected
            f = self._ring_frame(chips)
            b = tk.Button(f, text=name, bg=FG if sel else SURFACE, fg="#000000" if sel else FG,
                          activebackground=FG, activeforeground="#000000", relief="flat", bd=0,
                          font=(self.FT, 10), padx=14, pady=4, cursor="hand2", command=lambda n=name: on_pick(n))
            b.pack(padx=2, pady=2)
            f.inner = b
            f.pack(side="left", padx=(0, 6))
            items.append(self._btn_nav(f))
        return items

    # ------------------------------------------------------------ pagina: biblioteca

    def page_library(self):
        p = self.page
        q = self._query().lower()
        self._title(p, f'Resultados para "{self._query()}"' if q else "Minha biblioteca")
        games = self.games
        rows = []
        if q:
            games = [g for g in games if q in g["title"].lower() or q in g["group"].lower()]
            hd = [g for g in self.hd_games() if g["state"] != "installed" and q in g["title"].lower()]
        else:
            groups = ["Todos"] + sorted({g["group"] for g in self.games}, key=str.lower)
            if self.lib_filter not in groups:
                self.lib_filter = "Todos"
            rows.append(self._chips(p, groups, self.lib_filter, self._set_lib_filter))
            if self.lib_filter != "Todos":
                games = [g for g in games if g["group"] == self.lib_filter]
            hd = []
        tk.Label(p, text=f"{len(games)} jogos instalados", fg=MUTED, bg=BG, anchor="w",
                 font=(self.FT, 10)).pack(fill="x", padx=PAD_X, pady=(8, 12))
        if not games and not hd:
            tk.Label(p, text="Nada encontrado.", fg=MUTED, bg=BG, font=(self.FT, 12)).pack(pady=40)
        rows += self._grid(p, games)
        if hd:
            tk.Label(p, text=f"No HD do Xbox ({len(hd)})", fg=FG, bg=BG, anchor="w",
                     font=(self.F, 15, "bold")).pack(fill="x", padx=PAD_X, pady=(10, 12))
            rows += self._grid(p, hd)
        return rows

    def _set_lib_filter(self, name):
        self.lib_filter = name
        self.render_current()

    def _on_search(self):
        if getattr(self.search_entry, "_ph", False):
            return
        if self._query():
            if self.current[0] != "library":
                self.history.append(self.current)
                self.current = ("library", {})
            self.render_current()
        elif self.current[0] == "library":
            self.render_current()

    # ------------------------------------------------------------ pagina: jogo

    def open_game(self, g):
        self.show_page("game", path=g["path"])

    def find_game(self, path):
        pool = self.hd_games() if path.startswith("hd:") else self.games
        return next((x for x in pool if x["path"] == path), None)

    def local_for(self, hd_game):
        pkg = hd_game["hd"]["pkg"].upper()
        return next((g for g in self.games if os.path.basename(g["path"]).upper() == pkg), None)

    def page_game(self, path):
        g = self.find_game(path)
        p = self.page
        if g is None:
            tk.Label(p, text="Jogo não encontrado.", fg=MUTED, bg=BG, font=(self.FT, 12)).pack(pady=80)
            return []
        rows = [self._hero(p, g, label="HD DO XBOX" if g["kind"] == "HD" else platform_name(g).upper(), detail=True)]
        box = self._section(p, "Sobre este jogo", pad_top=10)
        if g["kind"] == "HD":
            h = g["hd"]
            st = {"installed": ("Instalado no PC", ACCENT_HI), "queued": ("Instalando…", WARN),
                  "hd": ("Só no HD do Xbox — instale para jogar", MUTED)}[g["state"]]
            self._kv(box, [("Situação", st[0], st[1]),
                           ("Tipo", fatx.GAME_TYPES.get(h["type"], h["type"])),
                           ("Title ID", h["title_id"]), ("Tamanho", fmt_size(h["size"])),
                           ("No HD do Xbox", h["path"]),
                           ("Será instalado em", self.dest_dir(h))])
        else:
            last = self.cfg.get("last_played", {}).get(g["path"])
            fmt = {"GOD": "Games on Demand (GOD)", "XBLA": "Xbox Live Arcade",
                   "XEX": "Pasta extraída (default.xex)", "ISO": "Imagem de disco (.iso)",
                   "XBOX": "Imagem de disco (.iso)", "PC": "Versão para PC (app Xbox)"}[g["kind"]]
            plat = {"XBOX": "Xbox original (via xemu)",
                    "PC": "Xbox One / Series (abre pelo app Xbox)"}.get(g["kind"], "Xbox 360 (via Xenia Canary)")
            self._kv(box, [("Plataforma", plat), ("Formato", fmt),
                           ("Title ID", g["title_id"] or "—"), ("Coleção", g["group"]),
                           ("Jogado pela última vez",
                            time.strftime("%d/%m/%Y %H:%M", time.localtime(last)) if last else "Nunca"),
                           ("Local", g["path"])])
        if is_kinect(g):
            tk.Label(p, text="⚠  Este jogo usa Kinect. Os emuladores não emulam o sensor, então provavelmente não vai funcionar.",
                     fg=WARN, bg=BG, anchor="w", font=(self.FT, 10)).pack(fill="x", padx=PAD_X, pady=(12, 0))
        if g["kind"] != "HD":
            more = [x for x in self.games if x["group"] == g["group"] and x is not g]
            if more:
                rows.append(self._row(p, f"Mais de {g['group']}", more))
        tk.Frame(p, bg=BG, height=40).pack()
        return rows

    # ------------------------------------------------------------ pagina: HD do Xbox

    def page_hd(self):
        p = self.page
        self._title(p, "HD do Xbox", "Instale no PC os jogos do HD ou pendrive do seu Xbox 360.")
        rows = []
        box = self._section(p, "Disco", pad_top=18)
        if self.hd_disks is None:
            tk.Label(box, text="Procurando o HD do Xbox…", fg=MUTED, bg=SURFACE, anchor="w",
                     font=(self.FT, 10)).pack(fill="x", padx=20, pady=16)
        elif not self.hd_disks:
            tk.Label(box, text="Nenhum HD do Xbox encontrado. Ligue o HD (ou pendrive do Xbox) na USB do PC "
                               "com um adaptador SATA-USB e clique em Procurar.",
                     fg=MUTED, bg=SURFACE, anchor="w", justify="left", wraplength=780,
                     font=(self.FT, 10)).pack(fill="x", padx=20, pady=(16, 4))
            rows.append(self._btnbar(box, [("Procurar novamente", self.detect_hd)]))
        else:
            d = self.hd_disk
            read = self.hd_list.get("read_at") if self.hd_list else None
            fields = [("Disco", f"{d['name']}  ·  {fmt_size(d['size'])}  ·  {d['bus']}"),
                      ("Lista de jogos",
                       time.strftime("lida em %d/%m/%Y %H:%M", time.localtime(read)) if read else "ainda não lida",
                       None if read else WARN)]
            free = self.free_space()
            fields.append(("Instalar em", f"{self.cfg['install_dir']}  ·  {fmt_size(free)} livres"))
            self._kv(box, [f if f[-1] is not None else f[:2] for f in fields])
            if self.hd_reading:
                tk.Label(box, text="Lendo o HD… (aceite o pedido de permissão do Windows)", fg=WARN, bg=SURFACE,
                         anchor="w", font=(self.FT, 10)).pack(fill="x", padx=20, pady=(0, 16))
            else:
                btns = [("Ler jogos do HD" if not read else "Ler novamente", self.read_hd, not read),
                        ("Alterar pasta de instalação", self.pick_install_dir)]
                if len(self.hd_disks) > 1:
                    btns.append(("Trocar disco", self.next_disk))
                rows.append(self._btnbar(box, btns))
            tk.Label(box, text="Ler o HD precisa de permissão de administrador (o Windows vai perguntar). "
                               "O HD é aberto somente para leitura — nada é apagado ou alterado no Xbox.",
                     fg=MUTED, bg=SURFACE, anchor="w", justify="left", wraplength=780,
                     font=(self.FT, 9)).pack(fill="x", padx=20, pady=(0, 14))
        games = self.hd_games()
        if games:
            q = self._query().lower()
            if q:
                games = [g for g in games if q in g["title"].lower()]
            names = ["Todos", "Não instalados", "Instalados"]
            rows.append(self._chips(p, names, self.hd_filter, self._set_hd_filter))
            if self.hd_filter == "Não instalados":
                games = [g for g in games if g["state"] != "installed"]
            elif self.hd_filter == "Instalados":
                games = [g for g in games if g["state"] == "installed"]
            total = sum(g["size"] for g in games)
            tk.Label(p, text=f"{len(games)} jogos  ·  {fmt_size(total)}", fg=MUTED, bg=BG, anchor="w",
                     font=(self.FT, 10)).pack(fill="x", padx=PAD_X, pady=(8, 12))
            rows += self._grid(p, games)
        return rows

    def _set_hd_filter(self, name):
        self.hd_filter = name
        self.render_current()

    def next_disk(self):
        i = self.hd_disks.index(self.hd_disk)
        self.select_disk(self.hd_disks[(i + 1) % len(self.hd_disks)])
        self.render_current()

    # ------------------------------------------------------------ pagina: este PC

    def page_pc(self):
        p = self.page
        self._title(p, "Este PC", "Verifica se este computador consegue rodar jogos de Xbox 360 no Xenia.")
        if self.pc is None:
            tk.Label(p, text="Analisando o hardware…", fg=MUTED, bg=BG, font=(self.FT, 12)).pack(pady=60)
            return []
        info = self.pc
        level, title, notes = pc_verdict(info)
        color = {0: BAD, 1: WARN, 2: ACCENT_HI, 3: ACCENT_HI}[level]
        box = self._section(p, "Resultado", pad_top=18)
        head = tk.Frame(box, bg=SURFACE)
        head.pack(fill="x", padx=20, pady=(16, 6))
        meter = tk.Canvas(head, width=120, height=10, bg=SURFACE, highlightthickness=0)
        meter.pack(side="left", padx=(0, 14))
        for i in range(3):
            meter.create_rectangle(i * 40, 0, i * 40 + 36, 10, outline="",
                                   fill=color if i < max(level, 0) else SURFACE_HI)
        tk.Label(head, text=title, fg=color, bg=SURFACE, font=(self.F, 15, "bold")).pack(side="left")
        for n in notes:
            tk.Label(box, text="•  " + n, fg=FG, bg=SURFACE, anchor="w", justify="left", wraplength=780,
                     font=(self.FT, 10)).pack(fill="x", padx=20, pady=2)
        tk.Frame(box, bg=SURFACE, height=12).pack()

        box = self._section(p, "Hardware")
        ok, no = ("Sim", ACCENT_HI), ("Não", BAD)
        self._kv(box, [("Processador", info["cpu"]),
                       ("AVX (obrigatório)", *(ok if info["avx"] else no)),
                       ("AVX2 (recomendado)", *(ok if info["avx2"] else ("Não", WARN))),
                       ("Memória RAM", f"{info['ram_gb']} GB"),
                       ("Placa de vídeo", "\n".join(info["gpus"]) or "—"),
                       ("Sistema", info["windows"] + ("  ·  64 bits" if info["x64"] else "  ·  32 bits")),
                       ("Xenia", self.cfg["xenia_path"] if self.xenia_ok() else "não instalado",
                        FG if self.xenia_ok() else WARN)])

        box = self._section(p, "Para rodar em outro PC")
        tk.Label(box, text="Copie a pasta inteira da Biblioteca 360 (ou o Biblioteca360.exe com a pasta dele) "
                           "para o outro computador. Na primeira vez, abra Configurações → Baixar Xenia. "
                           "Os jogos instalados podem ir junto num HD externo: adicione a pasta deles em "
                           "Configurações → Pastas de jogos.",
                 fg=FG, bg=SURFACE, anchor="w", justify="left", wraplength=780,
                 font=(self.FT, 10)).pack(fill="x", padx=20, pady=16)
        rows = [self._btnbar(box, [("Analisar novamente", self._rescan_pc)])]
        tk.Frame(p, bg=BG, height=40).pack()
        return rows

    def _rescan_pc(self):
        self.pc = None
        self.render_current()
        threading.Thread(target=self._load_pc_info, daemon=True).start()

    # ------------------------------------------------------------ pagina: configuracoes

    def page_settings(self):
        p = self.page
        self._title(p, "Configurações")
        rows = []
        ok = self.xenia_ok()
        box = self._section(p, "Xbox 360 (Xenia)", "Os jogos de Xbox 360 rodam no Xenia Canary, aberto dentro desta janela.")
        tk.Label(box, text="● " + self.cfg["xenia_path"] if ok else "● Xenia não instalado",
                 fg=FG if ok else WARN, bg=SURFACE, anchor="w", font=(self.FT, 10)).pack(fill="x", padx=20)
        btns = [("Baixar Xenia" if not ok else "Atualizar Xenia", self.download_xenia, not ok),
                ("Escolher xenia_canary.exe...", self.pick_xenia)]
        rows.append(self._btnbar(box, btns))

        box = self._section(p, "Perfil do Xenia (Xbox 360)",
                            "Alguns jogos pedem um perfil (Sign In) ao abrir. Escreva aqui o nome do perfil, com o "
                            "controle ou o teclado. No jogo, clique em Create Profile e segure Back + Y: o app "
                            "digita o nome sozinho. Depois é só confirmar com OK. O botão Teclado na barra do jogo "
                            "abre o teclado virtual do Windows (para usar com o mouse).")
        gt = self.cfg.get("gamertag", "")
        self._kv(box, [("Nome do perfil", gt or "ainda não definido", FG if gt else WARN)])
        rows.append(self._btnbar(box, [("Escrever nome do perfil", self.edit_gamertag, not gt)]))

        ok = self.xemu_ok()
        missing = self.xemu_missing()
        box = self._section(p, "Xbox original (xemu)",
                            "Os jogos do primeiro Xbox (imagens .iso) rodam no emulador xemu. Por lei, o xemu não "
                            "vem com o software do console: copie do seu próprio Xbox a BIOS (flash) e a MCPX "
                            "(boot ROM). O HD pode ser um HD vazio, baixado aqui mesmo.")

        def file_row(label, key):
            v = self.cfg.get(key, "")
            return (label, v, FG) if v and os.path.isfile(v) else (label, "não escolhido", WARN)

        self._kv(box, [("xemu", self.cfg["xemu_path"] if ok else "não instalado", FG if ok else WARN),
                       file_row("BIOS (flash)", "xbox_bios"), file_row("MCPX (boot ROM)", "xbox_mcpx"),
                       file_row("HD (.qcow2)", "xbox_hdd")])
        rows.append(self._btnbar(box, [("Baixar xemu" if not ok else "Atualizar xemu", self.download_xemu, not ok),
                                       ("Escolher BIOS...", lambda: self.pick_xbox_file("xbox_bios")),
                                       ("Escolher MCPX...", lambda: self.pick_xbox_file("xbox_mcpx"))]))
        rows.append(self._btnbar(box, [("Escolher HD...", lambda: self.pick_xbox_file("xbox_hdd")),
                                       ("Baixar HD vazio", self.download_xemu_hdd, "HD (.qcow2)" in missing)]))

        box = self._section(p, "Xbox One e Series",
                            "Não existe emulador de Xbox One/Series. Os jogos dessas gerações que a Microsoft lança "
                            "para PC (Xbox Play Anywhere e Game Pass para PC) aparecem na biblioteca depois de "
                            "instalados pelo app Xbox. Com Game Pass Ultimate, muitos outros dá para jogar pela "
                            "nuvem, sem instalar.")
        rows.append(self._btnbar(box, [("Abrir app Xbox", self.open_xbox_app),
                                       ("Jogar na nuvem (Xbox Cloud Gaming)", self.open_cloud)]))

        box = self._section(p, "Pastas de jogos",
                            "Onde a biblioteca procura jogos (Games on Demand, Arcade, default.xex e imagens .iso "
                            "de Xbox 360 e Xbox original).")
        line = tk.Frame(box, bg=SURFACE)
        line.pack(fill="x", padx=20, pady=2)
        tk.Label(line, text="⭳  " + self.cfg["install_dir"] + "   (jogos instalados do HD do Xbox)", fg=FG,
                 bg=SURFACE, anchor="w", font=(self.FT, 10)).pack(side="left")
        b = self._button(line, "Alterar", self.pick_install_dir)
        b.pack(side="right")
        rows.append([self._btn_nav(b)])
        for d in self.cfg["game_dirs"]:
            line = tk.Frame(box, bg=SURFACE)
            line.pack(fill="x", padx=20, pady=2)
            exists = os.path.isdir(d)
            tk.Label(line, text="📁  " + d + ("" if exists else "   (não conectado)"),
                     fg=FG if exists else WARN, bg=SURFACE, anchor="w", font=(self.FT, 10)).pack(side="left")
            rb = self._button(line, "Remover", lambda d=d: self._remove_dir(d))
            rb.pack(side="right")
            rows.append([self._btn_nav(rb)])
        rows.append(self._btnbar(box, [("Adicionar pasta...", self._add_dir),
                                       ("Procurar jogos novamente", self.refresh)]))

        box = self._section(p, "Exibição")
        f = self._ring_frame(box)
        self.fs_var = tk.BooleanVar(value=self.cfg.get("fullscreen", False))
        cb = tk.Checkbutton(f, text="Abrir jogos em tela cheia (F11 alterna, Esc sai)", variable=self.fs_var,
                            command=self._save_fs, bg=SURFACE, fg=FG, selectcolor=SURFACE_HI,
                            activebackground=SURFACE, activeforeground=FG, font=(self.FT, 10), anchor="w")
        cb.pack(padx=2, pady=2)
        f.inner = cb
        f.pack(anchor="w", padx=16, pady=14)
        rows.append([self._btn_nav(f)])

        box = self._section(p, "Capas dos jogos",
                            "Baixa as capas oficiais (download.xbox.com) pelo Title ID. Nem todo jogo tem capa "
                            "disponível; os outros continuam com o ícone do pacote.")
        ids = {g["title_id"] for g in self.games + self.hd_games() if g["title_id"]}
        have = len([t for t in ids if os.path.isfile(os.path.join(COVERS_DIR, t + ".jpg"))])
        self.cover_status = tk.Label(box, text=f"{have} de {len(ids)} jogos com capa", fg=MUTED, bg=SURFACE,
                                     anchor="w", font=(self.FT, 10))
        self.cover_status.pack(fill="x", padx=20)
        rows.append(self._btnbar(box, [("Baixar capas", self.download_covers)]))

        box = self._section(p, "Controle",
                            "Funciona com controles Xbox (XInput): com fio, ou sem fio com o receptor USB. "
                            "O cabo Play & Charge só carrega a bateria e não envia comandos.\n\n"
                            "Na biblioteca: direcional/analógico navega · A abre · B volta · LB/RB trocam de página.\n"
                            "No jogo: segure Back + Start para voltar à biblioteca · Back + Y digita o nome do perfil.")
        self.pad_settings_lbl = tk.Label(box, text="", fg=MUTED, bg=SURFACE, anchor="w", font=(self.FT, 10))
        self.pad_settings_lbl.pack(fill="x", padx=20, pady=(0, 16))
        self._update_pad_labels()
        tk.Frame(p, bg=BG, height=40).pack()
        return rows

    def _add_dir(self):
        d = filedialog.askdirectory(title="Adicionar pasta de jogos")
        if d:
            d = os.path.normpath(d)
            if d not in self.cfg["game_dirs"]:
                self.cfg["game_dirs"].append(d)
                save_config(self.cfg)
            self.refresh()

    def _remove_dir(self, d):
        self.cfg["game_dirs"] = [x for x in self.cfg["game_dirs"] if x != d]
        save_config(self.cfg)
        self.refresh()

    def pick_install_dir(self):
        d = filedialog.askdirectory(title="Pasta onde instalar os jogos do HD do Xbox")
        if d:
            self.cfg["install_dir"] = os.path.normpath(d)
            save_config(self.cfg)
            self.refresh()

    def _save_fs(self):
        self.cfg["fullscreen"] = self.fs_var.get()
        save_config(self.cfg)

    def pick_xenia(self):
        path = filedialog.askopenfilename(title="Selecione o xenia_canary.exe",
                                          filetypes=[("Xenia", "xenia*.exe"), ("Executáveis", "*.exe")])
        if path:
            self.cfg["xenia_path"] = os.path.normpath(path)
            save_config(self.cfg)
            self.refresh_sidebar()
            self.render_current()

    def _http(self, url, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": "Biblioteca360"})
        return urllib.request.urlopen(req, timeout=timeout)

    def _download_zip(self, label, resolve, dest, done):
        """Baixa um .zip em segundo plano, com progresso na barra lateral, e extrai em dest.
        resolve() roda na thread e devolve (url, info); done(info, erro) roda na interface."""
        if self.download:
            self.toast("Aguarde o download atual terminar.")
            return
        self.download = {"pct": 0, "text": label}
        self._show_progress()

        def work():
            try:
                url, info = resolve()
                os.makedirs(dest, exist_ok=True)
                zpath = os.path.join(dest, "download.zip")
                with self._http(url, 120) as r, open(zpath, "wb") as out:
                    total = int(r.headers.get("Content-Length") or 0) or 1
                    got = 0
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        got += len(chunk)
                        self.download["pct"] = min(100, int(got * 100 / total))
                with zipfile.ZipFile(zpath) as z:
                    z.extractall(dest)
                os.remove(zpath)
                self.after(0, lambda: self._download_done(done, info, ""))
            except Exception as e:
                err = str(e) or type(e).__name__
                self.after(0, lambda: self._download_done(done, None, err))

        threading.Thread(target=work, daemon=True).start()

    def _download_done(self, done, info, err):
        self.download = None
        done(info, err)
        self.refresh_sidebar()
        self.render_current()

    def _latest_asset(self, api, match):
        with self._http(api) as r:
            rel = json.load(r)
        asset = next((a for a in rel["assets"] if match(a["name"].lower())), None)
        if asset is None:
            raise FileNotFoundError("arquivo para Windows não encontrado na versão " + rel.get("tag_name", ""))
        return asset["browser_download_url"], rel.get("tag_name", "")

    def download_xenia(self):
        if not messagebox.askyesno("Baixar Xenia",
                                   "Baixar a versão mais recente do Xenia Canary (cerca de 5 MB) do GitHub oficial\n"
                                   f"e instalar em:\n{XENIA_DIR}?"):
            return

        def done(tag, err):
            exe = "" if err else find_xenia()
            if exe:
                self.cfg["xenia_path"] = exe
                save_config(self.cfg)
                self.toast(f"Xenia {tag} instalado.")
            else:
                self.toast("Não foi possível baixar o Xenia: " + (err or "arquivo não encontrado"), WARN)

        self._download_zip("Baixando Xenia…", lambda: self._latest_asset(
            XENIA_API, lambda n: n.startswith("xenia_canary_windows") and n.endswith(".zip")), XENIA_DIR, done)

    def download_xemu(self):
        if not messagebox.askyesno("Baixar xemu",
                                   "Baixar a versão mais recente do xemu (emulador do Xbox original) do GitHub "
                                   f"oficial e instalar em:\n{XEMU_DIR}?"):
            return

        def done(tag, err):
            exe = "" if err else find_xemu()
            if exe:
                self.cfg["xemu_path"] = exe
                save_config(self.cfg)
                self.toast(f"xemu {tag} instalado.")
            else:
                self.toast("Não foi possível baixar o xemu: " + (err or "arquivo não encontrado"), WARN)

        self._download_zip("Baixando xemu…", lambda: self._latest_asset(
            XEMU_API, lambda n: n.endswith(".zip") and "win" in n and ("x86_64" in n or "x64" in n)
            and not any(x in n for x in ("dbg", "pdb", "debug", "arm"))), XEMU_DIR, done)

    def download_xemu_hdd(self):
        if not messagebox.askyesno("Baixar HD vazio",
                                   "Baixar o HD vazio oficial do projeto xemu (sem nenhum arquivo da Microsoft) "
                                   f"para:\n{os.path.join(XEMU_DIR, 'hd')}?"):
            return
        dest = os.path.join(XEMU_DIR, "hd")

        def done(_info, err):
            hdd = next((os.path.join(cur, f) for cur, _, files in os.walk(dest) for f in files
                        if f.lower().endswith(".qcow2")), "") if not err else ""
            if hdd:
                self.cfg["xbox_hdd"] = hdd
                save_config(self.cfg)
                self.toast("HD vazio do Xbox pronto.")
            else:
                self.toast("Não foi possível baixar o HD: " + (err or "arquivo não encontrado"), WARN)

        self._download_zip("Baixando HD vazio…", lambda: (XEMU_HDD_URL, None), dest, done)

    def pick_xbox_file(self, key):
        title, types = {"xbox_bios": ("Selecione a BIOS do Xbox (flash, geralmente 256 KB ou 1 MB)",
                                      [("BIOS", "*.bin *.rom"), ("Todos", "*.*")]),
                        "xbox_mcpx": ("Selecione a MCPX boot ROM do Xbox (512 bytes)",
                                      [("MCPX", "*.bin *.rom"), ("Todos", "*.*")]),
                        "xbox_hdd": ("Selecione o HD do Xbox (.qcow2)",
                                     [("HD do xemu", "*.qcow2"), ("Todos", "*.*")])}[key]
        path = filedialog.askopenfilename(title=title, filetypes=types)
        if path:
            self.cfg[key] = os.path.normpath(path)
            save_config(self.cfg)
            self.render_current()

    def open_xbox_app(self):
        try:
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + XBOX_APP_AUMID])
        except OSError as e:
            self.toast(f"Não foi possível abrir o app Xbox: {e}", WARN)

    def open_cloud(self):
        webbrowser.open(CLOUD_URL)

    # ------------------------------------------------------------ nome do perfil (teclado na tela)

    def edit_gamertag(self):
        self._gt = self.cfg.get("gamertag", "")
        self.show_page("gamertag")

    def page_gamertag(self):
        p = self.page
        if not hasattr(self, "_gt"):
            self._gt = self.cfg.get("gamertag", "")
        self._title(p, "Nome do perfil",
                    "Escolha as letras com o direcional e aperte A. B volta sem salvar. Até 15 caracteres.")
        box = self._section(p, "Nome", pad_top=18)
        self._gt_lbl = tk.Label(box, text=self._gt + "▏", fg=FG, bg=SURFACE, anchor="w", font=(self.F, 22, "bold"))
        self._gt_lbl.pack(fill="x", padx=20, pady=16)
        rows = []
        for keys in ("1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"):
            bar = tk.Frame(p, bg=BG)
            bar.pack(anchor="w", padx=PAD_X, pady=(8, 0))
            row = []
            for k in keys:
                b = self._button(bar, f" {k} ", lambda k=k: self._gt_type(k))
                b.pack(side="left", padx=(0, 4))
                row.append(self._btn_nav(b))
            rows.append(row)
        rows.append(self._btnbar(p, [("Espaço", lambda: self._gt_type(" ")), ("Apagar", self._gt_back),
                                     ("Salvar", self._gt_save, True)]))
        return rows

    def _gt_type(self, ch):
        if len(self._gt) < 15:
            self._gt += ch
            self._gt_lbl.configure(text=self._gt + "▏")

    def _gt_back(self):
        self._gt = self._gt[:-1]
        self._gt_lbl.configure(text=self._gt + "▏")

    def _gt_save(self):
        self.cfg["gamertag"] = self._gt.strip()
        save_config(self.cfg)
        self.toast(f"Nome do perfil: {self.cfg['gamertag']}" if self.cfg["gamertag"] else "Nome do perfil apagado.")
        self.go_back()

    def type_gamertag(self):
        """Digita o nome do perfil na janela do Xenia (caixa de texto do Create Profile)."""
        name = self.cfg.get("gamertag", "")
        if not (name and self.game_hwnd):
            return
        user32.SetFocus(self.game_hwnd)
        for ch in name:
            user32.PostMessageW(self.game_hwnd, WM_CHAR, ord(ch), 1)

    def open_osk(self):
        try:
            os.startfile("osk.exe")
        except OSError as e:
            self.toast(f"Não foi possível abrir o teclado virtual: {e}", WARN)

    def download_covers(self):
        ids = sorted({g["title_id"] for g in self.games + self.hd_games() if g["title_id"]})
        self.cover_status.configure(text="Baixando capas…", fg=FG)

        def work():
            ok = 0
            os.makedirs(COVERS_DIR, exist_ok=True)
            for tid in ids:
                dest = os.path.join(COVERS_DIR, tid + ".jpg")
                if os.path.isfile(dest):
                    ok += 1
                    continue
                try:
                    with self._http(COVER_URL.format(tid=tid), 10) as r:
                        data = r.read()
                    if data[:2] == b"\xff\xd8":
                        with open(dest, "wb") as f:
                            f.write(data)
                        ok += 1
                except Exception:
                    continue
            self.after(0, lambda: done(ok))

        def done(ok):
            self.art.clear()
            self.refresh_sidebar()
            self.render_current()
            self.toast(f"{ok} de {len(ids)} jogos com capa.")

        threading.Thread(target=work, daemon=True).start()

    def open_compat(self, g):
        webbrowser.open(COMPAT_URL + (g["title_id"] or g["title"]))

    def open_folder(self, g):
        subprocess.Popen(["explorer", "/select,", g["path"]], creationflags=NO_WINDOW)

    # ------------------------------------------------------------ HD do Xbox: leitura e instalacao

    def detect_hd(self):
        self.hd_disks = None
        if self.current[0] == "hd":
            self.render_current()

        def work():
            disks = detect_xbox_disks()
            self.after(0, lambda: self._set_disks(disks))

        threading.Thread(target=work, daemon=True).start()

    def _set_disks(self, disks):
        self.hd_disks = disks
        if disks:
            keep = [d for d in disks if self.hd_disk and disk_key(d) == disk_key(self.hd_disk)]
            self.select_disk(keep[0] if keep else disks[0])
        else:
            self.hd_disk = None
            self.hd_list = None
        self.refresh_sidebar()
        if not self.in_game and self.current[0] in ("hd", "home", "library"):
            self.render_current()

    def select_disk(self, d):
        self.hd_disk = d
        self.hd_list = None
        try:
            with open(os.path.join(DATA_DIR, f"hd_{disk_key(d)}.json"), encoding="utf-8") as f:
                self.hd_list = json.load(f)
        except (OSError, ValueError):
            pass

    def _job_files(self, kind):
        stamp = str(int(time.time() * 1000))
        base = os.path.join(DATA_DIR, f"job_{kind}_{stamp}")
        return base + ".json", base + "_progress.json", base + "_cancel"

    def _start_helper(self, kind, job):
        job_path, prog, cancel = self._job_files(kind)
        job.update({"disk": self.hd_disk["number"], "size": self.hd_disk["size"], "progress": prog,
                    "cancel": cancel})
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
        if not run_elevated_helper(job_path):
            self.toast("Permissão de administrador negada — não foi possível ler o HD.", WARN)
            return None
        return {"kind": kind, "job": job_path, "progress": prog, "cancel": cancel, "started": time.time(),
                "state": {}, **job}

    def read_hd(self):
        if self.job or not self.hd_disk:
            if self.job:
                self.toast("Aguarde a tarefa atual terminar.")
            return
        out = os.path.join(DATA_DIR, f"hd_{disk_key(self.hd_disk)}.json")
        job = self._start_helper("list", {"action": "list", "out": out})
        if job:
            self.job = job
            self.hd_reading = True
            self._show_progress()
            self.render_current()
            self.after(500, self._poll_job)

    def dest_dir(self, h):
        return os.path.join(self.cfg["install_dir"], "Do HD do Xbox", h["title_id"], h["type"])

    def free_space(self):
        d = self.cfg["install_dir"]
        while d and not os.path.exists(d):
            d = os.path.dirname(d) if os.path.dirname(d) != d else ""
        try:
            return shutil.disk_usage(d or "C:\\").free
        except OSError:
            return 0

    def install(self, g):
        h = g["hd"]
        pending = sum(q["size"] for q in self.queue)
        if self.job and self.job["kind"] == "copy":
            st = self.job["state"]
            pending += max(0, st.get("total", 0) - st.get("done", 0))
        if h["size"] + pending + 300 * 1024 * 1024 > self.free_space():
            messagebox.showwarning("Sem espaço",
                                   f"{h['title']} precisa de {fmt_size(h['size'])}, mas só há "
                                   f"{fmt_size(max(0, self.free_space() - pending))} livres em "
                                   f"{self.cfg['install_dir']}.\n\nLibere espaço ou escolha outra pasta de "
                                   "instalação (pode ser outro HD).")
            return
        if any(q["path"] == h["path"] for q in self.queue):
            return
        self.queue.append(dict(h, dest_dir=self.dest_dir(h)))
        self._next_install()
        self.render_current()

    def _next_install(self):
        if self.job or not self.queue or not self.hd_disk:
            return
        items = self.queue[:]
        job = self._start_helper("copy", {"action": "copy", "items": items})
        if not job:
            self.queue.clear()
            self.render_current()
            return
        self.queue.clear()
        self.job = job
        self._show_progress()
        self.after(500, self._poll_job)

    def cancel_install(self, g):
        path = g["hd"]["path"]
        before = len(self.queue)
        self.queue = [q for q in self.queue if q["path"] != path]
        if len(self.queue) == before and self.job and self.job["kind"] == "copy":
            if messagebox.askyesno("Cancelar instalação", "Cancelar a instalação em andamento?"):
                open(self.job["cancel"], "w").close()
        self.render_current()

    def job_percent(self):
        st = self.job["state"] if self.job else {}
        return int(st.get("done", 0) * 100 / st["total"]) if st.get("total") else 0

    def _poll_job(self):
        job = self.job
        if not job:
            return
        try:
            with open(job["progress"], encoding="utf-8") as f:
                job["state"] = json.load(f)
        except (OSError, ValueError):
            if time.time() - job["started"] > 60:
                job["state"] = {"finished": True, "error": "O ajudante de leitura não respondeu."}
        st = job["state"]
        self._show_progress()
        if st.get("finished"):
            self.job = None
            self.hd_reading = False
            for p in (job["job"], job["progress"], job["cancel"]):
                try:
                    os.remove(p)
                except OSError:
                    pass
            if job["kind"] == "list":
                if st.get("error"):
                    self.toast("Erro ao ler o HD: " + st["error"], WARN)
                else:
                    self.select_disk(self.hd_disk)
                    n = len(self.hd_list["games"]) if self.hd_list else 0
                    self.toast(f"{n} jogos encontrados no HD do Xbox.")
            else:
                done = len(st.get("completed", []))
                if st.get("error") == "cancelado":
                    self.toast("Instalação cancelada.")
                elif st.get("error"):
                    self.toast("Erro na instalação: " + st["error"], WARN)
                else:
                    self.toast(f"{done} jogo(s) instalado(s). Já estão na sua biblioteca!")
            self.refresh()
            self._show_progress()
            self.after(300, self._next_install)
            return
        if job["kind"] == "copy" and not self.in_game and self.current[0] == "game":
            if self.current[1].get("path", "").startswith("hd:"):
                self._update_install_button()
        self.after(500, self._poll_job)

    def _update_install_button(self):
        pct = self.job_percent()
        for w in self.page.winfo_children():
            if isinstance(w, tk.Canvas):
                for item in w.find_all():
                    if w.type(item) == "window":
                        fr = w.nametowidget(w.itemcget(item, "window"))
                        b = getattr(fr, "inner", None)
                        if b is not None and str(b["text"]).startswith("Instalando"):
                            b.configure(text=f"Instalando…  {pct}%")
                return

    def _show_progress(self):
        text, pct = "", None
        if self.download:
            text, pct = f"{self.download['text']} {self.download['pct']}%", self.download["pct"]
        elif self.job:
            st = self.job["state"]
            if self.job["kind"] == "list":
                text = "Lendo o HD do Xbox…\n(aceite o pedido de permissão)" if not st else "Lendo o HD do Xbox…"
            else:
                pct = self.job_percent()
                cur = st.get("current") or self.job["items"][0]["title"]
                n = len(self.job["items"])
                extra = f"  ·  {len(st.get('completed', [])) + 1}/{n}" if n > 1 else ""
                text = (f"Instalando {cur}{extra}\n{fmt_size(st.get('done', 0))} de "
                        f"{fmt_size(st.get('total', 0))}  ·  {pct}%")
                if self.queue:
                    text += f"\n+{len(self.queue)} na fila"
        if not text:
            self.job_box.pack_forget()
            return
        self.job_lbl.configure(text=text)
        if not self.job_box.winfo_ismapped():
            self.job_box.pack(side="bottom", fill="x", padx=12, pady=(0, 4), before=self.side_status)
        self.job_bar.delete("all")
        w = max(1, self.job_bar.winfo_width())
        if pct is None:
            x = (time.time() * 120) % (w + 60) - 60
            self.job_bar.create_rectangle(x, 0, x + 60, 4, fill=ACCENT, outline="")
        else:
            self.job_bar.create_rectangle(0, 0, w * pct / 100, 4, fill=ACCENT, outline="")
        if self.download:
            self.after(300, self._show_progress)

    # ------------------------------------------------------------ navegacao por teclado / controle

    def set_nav(self, rows):
        self.nav_rows = [r for r in rows if r]
        self.nav_pos = None
        if self.pad_connected and self.nav_rows:
            self._focus_at(0, 0)

    def _focus_at(self, r, c):
        if self.nav_pos is not None:
            try:
                o = self.nav_rows[self.nav_pos[0]][self.nav_pos[1]]
                o.focus(False)
            except (IndexError, tk.TclError):
                pass
        self.nav_pos = (r, c)
        item = self.nav_rows[r][c]
        item.focus(True)
        self._ensure_visible(item, first_row=r == 0)

    def nav_move(self, dr, dc):
        if not self.nav_rows:
            return
        if self.nav_pos is None:
            self._focus_at(0, 0)
            return
        r, c = self.nav_pos
        if dr:
            r = max(0, min(len(self.nav_rows) - 1, r + dr))
            c = min(c, len(self.nav_rows[r]) - 1)
        if dc:
            c = max(0, min(len(self.nav_rows[r]) - 1, c + dc))
        self._focus_at(r, c)

    def nav_activate(self):
        if self.nav_pos is None:
            self.nav_move(0, 0)
        else:
            self.nav_rows[self.nav_pos[0]][self.nav_pos[1]].activate()

    def _ensure_visible(self, item, first_row=False):
        self.update_idletasks()
        w = item.widget
        hc = item.hcanvas
        if hc is not None:
            inner = hc._inner
            total = max(1, inner.winfo_width())
            x = w.winfo_rootx() - inner.winfo_rootx()
            left = hc.xview()[0] * total
            if x < left:
                hc.xview_moveto(max(0, x - 10) / total)
            elif x + w.winfo_width() > left + hc.winfo_width():
                hc.xview_moveto((x + w.winfo_width() - hc.winfo_width() + 30) / total)
        if first_row:
            self.vcanvas.yview_moveto(0)
            return
        total = max(1, self.page.winfo_height())
        y = w.winfo_rooty() - self.page.winfo_rooty()
        top = self.vcanvas.yview()[0] * total
        vh = self.vcanvas.winfo_height()
        if y - 60 < top:
            self.vcanvas.yview_moveto(max(0, y - 70) / total)
        elif y + w.winfo_height() + 20 > top + vh:
            self.vcanvas.yview_moveto((y + w.winfo_height() - vh + 40) / total)

    def _typing(self):
        return isinstance(self.focus_get(), tk.Entry)

    def _key_nav(self, dr, dc):
        if not self.in_game and not self._typing():
            self.nav_move(dr, dc)

    def _key_activate(self, e):
        if self.in_game:
            return
        if self._typing():
            self.focus_set()
            if self.nav_rows:
                self._focus_at(0, 0)
            return
        self.nav_activate()

    def _on_escape(self, e):
        if self.attributes("-fullscreen"):
            self.set_fullscreen(False)
        elif not self.in_game:
            if self._typing():
                self.focus_set()
            else:
                self.go_back()

    def _switch_page(self, step):
        name = self.current[0]
        i = self.PAGES.index(name) if name in self.PAGES else 0
        self.show_page(self.PAGES[(i + step) % len(self.PAGES)])

    def _on_wheel(self, e):
        if self.in_game:
            return
        w = self.winfo_containing(e.x_root, e.y_root)
        if e.state & 1:  # Shift: rola a fileira na horizontal
            while w is not None and not getattr(w, "_hrow", False):
                w = w.master
            if w is not None:
                w.xview_scroll(int(-e.delta / 120), "units")
            return
        self.vcanvas.yview_scroll(int(-e.delta / 120) * 2, "units")

    def _on_canvas_resize(self, e):
        self.vcanvas.itemconfigure(self._page_win, width=e.width)
        if abs(e.width - self._last_w) > 60 and not self.in_game:
            if self._resize_job:
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(250, self.render_current)

    def toast(self, msg, color=FG):
        self.toast_lbl.configure(text=msg, fg=color)
        self.toast_lbl.place(relx=0.5, rely=1.0, y=-24, anchor="s")
        self.toast_lbl.lift()
        if self._toast_job:
            self.after_cancel(self._toast_job)
        self._toast_job = self.after(5000, self.toast_lbl.place_forget)

    def _update_pad_labels(self):
        on = bool(self.pad_connected)
        self.pad_lbl.configure(text="🎮  Controle conectado" if on else "🎮  Sem controle", fg=FG if on else MUTED)
        lbl = getattr(self, "pad_settings_lbl", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text="● Controle conectado" if on else "● Nenhum controle detectado",
                          fg=ACCENT_HI if on else WARN)

    def _poll_pad(self):
        connected, pressed = self.pad.poll()
        if connected != self.pad_connected:
            self.pad_connected = connected
            self._update_pad_labels()
        if self.in_game:
            if {"BACK", "START"} <= self.pad.down:
                self._combo = getattr(self, "_combo", 0) + 1
                if self._combo == 15:  # ~0,75 s segurando Back + Start
                    self.close_game()
            else:
                self._combo = 0
            if {"BACK", "Y"} <= self.pad.down:
                if not getattr(self, "_typed", False):
                    self._typed = True
                    self.type_gamertag()
            else:
                self._typed = False
        elif pressed and self.focus_displayof() is not None:
            for btn in pressed:
                if btn in ("UP", "DOWN", "LEFT", "RIGHT"):
                    self.nav_move(*{"UP": (-1, 0), "DOWN": (1, 0), "LEFT": (0, -1), "RIGHT": (0, 1)}[btn])
                elif btn == "A":
                    self.nav_activate()
                elif btn == "B":
                    self.go_back()
                elif btn in ("LB", "RB"):
                    self._switch_page(-1 if btn == "LB" else 1)
        self.after(50, self._poll_pad)

    # ------------------------------------------------------------ jogo (Xenia dentro da janela)

    def video_mode(self, g):
        key = self.cfg.get("video_modes", {}).get(g["path"], "padrao")
        return next((m for m in VIDEO_MODES if m[0] == key), VIDEO_MODES[0])

    def next_video_mode(self, g):
        """Troca o modo de video do Xenia deste jogo (vale a partir da proxima vez que ele abrir)."""
        i = VIDEO_MODES.index(self.video_mode(g))
        mode = VIDEO_MODES[(i + 1) % len(VIDEO_MODES)]
        self.cfg.setdefault("video_modes", {})[g["path"]] = mode[0]
        save_config(self.cfg)
        self.toast(f"Modo de vídeo: {mode[1]}. Abra o jogo de novo para testar.")
        self.render_current()

    def _mark_played(self, g):
        self.cfg["recent"] = [g["path"]] + [p for p in self.cfg.get("recent", []) if p != g["path"]][:19]
        self.cfg.setdefault("last_played", {})[g["path"]] = int(time.time())
        save_config(self.cfg)
        self.refresh_sidebar()

    def launch(self, g):
        if g is None:
            return
        if g["kind"] == "PC":
            try:
                subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + g["aumid"]])
            except OSError as e:
                messagebox.showerror("Erro ao iniciar", str(e))
                return
            self._mark_played(g)
            self.toast(f"Abrindo {g['title']} pelo Windows…")
            return
        xbox = g["kind"] == "XBOX"
        if not (self.xemu_ok() if xbox else self.xenia_ok()):
            name = "xemu" if xbox else "Xenia Canary"
            if messagebox.askyesno(f"{name} não instalado",
                                   f"Para rodar jogos de {platform_name(g)} é preciso o emulador {name}.\n\n"
                                   "Baixar agora?"):
                self.download_xemu() if xbox else self.download_xenia()
            return
        if xbox and self.xemu_missing():
            messagebox.showinfo("Arquivos do Xbox original",
                                "Para ligar o Xbox original, o xemu precisa de arquivos do seu console:\n\n• "
                                + "\n• ".join(self.xemu_missing())
                                + "\n\nEscolha-os em Configurações → Xbox original.")
            self.show_page("settings")
            return
        if self.proc and self.proc.poll() is None:
            return
        try:
            if xbox:
                emu = self.cfg["xemu_path"]
                write_xemu_config(self.cfg)
                cmd = [emu, "-dvd_path", g["path"]]
            else:
                emu = self.cfg["xenia_path"]
                cmd = [emu, "--fullscreen=false", *self.video_mode(g)[2], g["path"]]
            self.proc = subprocess.Popen(cmd, cwd=os.path.dirname(emu))
        except OSError as e:
            messagebox.showerror("Erro ao iniciar", str(e))
            return
        self._mark_played(g)
        self._enter_game(g)

    def _build_game_view(self):
        self.game_view = tk.Frame(self, bg="black")
        self.game_bar = tk.Frame(self.game_view, bg=SIDEBAR)
        self.game_bar.pack(fill="x")
        self._button(self.game_bar, "←  Biblioteca", self.close_game).pack(side="left", padx=8, pady=4)
        self.game_title = tk.Label(self.game_bar, text="", fg=FG, bg=SIDEBAR, font=(self.F, 11, "bold"))
        self.game_title.pack(side="left", padx=8)
        self._button(self.game_bar, "Tela cheia (F11)", self.toggle_fullscreen).pack(side="right", padx=8)
        self._button(self.game_bar, "⌨  Teclado", self.open_osk).pack(side="right", padx=(8, 0))
        tk.Label(self.game_bar, text="Voltar: segure Back + Start  ·  Nome do perfil: Back + Y", fg=MUTED, bg=SIDEBAR,
                 font=(self.FT, 9)).pack(side="right", padx=8)
        self.game_host = tk.Frame(self.game_view, bg="black")
        self.game_host.pack(fill="both", expand=True)
        self.game_msg = tk.Label(self.game_host, text="", fg=MUTED, bg="black", font=(self.FT, 12))
        self.game_msg.place(relx=0.5, rely=0.5, anchor="center")
        self.game_host.bind("<Configure>", lambda e: self._fit_game())

    def _enter_game(self, g):
        self.in_game = True
        self.game_hwnd = None
        self.shell.pack_forget()
        self.game_title.config(text=g["title"])
        self.game_msg.config(text=f"Carregando {g['title']}...\nA primeira vez pode demorar alguns minutos.")
        self.game_view.pack(fill="both", expand=True)
        if self.cfg.get("fullscreen"):
            self.set_fullscreen(True)
        self._attach_tries = 0
        self.after(300, self._attach_game)

    def _attach_game(self):
        if not self.in_game:
            return
        if self.proc.poll() is not None:
            self._leave_game("O emulador fechou. Veja o arquivo de log na pasta dele se foi um erro.")
            return
        hwnd = win32_find_main_window(self.proc.pid)
        if not hwnd:
            self._attach_tries += 1
            if self._attach_tries > 150:
                self.game_msg.config(text="A janela do emulador não apareceu.")
                return
            self.after(300, self._attach_game)
            return
        self.game_hwnd = hwnd
        self.update_idletasks()
        win32_embed(hwnd, self.game_host.winfo_id())
        self._fit_game()
        user32.SetFocus(hwnd)
        self._watch_game()

    def _fit_game(self):
        if self.game_hwnd:
            win32_move(self.game_hwnd, self.game_host.winfo_width(), self.game_host.winfo_height())

    def _watch_game(self):
        if not self.in_game:
            return
        if self.proc.poll() is not None:
            self._leave_game()
            return
        self.after(500, self._watch_game)

    def close_game(self):
        if self.proc and self.proc.poll() is None:
            if self.game_hwnd:
                user32.PostMessageW(self.game_hwnd, WM_CLOSE, 0, 0)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._leave_game()

    def _leave_game(self, msg=None):
        if not self.in_game:
            return
        self.in_game = False
        self.game_hwnd = None
        self.set_fullscreen(False)
        self.game_view.pack_forget()
        self.shell.pack(fill="both", expand=True)
        self.render_current()
        if msg:
            self.toast(msg, WARN)

    def set_fullscreen(self, on):
        self.attributes("-fullscreen", on)
        if self.in_game:
            if on:
                self.game_bar.pack_forget()
            else:
                self.game_bar.pack(fill="x", before=self.game_host)
            self.after(100, self._fit_game)

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.attributes("-fullscreen"))

    def on_close(self):
        if self.job and self.job["kind"] == "copy":
            if not messagebox.askyesno("Instalação em andamento",
                                       "Há uma instalação em andamento. Fechar vai cancelá-la. Fechar mesmo assim?"):
                return
            open(self.job["cancel"], "w").close()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.destroy()


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--hd-helper":
        fatx.run_job(sys.argv[2])
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        pass
    Launcher().mainloop()


if __name__ == "__main__":
    main()
