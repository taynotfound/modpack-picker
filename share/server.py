#!/usr/bin/env python3
"""Modpack Picker backend: browse Modrinth modpacks / local .mrpack files and
install them as Prism Launcher instances. Listens on 127.0.0.1 only."""
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
PRISM_APP = "org.prismlauncher.PrismLauncher"
PRISM_DATA = HOME / ".var/app" / PRISM_APP / "data/PrismLauncher"
INSTANCES = PRISM_DATA / "instances"
ICONS = PRISM_DATA / "icons"
DEFAULT_CONF = HOME / ".config/minecraft-handheld/instance"
SETTINGS_FILE = HOME / ".config/minecraft-handheld/settings.json"
DEFAULT_SETTINGS = {
    "shortcut": "auto",      # auto | list | last | starred
    "after_game": "steam",   # steam | list
    "memory_gb": 4,          # RAM for newly created games
}
ESSENTIAL_SLUGS = {"controlify", "midnightcontrols", "sodium", "embeddium", "fabric-api"}
STATIC = Path(__file__).resolve().parent
PACK_DIRS = [HOME / "Downloads", HOME / "Modpacks"]
API = "https://api.modrinth.com/v2"
UA = "handheld-modpack-picker/1.0"
ALLOWED_HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com"}
# Always added to every pack. Per category: first available candidate wins,
# skipped if the pack already has something matching the hints.
ESSENTIALS = [
    {"label": "Controller support", "candidates": ["controlify", "midnightcontrols"],
     "hints": ("controlify", "midnightcontrols", "controllable")},
    {"label": "Performance", "candidates": ["sodium", "embeddium"],
     "hints": ("sodium", "embeddium", "rubidium", "magnesium", "optifine")},
]
META_FABRIC = "https://meta.fabricmc.net/v2/versions/loader"
# mrpack dependency key -> (modrinth loader name, Prism component uid)
LOADERS = {
    "fabric-loader": ("fabric", "net.fabricmc.fabric-loader"),
    "quilt-loader": ("quilt", "org.quiltmc.quilt-loader"),
    "neoforge": ("neoforge", "net.neoforged"),
    "forge": ("forge", "net.minecraftforge"),
}

JOBS = {}
INSTALL_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

def _ssl_context():
    """Default certs, falling back to a known CA bundle file (some sandboxes
    only ship the bundle, not the hashed cert directory OpenSSL looks for)."""
    ctx = ssl.create_default_context()
    for f in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
              "/etc/ssl/certs/ca-bundle.crt", "/etc/ssl/cert.pem", "/etc/pki/tls/cert.pem"):
        if os.path.isfile(f):
            try:
                ctx.load_verify_locations(cafile=f)
                break
            except (OSError, ssl.SSLError):
                pass
    return ctx


SSL_CTX = _ssl_context()

def api(path, params=None, data=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return json.load(r)


def check_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Blocked download from {u.hostname}")


def download(url, dest, sha512=None, sha1=None):
    check_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h512, h1 = hashlib.sha512(), hashlib.sha1()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h512.update(chunk)
            h1.update(chunk)
    bad = (sha512 and h512.hexdigest() != sha512.lower()) or \
          (not sha512 and sha1 and h1.hexdigest() != sha1.lower())
    if bad:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Checksum mismatch for {dest.name}")
    tmp.replace(dest)


def safe_join(root, rel):
    rel = rel.replace("\\", "/").lstrip("/")
    p = (root / rel).resolve()
    r = root.resolve()
    if p != r and r not in p.parents:
        raise ValueError(f"Unsafe path in pack: {rel}")
    return p


def unique_id(name):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "modpack"
    base = base[:48]
    iid, n = base, 2
    while (INSTANCES / iid).exists() or (INSTANCES / (".installing-" + iid)).exists():
        iid = f"{base}-{n}"
        n += 1
    return iid


def get_default():
    try:
        return DEFAULT_CONF.read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def set_default(iid):
    DEFAULT_CONF.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CONF.write_text(iid + "\n")


def load_settings():
    out = dict(DEFAULT_SETTINGS)
    try:
        out.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text()).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return out


