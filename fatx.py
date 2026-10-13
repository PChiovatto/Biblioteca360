"""Leitor FATX (HD / pendrive do Xbox 360) - SOMENTE LEITURA.

Usado pela Biblioteca 360 como "ajudante" com permissao de administrador
(necessaria para abrir o disco fisico). O disco e aberto so para leitura.

Uso direto:
  python fatx.py job <arquivo_job.json>
  python fatx.py list <numero_disco> <saida.json>
"""
import base64
import json
import os
import struct
import sys
import time

SECTOR = 512
READ_RUN = 256  # clusters consecutivos lidos de uma vez

# (nome, deslocamento, tamanho ou None = ate o fim do disco)
LAYOUTS = [
    ("Data", 0x130EB0000, None),          # HD do Xbox 360
    ("Compatibility", 0x120EB0000, 0x10000000),
    ("SystemExtended", 0x10C080000, 0xCE30000),
    ("SystemAux", 0x118EB0000, 0x8000000),
    ("Data", 0x20000000, None),           # pendrive formatado pelo Xbox
]

GAME_TYPES = {"00007000": "Games on Demand", "000D0000": "Xbox Live Arcade"}


class Disk:
    def __init__(self, number, size=0):
        self.f = open(f"\\\\.\\PhysicalDrive{number}", "rb", buffering=0)
        self.size = self._size(number) or int(size or 0)

    @staticmethod
    def _size(number):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        ctypes.c_void_p]
        h = k32.CreateFileW(f"\\\\.\\PhysicalDrive{number}", 0x80000000, 3, None, 3, 0, None)
        out = ctypes.c_longlong()
        ret = wintypes.DWORD()
        ok = k32.DeviceIoControl(h, 0x7405C, None, 0, ctypes.byref(out), 8, ctypes.byref(ret), None)
        k32.CloseHandle(h)
        return out.value if ok else 0

    def read(self, off, n):
        start = off - off % SECTOR
        end = off + n
        end += -end % SECTOR
        self.f.seek(start)
        data = self.f.read(end - start)
        return data[off - start:off - start + n]


class Fatx:
    def __init__(self, disk, offset, size):
        self.d = disk
        self.off = offset
        hdr = disk.read(offset, 16)
        if hdr[:4] != b"XTAF":
            raise ValueError("sem assinatura XTAF")
        self.spc = struct.unpack(">I", hdr[8:12])[0]
        self.root = struct.unpack(">I", hdr[12:16])[0]
        self.cluster = self.spc * SECTOR
        self.clusters = size // self.cluster
        self.entry = 2 if self.clusters < 0xFFF0 else 4
        fat_size = self.clusters * self.entry
        fat_size += -fat_size % 4096
        self.fat_off = offset + 4096
        self.data_off = self.fat_off + fat_size
        self._fat_cache = {}

    def fat(self, n):
        page = n * self.entry // 65536
        if page not in self._fat_cache:
            self._fat_cache[page] = self.d.read(self.fat_off + page * 65536, 65536)
        b = self._fat_cache[page]
        i = n * self.entry % 65536
        if self.entry == 2:
            v = struct.unpack(">H", b[i:i + 2])[0]
            return v if v < 0xFFF0 else None
        v = struct.unpack(">I", b[i:i + 4])[0]
        return v if v < 0xFFFFFFF0 else None

    def chain(self, first):
        seen = set()
        n = first
        while n and n not in seen and n <= self.clusters:
            seen.add(n)
            yield n
            n = self.fat(n)

    def cluster_off(self, n):
        return self.data_off + (n - 1) * self.cluster

    def listdir(self, first):
        out = []
        for c in self.chain(first):
            buf = self.d.read(self.cluster_off(c), self.cluster)
            for i in range(0, self.cluster, 64):
                e = buf[i:i + 64]
                nl = e[0]
                if nl in (0x00, 0xFF):
                    return out
                if nl == 0xE5 or nl > 42:
                    continue
                first_c, size = struct.unpack(">II", e[44:52])
                out.append({"name": e[2:2 + nl].decode("latin-1"), "dir": bool(e[1] & 0x10),
                            "cluster": first_c, "size": size})
        return out

    def resolve(self, path):
        cur = {"dir": True, "cluster": self.root, "name": "", "size": 0}
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            match = [e for e in self.listdir(cur["cluster"]) if e["name"].lower() == part.lower()]
            if not match:
                raise FileNotFoundError(path)
            cur = match[0]
        return cur

    def tree_size(self, entry):
        if not entry["dir"]:
            return entry["size"]
        return sum(self.tree_size(e) for e in self.listdir(entry["cluster"]))

    def iter_file(self, entry):
        """Le o arquivo em blocos grandes, juntando clusters consecutivos."""
        left = entry["size"]
        need = (left + self.cluster - 1) // self.cluster
        run = []
        for c in self.chain(entry["cluster"]):
            if need <= 0:
                break
            need -= 1
            if run and (c != run[-1] + 1 or len(run) >= READ_RUN):
                data = self.d.read(self.cluster_off(run[0]), len(run) * self.cluster)
                data = data[:left]
                left -= len(data)
                yield data
                run = []
            run.append(c)
        if run and left > 0:
            yield self.d.read(self.cluster_off(run[0]), len(run) * self.cluster)[:left]

    def head(self, entry, n):
        out = b""
        for chunk in self.iter_file(dict(entry, size=min(n, entry["size"]))):
            out += chunk
        return out

    def copy(self, entry, dest, on_bytes, cancelled):
        if entry["dir"]:
            os.makedirs(dest, exist_ok=True)
            for e in self.listdir(entry["cluster"]):
                self.copy(e, os.path.join(dest, e["name"]), on_bytes, cancelled)
            return
        with open(dest, "wb") as out:
            for chunk in self.iter_file(entry):
                if cancelled():
                    raise InterruptedError("cancelado")
                out.write(chunk)
                on_bytes(len(chunk), dest)

    def games(self):
        found = []
        for top in self.listdir(self.root):
            if not (top["dir"] and top["name"].lower() == "content"):
                continue
            for prof in (x for x in self.listdir(top["cluster"]) if x["dir"]):
                for title in (x for x in self.listdir(prof["cluster"]) if x["dir"]):
                    for ctype in (x for x in self.listdir(title["cluster"]) if x["dir"]):
                        if ctype["name"] not in GAME_TYPES:
                            continue
                        entries = self.listdir(ctype["cluster"])
                        for pkg in (x for x in entries if not x["dir"]):
                            h = self.head(pkg, 0xB000)
                            if h[:4] not in (b"LIVE", b"PIRS", b"CON ") or len(h) < 0x1720:
                                continue
                            name = h[0x411:0x491].decode("utf-16-be", "ignore").split("\x00")[0].strip()
                            icon = ""
                            isz = struct.unpack(">I", h[0x1712:0x1716])[0]
                            if 0 < isz <= 0x4000 and h[0x171A:0x171E] == b"\x89PNG":
                                icon = base64.b64encode(h[0x171A:0x171A + isz]).decode()
                            data = [x for x in entries
                                    if x["dir"] and x["name"].lower() == pkg["name"].lower() + ".data"]
                            size = pkg["size"] + (self.tree_size(data[0]) if data else 0)
                            found.append({"title": name or title["name"], "title_id": title["name"],
                                          "type": ctype["name"], "pkg": pkg["name"], "size": size, "icon": icon,
                                          "path": f"/Content/{prof['name']}/{title['name']}/{ctype['name']}/{pkg['name']}"})
        found.sort(key=lambda g: g["title"].lower())
        return found