def save_settings(new):
    cur = load_settings()
    if new.get("shortcut") in ("auto", "list", "last", "starred"):
        cur["shortcut"] = new["shortcut"]
    if new.get("after_game") in ("steam", "list"):
        cur["after_game"] = new["after_game"]
    if isinstance(new.get("memory_gb"), int) and 2 <= new["memory_gb"] <= 12:
        cur["memory_gb"] = new["memory_gb"]
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(cur, indent=2) + "\n")
    return cur


def read_cfg(iid):
    out = {}
    for line in (INSTANCES / iid / "instance.cfg").read_text(errors="ignore").splitlines():
        if "=" in line and not line.startswith("["):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def write_cfg(iid, updates):
    """Set keys in a Prism instance.cfg, keeping everything else."""
    f = INSTANCES / iid / "instance.cfg"
    lines = f.read_text(errors="ignore").splitlines() if f.exists() else []
    todo = {k: str(v).replace("\n", " ") for k, v in updates.items()}
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0]
        if k in todo:
            lines[i] = f"{k}={todo.pop(k)}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    f.write_text("\n".join(lines) + "\n")


def new_instance_cfg(name, icon_key="default", extra=None):
    gb = load_settings()["memory_gb"]
    cfg = {"InstanceType": "OneSix", "name": name.replace("\n", " "), "iconKey": icon_key,
           "OverrideMemory": "true", "MinMemAlloc": 512, "MaxMemAlloc": gb * 1024}
    cfg.update(extra or {})
    return "\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n"


def instance_target(iid):
    """(minecraft version, [modrinth loaders]) for an instance."""
    mc, loader = "", ""
    try:
        for c in json.loads((INSTANCES / iid / "mmc-pack.json").read_text()).get("components", []):
            if c.get("uid") == "net.minecraft":
                mc = c.get("version", "")
            for ln, uid in LOADERS.values():
                if c.get("uid") == uid:
                    loader = ln
    except (OSError, ValueError):
        pass
    loaders = [loader] + (["fabric"] if loader == "quilt" else []) if loader else []
    return mc, loaders


def valid_instance(iid):
    return bool(iid) and "/" not in iid and not iid.startswith(".") and \
        (INSTANCES / iid / "instance.cfg").is_file()


# ---------------------------------------------------------------- jobs
class Job:
    def __init__(self, title):
        self.id = uuid.uuid4().hex[:10]
        self.title = title
        self.state = "running"
        self.step = "Waiting..."
        self.done = 0
        self.total = 0
        self.error = None
        self.instance = None
        self.notes = []
        JOBS[self.id] = self

    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("id", "title", "state", "step", "done", "total", "error", "instance", "notes")}


def best_version(project, mc, loaders):
    try:
        vs = api(f"/project/{project}/version",
                 {"loaders": json.dumps(loaders), "game_versions": json.dumps([mc])})
    except urllib.error.HTTPError:
        return None
    if not vs:
        return None
    rel = [v for v in vs if v.get("version_type") == "release"]
    return (rel or vs)[0]


def present_projects(mods_dir):
    hashes = []
    if mods_dir.is_dir():
        for p in mods_dir.iterdir():
            if p.is_file() and p.suffix in (".jar", ".disabled"):
                hashes.append(hashlib.sha1(p.read_bytes()).hexdigest())
    if not hashes:
        return set()
    try:
        res = api("/version_files", data={"hashes": hashes, "algorithm": "sha1"})
        return {v["project_id"] for v in res.values()}
    except Exception:
        return set()


def add_mod_with_deps(job, mods, first, present, mc, loaders):
    queue, added = [first], []
    while queue:
        v = queue.pop()
        if v["project_id"] in present:
            continue
        present.add(v["project_id"])
        f = next((f for f in v["files"] if f.get("primary")), v["files"][0])
        download(f["url"], safe_join(mods, f["filename"]), sha512=f["hashes"].get("sha512"))
        added.append(f["filename"])
        for d in v.get("dependencies") or []:
            if d.get("dependency_type") != "required" or d.get("project_id") in present:
                continue
            dv = None
            if d.get("version_id"):
                dv = api(f"/version/{d['version_id']}")
            elif d.get("project_id"):
                dv = best_version(d["project_id"], mc, loaders)
            if dv:
                queue.append(dv)
    return added


def add_essentials(job, game, mc, loader):
    mods = game / "mods"
    loaders = [loader] + (["fabric"] if loader == "quilt" else [])
    present = None
    for ess in ESSENTIALS:
        job.step = f"Adding {ess['label'].lower()}"
        names = [p.name.lower() for p in mods.iterdir()] if mods.is_dir() else []
        if any(h in n for n in names for h in ess["hints"]):
            job.notes.append(f"{ess['label']}: already in pack.")
            continue
        first = next((v for v in (best_version(c, mc, loaders) for c in ess["candidates"]) if v), None)
        if not first:
            job.notes.append(f"{ess['label']}: nothing available for {loader} {mc}.")
            continue
        if present is None:
            present = present_projects(mods)
        try:
            added = add_mod_with_deps(job, mods, first, present, mc, loaders)
            if added:
                job.notes.append(f"{ess['label']}: added " + ", ".join(added))
        except Exception as e:
            job.notes.append(f"{ess['label']}: failed ({e})")


def install_mrpack(job, mrpack, display_name=None, icon_url=None, managed=None):
    INSTANCES.mkdir(parents=True, exist_ok=True)
    tmpinst = None
    try:
        with zipfile.ZipFile(mrpack) as z:
            idx = json.loads(z.read("modrinth.index.json"))
            deps = idx.get("dependencies", {})
            mc = deps.get("minecraft")
            if not mc:
                raise ValueError("Pack does not say which Minecraft version it needs")
            loader = next(((k, *LOADERS[k], deps[k]) for k in LOADERS if k in deps), None)
            name = (display_name or idx.get("name") or Path(mrpack).stem).replace("\n", " ").strip()
            job.title = name
            iid = unique_id(name)
            tmpinst = INSTANCES / (".installing-" + iid)
            game = tmpinst / "minecraft"
            game.mkdir(parents=True)

            files = [f for f in idx.get("files", [])
                     if (f.get("env") or {}).get("client") != "unsupported"]
            job.total, job.done = len(files), 0
            job.step = "Downloading mods"
            lock = threading.Lock()

            def fetch(f):
                dest = safe_join(game, f["path"])
                urls = f.get("downloads") or []
                last = None
                for u in urls:
                    try:
                        download(u, dest, f["hashes"].get("sha512"), f["hashes"].get("sha1"))
                        break
                    except Exception as e:
                        last = e
                else:
                    raise last or ValueError(f"No download for {f['path']}")
                with lock:
                    job.done += 1

            with ThreadPoolExecutor(6) as ex:
                for _ in ex.map(fetch, files):
                    pass

            job.step = "Copying configs"
            for prefix in ("overrides/", "client-overrides/"):
                for info in z.infolist():
                    if info.is_dir() or not info.filename.startswith(prefix):
                        continue
                    rel = info.filename[len(prefix):]
                    if not rel:
                        continue
                    dest = safe_join(game, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)

        if loader:
            add_essentials(job, game, mc, loader[1])
        else:
            job.notes.append("Vanilla pack: no mod loader, so essentials were skipped.")

        job.step = "Finishing"
        icon_key = "default"
        if icon_url:
            try:
                ext = Path(urllib.parse.urlparse(icon_url).path).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
                    icon_key = "mp_" + iid
                    download(icon_url, ICONS / (icon_key + ext))
            except Exception:
                icon_key = "default"

        comps = [{"uid": "net.minecraft", "version": mc, "important": True}]
        if loader:
            comps.append({"uid": loader[2], "version": loader[3]})
        inst = INSTANCES / iid
        tmpinst.rename(inst)
        tmpinst = None
        (inst / "mmc-pack.json").write_text(json.dumps({"components": comps, "formatVersion": 1}, indent=2))
        (inst / "instance.cfg").write_text(new_instance_cfg(name, icon_key, managed))
        job.instance = iid
        if not get_default() or not valid_instance(get_default()):
            set_default(iid)
            job.notes.append("Set as the pack the Minecraft shortcut starts.")
        job.state, job.step = "done", "Installed"
    except Exception as e:
        job.state, job.error = "error", str(e)
        if tmpinst and tmpinst.exists():
            shutil.rmtree(tmpinst, ignore_errors=True)