def open_partitions(disk):
    parts = []
    for name, off, size in LAYOUTS:
        if off >= disk.size or any(p[0] == name for p in parts):
            continue
        try:
            parts.append((name, Fatx(disk, off, size or disk.size - off)))
        except ValueError:
            continue
    return parts


# ---------------------------------------------------------------- tarefas


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def run_job(job_path):
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    prog = job["progress"]
    state = {"finished": False, "error": "", "done": 0, "total": 0, "current": "", "completed": []}
    _write_json(prog, state)
    last = [0.0]

    def flush(force=False):
        if force or time.monotonic() - last[0] > 0.4:
            last[0] = time.monotonic()
            _write_json(prog, state)

    try:
        disk = Disk(job["disk"], job.get("size", 0))
        parts = dict(open_partitions(disk))
        if "Data" not in parts:
            raise RuntimeError("Este disco não parece ser um HD ou pendrive do Xbox 360 (FATX não encontrado).")
        fx = parts["Data"]

        if job["action"] == "list":
            state["current"] = "Lendo a lista de jogos..."
            flush(True)
            _write_json(job["out"], {"read_at": int(time.time()), "disk_size": disk.size, "games": fx.games()})

        elif job["action"] == "copy":
            cancel_flag = job.get("cancel", "")
            cancelled = lambda: bool(cancel_flag) and os.path.exists(cancel_flag)
            state["total"] = sum(it["size"] for it in job["items"])
            for it in job["items"]:
                state["current"] = it["title"]
                flush(True)
                folder = os.path.dirname(it["path"])
                pkg = fx.resolve(it["path"])
                dest_dir = it["dest_dir"]
                os.makedirs(dest_dir, exist_ok=True)

                def on_bytes(n, _dest):
                    state["done"] += n
                    flush()

                # Pasta <pacote>.data (jogos GOD) vem primeiro; o pacote por ultimo,
                # via .part, para so aparecer completo.
                try:
                    data = fx.resolve(folder + "/" + it["pkg"] + ".data")
                    fx.copy(data, os.path.join(dest_dir, it["pkg"] + ".data"), on_bytes, cancelled)
                except FileNotFoundError:
                    pass
                part = os.path.join(dest_dir, it["pkg"] + ".part")
                fx.copy(pkg, part, on_bytes, cancelled)
                os.replace(part, os.path.join(dest_dir, it["pkg"]))
                state["completed"].append(it["path"])
                flush(True)
    except InterruptedError:
        state["error"] = "cancelado"
    except PermissionError:
        state["error"] = "Sem permissão para ler o disco (é preciso aceitar o pedido de administrador)."
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
    finally:
        state["finished"] = True
        _write_json(prog, state)


def main():
    if sys.argv[1] == "job":
        run_job(sys.argv[2])
    elif sys.argv[1] == "list":
        disk = Disk(int(sys.argv[2]))
        fx = dict(open_partitions(disk))["Data"]
        _write_json(sys.argv[3], {"games": fx.games()})


if __name__ == "__main__":
    main()