def run_job(job, target):
    with INSTALL_LOCK:
        try:
            target()
        except Exception as e:
            job.state, job.error = "error", str(e)


def install_from_modrinth(job, version_id, title, icon_url):
    job.step = "Fetching pack"
    v = api(f"/version/{version_id}")
    f = next((f for f in v["files"] if f["filename"].endswith(".mrpack")), None)
    if not f:
        raise ValueError("This version has no .mrpack file")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "pack.mrpack"
        download(f["url"], p, f["hashes"].get("sha512"))
        managed = {"ManagedPack": "true", "ManagedPackType": "modrinth",
                   "ManagedPackID": v.get("project_id", ""), "ManagedPackVersionID": v.get("id", ""),
                   "ManagedPackName": title or "", "ManagedPackVersionName": v.get("version_number", "")}
        install_mrpack(job, p, title, icon_url, managed)


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return json.load(r)


def minecraft_releases():
    """Recent release versions that both Fabric and the essentials support."""
    tags = api("/tag/game_version")
    return [t["version"] for t in tags if t.get("version_type") == "release"][:25]


def install_plain(job, mc, name=None):
    """Fabric + essentials only: the starting point for a custom pack."""
    INSTANCES.mkdir(parents=True, exist_ok=True)
    job.step = "Finding Fabric loader"
    loaders = get_json(f"{META_FABRIC}/{urllib.parse.quote(mc)}")
    if not loaders:
        raise ValueError(f"Fabric does not support Minecraft {mc}")
    stable = [l for l in loaders if l["loader"].get("stable")]
    fabric_ver = (stable or loaders)[0]["loader"]["version"]
    name = (name or f"Minecraft {mc}").replace("\n", " ").strip()[:60] or f"Minecraft {mc}"
    job.title = name
    iid = unique_id(name.replace(" ", "-"))
    tmpinst = INSTANCES / (".installing-" + iid)
    try:
        game = tmpinst / "minecraft"
        (game / "mods").mkdir(parents=True)
        add_essentials(job, game, mc, "fabric")
        fapi = best_version("fabric-api", mc, ["fabric"])
        if fapi:
            add_mod_with_deps(job, game / "mods", fapi, present_projects(game / "mods"), mc, ["fabric"])
        job.step = "Finishing"
        inst = INSTANCES / iid
        tmpinst.rename(inst)
        comps = [{"uid": "net.minecraft", "version": mc, "important": True},
                 {"uid": "net.fabricmc.fabric-loader", "version": fabric_ver}]
        (inst / "mmc-pack.json").write_text(json.dumps({"components": comps, "formatVersion": 1}, indent=2))
        (inst / "instance.cfg").write_text(new_instance_cfg(name))
        job.instance = iid
        if not valid_instance(get_default()):
            set_default(iid)
        job.state, job.step = "done", "Installed"
    except Exception:
        shutil.rmtree(tmpinst, ignore_errors=True)
        raise



# ---------------------------------------------------------------- mods in an instance
_HASHES = {}   # path -> (mtime, size, sha1)


def sha1_of(path):
    st = path.stat()
    hit = _HASHES.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    _HASHES[str(path)] = (st.st_mtime, st.st_size, h.hexdigest())
    return h.hexdigest()


def mods_dir(iid):
    return INSTANCES / iid / "minecraft" / "mods"


def mod_files(iid):
    d = mods_dir(iid)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and
                   (p.name.endswith(".jar") or p.name.endswith(".jar.disabled"))),
                  key=lambda p: p.name.lower())


def instance_mods(iid):
    files = mod_files(iid)
    hashes = {p.name: sha1_of(p) for p in files}
    versions, projects = {}, {}
    if hashes:
        try:
            versions = api("/version_files", data={"hashes": list(hashes.values()), "algorithm": "sha1"})
            pids = sorted({v["project_id"] for v in versions.values()})
            if pids:
                projects = {p["id"]: p for p in api("/projects", {"ids": json.dumps(pids)})}
        except Exception:
            pass
    out = []
    for p in files:
        v = versions.get(hashes[p.name]) or {}
        proj = projects.get(v.get("project_id"), {})
        base = p.name[:-9] if p.name.endswith(".disabled") else p.name
        out.append({"file": p.name, "enabled": not p.name.endswith(".disabled"),
                    "title": proj.get("title") or re.sub(r"\.jar$", "", base),
                    "icon_url": proj.get("icon_url"), "project_id": v.get("project_id"),
                    "version": v.get("version_number", ""), "size": p.stat().st_size,
                    "essential": proj.get("slug") in ESSENTIAL_SLUGS})
    out.sort(key=lambda m: (not m["essential"], m["title"].lower()))
    return out


def mod_file(iid, name):
    if not valid_instance(iid) or "/" in name or name.startswith(".") or \
            not (name.endswith(".jar") or name.endswith(".jar.disabled")):
        raise ValueError("unknown mod file")
    p = mods_dir(iid) / name
    if not p.is_file():
        raise ValueError("mod file not found")
    return p


def toggle_mod(iid, name):
    p = mod_file(iid, name)
    new = p.with_name(name[:-9]) if name.endswith(".disabled") else p.with_name(name + ".disabled")
    p.rename(new)
    return new.name


def add_mod(job, iid, project):
    mc, loaders = instance_target(iid)
    if not loaders:
        raise ValueError("This game has no mod loader, so mods can't be added")
    job.step = "Finding a compatible version"
    first = best_version(project, mc, loaders)
    if not first:
        raise ValueError(f"No version for Minecraft {mc} ({loaders[0]})")
    mods = mods_dir(iid)
    mods.mkdir(parents=True, exist_ok=True)
    job.step = "Downloading"
    added = add_mod_with_deps(job, mods, first, present_projects(mods), mc, loaders)
    job.instance = iid
    job.notes = ["Added " + ", ".join(added)] if added else ["Already installed."]
    job.state, job.step = "done", "Installed"


def find_updates(iid):
    mc, loaders = instance_target(iid)
    files = mod_files(iid)
    if not files or not loaders:
        return []
    by_hash = {sha1_of(p): p for p in files}
    body = {"hashes": list(by_hash), "algorithm": "sha1"}
    cur = api("/version_files", data=body)
    new = api("/version_files/update", data=dict(body, loaders=loaders, game_versions=[mc]))
    out = []
    for h, nv in new.items():
        cv = cur.get(h)
        if not cv or nv["id"] == cv["id"] or nv.get("date_published", "") <= cv.get("date_published", ""):
            continue
        f = next((f for f in nv["files"] if f.get("primary")), nv["files"][0])
        out.append({"file": by_hash[h].name, "from": cv.get("version_number", ""),
                    "to": nv.get("version_number", ""), "project_id": nv["project_id"],
                    "url": f["url"], "filename": f["filename"], "sha512": f["hashes"].get("sha512")})
    return out


def update_mods(job, iid):
    job.step = "Checking for updates"
    ups = find_updates(iid)
    job.total, job.done = len(ups), 0
    job.step = "Updating mods"
    mods = mods_dir(iid)
    for u in ups:
        old = mods / u["file"]
        disabled = u["file"].endswith(".disabled")
        dest = safe_join(mods, u["filename"] + (".disabled" if disabled else ""))
        try:
            download(u["url"], dest, sha512=u["sha512"])
            if dest != old:
                old.unlink(missing_ok=True)
            job.notes.append(f"{u['filename']}: {u['from']} -> {u['to']}")
        except Exception as e:
            job.notes.append(f"{u['file']}: failed ({e})")
        job.done += 1
    if not ups:
        job.notes.append("Everything is up to date.")
    job.instance = iid
    job.state, job.step = "done", "Updated"


def delete_instance(iid):
    d = INSTANCES / iid
    trashed = False
    if shutil.which("gio"):
        trashed = subprocess.run(["gio", "trash", str(d)], capture_output=True).returncode == 0
    if not trashed:
        shutil.rmtree(d)
    if get_default() == iid:
        rest = [i["id"] for i in list_instances()]
        if rest:
            set_default(rest[0])
        else:
            DEFAULT_CONF.unlink(missing_ok=True)
    return trashed


def duplicate_instance(iid):
    name = read_cfg(iid).get("name", iid) + " (copy)"
    new = unique_id(name)
    shutil.copytree(INSTANCES / iid, INSTANCES / new, symlinks=True,
                    ignore=shutil.ignore_patterns("logs", "crash-reports", ".cache"))
    write_cfg(new, {"name": name, "lastLaunchTime": 0, "totalTimePlayed": 0})
    return new


def search(q, kind, sort, loader, mc, offset):
    facets = [[f"project_type:{kind}"]]
    if loader:
        facets.append([f"categories:{loader}"] + (["categories:fabric"] if loader == "quilt" else []))
    if mc:
        facets.append([f"versions:{mc}"])
    if kind == "mod":
        facets.append(["client_side:required", "client_side:optional"])
    index = sort if sort in ("relevance", "downloads", "follows", "newest", "updated") else \
        ("relevance" if q else "downloads")
    params = {"facets": json.dumps(facets), "limit": 24, "offset": offset, "index": index}
    if q:
        params["query"] = q
    return api("/search", params)


# ---------------------------------------------------------------- data
def list_instances():
    out, default = [], get_default()
    if not INSTANCES.is_dir():
        return out
    for d in sorted(INSTANCES.iterdir(), key=lambda p: p.name.lower()):
        cfg = d / "instance.cfg"
        if d.name.startswith(".") or not cfg.is_file():
            continue
        info = {"id": d.name, "name": d.name, "icon": None, "mc": "", "loader": "",
                "default": d.name == default, "last": 0, "played": 0, "memory": 0,
                "pack": "", "mods": 0, "worlds": 0}
        for line in cfg.read_text(errors="ignore").splitlines():
            if line.startswith("name="):
                info["name"] = line[5:]
            elif line.startswith("iconKey="):
                info["icon"] = line[8:]
            elif line.startswith("lastLaunchTime="):
                info["last"] = int(line[15:] or 0) if line[15:].isdigit() else 0
            elif line.startswith("totalTimePlayed="):
                info["played"] = int(line[16:] or 0) if line[16:].isdigit() else 0
            elif line.startswith("MaxMemAlloc="):
                info["memory"] = int(line[12:]) if line[12:].isdigit() else 0
            elif line.startswith("ManagedPackName="):
                info["pack"] = line[16:]
        game = d / "minecraft"
        try:
            info["mods"] = sum(1 for p in (game / "mods").iterdir() if p.name.endswith(".jar"))
        except OSError:
            pass
        try:
            info["worlds"] = sum(1 for p in (game / "saves").iterdir() if (p / "level.dat").is_file())
        except OSError:
            pass
        try:
            pack = json.loads((d / "mmc-pack.json").read_text())
            for c in pack.get("components", []):
                if c.get("uid") == "net.minecraft":
                    info["mc"] = c.get("version", "")
                for k, (ln, uid) in LOADERS.items():
                    if c.get("uid") == uid:
                        info["loader"] = ln
        except (OSError, ValueError):
            pass
        out.append(info)
    return out


def list_local():
    out = []
    for base in PACK_DIRS:
        if not base.is_dir():
            continue
        for p in list(base.glob("*.mrpack")) + list(base.glob("*/*.mrpack")):
            st = p.stat()
            out.append({"path": str(p), "name": p.stem, "where": str(p.parent).replace(str(HOME), "~"),
                        "size": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda x: -x["mtime"])


def local_ok(path):
    p = Path(path).resolve()
    return p.suffix == ".mrpack" and p.is_file() and any(b.resolve() in p.parents for b in PACK_DIRS)


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "ModpackPicker"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/fonts/pixel.ttf":
                return self.send(200, (STATIC / "fonts/pixel.ttf").read_bytes(), "font/ttf")
            if u.path == "/api/search":
                kind = "mod" if q.get("kind") == "mod" else "modpack"
                loader = q.get("loader", "")
                mc = q.get("mc", "")
                if loader and loader not in ("fabric", "quilt", "forge", "neoforge"):
                    loader = ""
                if mc and not re.fullmatch(r"[0-9][0-9A-Za-z.\-]*", mc):
                    mc = ""
                return self.send(200, search(q.get("q", ""), kind, q.get("sort", ""), loader, mc,
                                             int(q.get("offset", 0))))
            if u.path == "/api/settings":
                return self.send(200, load_settings())
            if u.path == "/api/instance/mods":
                if not valid_instance(q.get("id", "")):
                    return self.send(400, {"error": "unknown instance"})
                return self.send(200, instance_mods(q["id"]))
            if u.path == "/api/instance/updates":
                if not valid_instance(q.get("id", "")):
                    return self.send(400, {"error": "unknown instance"})
                return self.send(200, [{k: x[k] for k in ("file", "from", "to")} for x in find_updates(q["id"])])
            if u.path == "/api/project":
                pid = q.get("id", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
                    return self.send(400, {"error": "bad id"})
                p = api(f"/project/{pid}")
                vs = api(f"/project/{pid}/version")
                return self.send(200, {
                    "id": p["id"], "title": p["title"], "description": p.get("description", ""),
                    "icon_url": p.get("icon_url"), "downloads": p.get("downloads", 0),
                    "categories": p.get("categories", []), "project_type": p.get("project_type"),
                    "updated": p.get("updated", ""), "gallery": [g["url"] for g in p.get("gallery", [])][:4],
                    "versions": [{"id": v["id"], "name": v["name"], "number": v["version_number"],
                                  "game_versions": v["game_versions"], "loaders": v["loaders"],
                                  "type": v.get("version_type")} for v in vs[:40]]})
            if u.path == "/api/mcversions":
                return self.send(200, minecraft_releases())
            if u.path == "/api/local":
                return self.send(200, list_local())
            if u.path == "/api/instances":
                return self.send(200, list_instances())
            if u.path == "/api/job":
                j = JOBS.get(q.get("id", ""))
                return self.send(200, j.to_dict()) if j else self.send(404, {"error": "no job"})
            if u.path.startswith("/icon/"):
                key = u.path[6:]
                if re.fullmatch(r"[A-Za-z0-9._-]+", key):
                    for p in ICONS.glob(key + ".*"):
                        ctype = {".svg": "image/svg+xml", ".webp": "image/webp", ".gif": "image/gif",
                                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(p.suffix, "image/png")
                        return self.send(200, p.read_bytes(), ctype)
                return self.send(404, {"error": "no icon"})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(502, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if u.path == "/api/install":
            if body.get("version_id"):
                vid = str(body["version_id"])
                if not re.fullmatch(r"[A-Za-z0-9]+", vid):
                    return self.send(400, {"error": "bad version"})
                job = Job(body.get("title") or "Modpack")
                target = lambda: install_from_modrinth(job, vid, body.get("title"), body.get("icon_url"))
            elif body.get("path"):
                if not local_ok(body["path"]):
                    return self.send(400, {"error": "file not allowed"})
                job = Job(Path(body["path"]).stem)
                target = lambda: install_mrpack(job, body["path"])
            elif body.get("plain"):
                mc = str(body["plain"])
                if not re.fullmatch(r"[0-9][0-9A-Za-z.\-]*", mc):
                    return self.send(400, {"error": "bad version"})
                name = str(body.get("name") or "")[:60]
                job = Job(name or f"Minecraft {mc}")
                target = lambda: install_plain(job, mc, name)
            else:
                return self.send(400, {"error": "nothing to install"})
            threading.Thread(target=run_job, args=(job, target), daemon=True).start()
            return self.send(200, job.to_dict())
        if u.path == "/api/default":
            if not valid_instance(body.get("id", "")):
                return self.send(400, {"error": "unknown instance"})
            set_default(body["id"])
            return self.send(200, {"ok": True})
        if u.path == "/api/settings":
            return self.send(200, save_settings(body))
        iid = str(body.get("id", ""))
        if u.path.startswith("/api/instance/") and not valid_instance(iid):
            return self.send(400, {"error": "unknown instance"})
        try:
            if u.path == "/api/instance/mods/add":
                pid = str(body.get("project", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
                    return self.send(400, {"error": "bad project"})
                job = Job(str(body.get("title") or "Mod"))
                threading.Thread(target=run_job, args=(job, lambda: add_mod(job, iid, pid)), daemon=True).start()
                return self.send(200, job.to_dict())
            if u.path == "/api/instance/mods/update":
                job = Job("Updating mods")
                threading.Thread(target=run_job, args=(job, lambda: update_mods(job, iid)), daemon=True).start()
                return self.send(200, job.to_dict())
            if u.path == "/api/instance/mods/toggle":
                return self.send(200, {"file": toggle_mod(iid, str(body.get("file", "")))})
            if u.path == "/api/instance/mods/remove":
                mod_file(iid, str(body.get("file", ""))).unlink()
                return self.send(200, {"ok": True})
            if u.path == "/api/instance/rename":
                name = str(body.get("name", "")).replace("\n", " ").strip()[:60]
                if not name:
                    return self.send(400, {"error": "empty name"})
                write_cfg(iid, {"name": name})
                return self.send(200, {"ok": True})
            if u.path == "/api/instance/memory":
                gb = body.get("gb")
                if not isinstance(gb, int) or not 2 <= gb <= 12:
                    return self.send(400, {"error": "bad memory"})
                write_cfg(iid, {"OverrideMemory": "true", "MaxMemAlloc": gb * 1024,
                                "MinMemAlloc": 512})
                return self.send(200, {"ok": True})
            if u.path == "/api/instance/duplicate":
                with INSTALL_LOCK:
                    return self.send(200, {"id": duplicate_instance(iid)})
            if u.path == "/api/instance/delete":
                with INSTALL_LOCK:
                    return self.send(200, {"trashed": delete_instance(iid)})
        except Exception as e:
            return self.send(400, {"error": str(e)})
        if u.path == "/api/play":
            if not valid_instance(body.get("id", "")):
                return self.send(400, {"error": "unknown instance"})
            result = os.environ.get("PICKER_RESULT")
            if result:
                # The launcher script starts the game in the foreground, so Steam
                # keeps tracking it and can bring you back to the list afterwards.
                Path(result).write_text(body["id"] + "\n")
            else:
                subprocess.Popen(["flatpak", "run", PRISM_APP, "--launch", body["id"]],
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send(200, {"ok": True})
            threading.Timer(1.5, self.server.shutdown).start()
            return
        if u.path == "/api/quit":
            self.send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown).start()
            return
        self.send(404, {"error": "not found"})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 47823
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
