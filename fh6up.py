import argparse
import glob
import json
import os
import re
import shutil
import string
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "0.2.0"

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "fh6up")
TOOLS_DIR = os.path.join(APP_DIR, "tools")
TEXCONV = os.path.join(TOOLS_DIR, "texconv.exe")
ESRGAN_DIR = os.path.join(TOOLS_DIR, "realesrgan")
ESRGAN = os.path.join(ESRGAN_DIR, "realesrgan-ncnn-vulkan.exe")
ESRGAN_MODEL = "realesrgan-x4plus"
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

SCALES = (2, 4)
MIN_PIXELS = 64 * 64
SKIP_PATTERNS = ("/stitches/",)
MAT_PREFIX = "game:\\media\\cars\\_library\\materials\\"
TEX_PREFIX = "game:\\media\\cars\\_library\\textures\\"
STRING_RE = re.compile(rb"[\x20-\x7e]{6,}")

FORMATS = {
    0: ("BC1_UNORM", 71, 8),
    3: ("BC4_UNORM", 80, 8),
    5: ("BC5_UNORM", 83, 16),
    9: ("BC7_UNORM", 98, 16),
}

SMOKE = [
    "Textures/Swatches/SMOKE_Twisty_Flipbook_6P_Diff_4a48e609-f55a-4c3c-a2d6-c2830f86f34c.swatchbin",
    "Textures/Swatches/SMOKE_Twisty_Flipbook_6P_Motion_3121b4f0-b713-4bdf-beb8-90ebe45e7219.swatchbin",
    "Textures/Swatches/SMOKE_Twisty_Flipbook_6P_Norm_2e878d46-aa28-47a2-a296-cf4690712e39.swatchbin",
    "Textures/Swatches/SMOKE_Twisty_Flipbook_6P_OFB_5f2049fc-e187-47b4-8915-3bd987e419f5.swatchbin",
    "Textures/Swatches/SMOKE_Twisty_Flipbook_6P_RLTB_393265f6-a16e-4de5-ab1f-4408d3c5b94f.swatchbin",
    "Textures/Swatches/SMOKE_Heavy_Flipbook_6P_Diff_4a48e609-f55a-4c3c-a2d6-c2830f86f34c.swatchbin",
    "Textures/Swatches/SMOKE_Heavy_Flipbook_6P_Motion_3121b4f0-b713-4bdf-beb8-90ebe45e7219.swatchbin",
    "Textures/Swatches/SMOKE_Heavy_Flipbook_6P_Norm_2e878d46-aa28-47a2-a296-cf4690712e39.swatchbin",
    "Textures/Swatches/SMOKE_Heavy_Flipbook_6P_OFB_5f2049fc-e187-47b4-8915-3bd987e419f5.swatchbin",
    "Textures/Swatches/SMOKE_Heavy_Flipbook_6P_RLTB_393265f6-a16e-4de5-ab1f-4408d3c5b94f.swatchbin",
]

print_lock = threading.Lock()
proc_lock = threading.Lock()
running_procs = set()
stop_event = threading.Event()


def log(msg):
    with print_lock:
        print(msg, flush=True)


def die(msg):
    log(msg)
    sys.exit(1)


# ---------------------------------------------------------------- game paths

def is_game_root(path):
    return os.path.exists(os.path.join(path, "media", "cars", "_library", "Textures.zip"))


def candidate_roots():
    drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    for d in drives:
        yield os.path.join(d, "XboxGames", "Forza Horizon 6", "Content")
        yield os.path.join(d, "SteamLibrary", "steamapps", "common", "ForzaHorizon6")
        yield os.path.join(d, "Program Files (x86)", "Steam", "steamapps", "common", "ForzaHorizon6")
        yield os.path.join(d, "Program Files", "Steam", "steamapps", "common", "ForzaHorizon6")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def find_game(explicit=None):
    if explicit:
        if not is_game_root(explicit):
            die(f"{explicit} does not look like a Forza Horizon 6 install (no media\\cars\\_library\\Textures.zip).")
        return explicit
    cfg = load_config()
    if cfg.get("game") and is_game_root(cfg["game"]):
        return cfg["game"]
    for root in candidate_roots():
        if is_game_root(root):
            cfg["game"] = root
            save_config(cfg)
            return root
    die("Could not find Forza Horizon 6. Pass --game with the folder that contains 'media'.")


class Paths:
    def __init__(self, game, target, scale):
        self.game = game
        self.target = target
        self.scale = scale
        if target == "cars":
            self.source_zip = os.path.join(game, "media", "cars", "_library", "Textures.zip")
            self.output_zip = os.path.join(game, "mediapc", "cars", "_library", "Textures.zip")
        else:
            self.source_zip = os.path.join(game, "media", "Particles.zip")
            self.output_zip = os.path.join(game, "mediapc", "Particles.zip")
        self.materials_zip = os.path.join(game, "media", "cars", "_library", "Materials.zip")
        self.cars_dir = os.path.join(game, "media", "cars")
        work = os.path.join(APP_DIR, "work", target)
        self.original = os.path.join(work, "original")
        self.dds = os.path.join(work, "dds")
        self.png = os.path.join(work, "png")
        self.png_up = os.path.join(work, "png_up")
        self.dds_up = os.path.join(work, f"dds_x{scale}")
        self.replaced = os.path.join(work, f"replaced_x{scale}")
        self.tmp = os.path.join(work, "tmp")


# ---------------------------------------------------------------- tools

def github_asset(repo, predicate):
    url = f"https://api.github.com/repos/{repo}/releases"
    req = urllib.request.Request(url, headers={"User-Agent": f"fh6up/{VERSION}"})
    with urllib.request.urlopen(req) as r:
        releases = json.load(r)
    for rel in releases:
        for asset in rel.get("assets", []):
            if predicate(asset["name"]):
                return asset["browser_download_url"], asset["name"]
    raise RuntimeError(f"No matching release asset found in {repo}")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": f"fh6up/{VERSION}"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def cmd_setup_tools(args):
    os.makedirs(TOOLS_DIR, exist_ok=True)
    if not os.path.exists(TEXCONV):
        url, _ = github_asset("microsoft/DirectXTex", lambda n: n == "texconv.exe")
        log(f"Downloading texconv from {url}")
        download(url, TEXCONV)
    if not os.path.exists(ESRGAN):
        url, name = github_asset("xinntao/Real-ESRGAN",
                                 lambda n: re.search(r"realesrgan-ncnn-vulkan.*windows\.zip$", n) is not None)
        archive = os.path.join(TOOLS_DIR, name)
        log(f"Downloading Real-ESRGAN from {url}")
        download(url, archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(ESRGAN_DIR)
        os.remove(archive)
        found = glob.glob(os.path.join(ESRGAN_DIR, "**", "realesrgan-ncnn-vulkan.exe"), recursive=True)
        if not found:
            die("Real-ESRGAN archive did not contain realesrgan-ncnn-vulkan.exe")
        src_dir = os.path.dirname(found[0])
        if os.path.normcase(src_dir) != os.path.normcase(ESRGAN_DIR):
            for item in os.listdir(src_dir):
                shutil.move(os.path.join(src_dir, item), os.path.join(ESRGAN_DIR, item))
    log(f"Tools ready in {TOOLS_DIR}")


def require_tools():
    if not (os.path.exists(TEXCONV) and os.path.exists(ESRGAN)):
        die("Tools missing. Run: python fh6up.py setup-tools")


# ---------------------------------------------------------------- texture selection

def strings(data):
    return [m.decode("ascii").lower() for m in STRING_RE.findall(data)]


def library_paths(data, prefix, ext):
    found = set()
    for s in strings(data):
        i = s.find(prefix)
        if i < 0:
            continue
        path = s[i:]
        j = path.find(ext)
        if j >= 0:
            found.add(path[len(prefix):j + len(ext)].replace("\\", "/"))
    return found


def interior_textures(paths, car_zips):
    materials, textures = set(), set()
    scanned = 0
    for car in car_zips:
        try:
            with zipfile.ZipFile(car) as z:
                for n in z.namelist():
                    if n.lower().startswith("scene/interior") and n.endswith(".modelbin"):
                        data = z.read(n)
                        materials |= library_paths(data, MAT_PREFIX, ".materialbin")
                        textures |= library_paths(data, TEX_PREFIX, ".swatchbin")
            scanned += 1
        except (zipfile.BadZipFile, OSError):
            continue
    log(f"Scanned {scanned} car file(s)")

    with zipfile.ZipFile(paths.materials_zip) as mats:
        names = {n.lower(): n for n in mats.namelist()}
        for m in materials:
            real = names.get(m)
            if real:
                textures |= library_paths(mats.read(real), TEX_PREFIX, ".swatchbin")

    selected = []
    with zipfile.ZipFile(paths.source_zip) as texs:
        names = {n.lower(): n for n in texs.namelist()}
        for t in sorted(textures):
            if any(p in t for p in SKIP_PATTERNS):
                continue
            real = names.get(t)
            if not real:
                continue
            with texs.open(real) as f:
                head = f.read(120)
            if len(head) < 120 or head[:4] != b"burG":
                continue
            w, h = struct.unpack_from("<II", head, 76)
            code = struct.unpack_from("<I", head, 116)[0]
            if w * h >= MIN_PIXELS and code in FORMATS:
                selected.append(real)
    return selected


def resolve_set(args, paths):
    if args.set == "smoke":
        return SMOKE
    if args.set == "all":
        return interior_textures(paths, sorted(glob.glob(os.path.join(paths.cars_dir, "*.zip"))))
    car_zip = os.path.join(paths.cars_dir, f"{args.set}.zip")
    if not os.path.exists(car_zip):
        die(f"No car named {args.set}. Car names are the zip names in {paths.cars_dir}, e.g. MER_E63SAMG_18")
    return interior_textures(paths, [car_zip])


# ---------------------------------------------------------------- swatchbin format

def parse_swatchbin(data):
    if data[:4] != b"burG":
        raise ValueError("not a swatchbin")
    header_size = struct.unpack_from("<I", data, 8)[0]
    data_size = struct.unpack_from("<I", data, 36)[0]
    width, height = struct.unpack_from("<II", data, 76)
    mips = data[90]
    fmt_code = struct.unpack_from("<I", data, 116)[0]
    if fmt_code not in FORMATS:
        raise ValueError(f"unsupported format code {fmt_code}")
    return width, height, mips, fmt_code, data[header_size:header_size + data_size]


def mip_sizes(width, height, mips, block):
    sizes = []
    w, h = width, height
    for _ in range(mips):
        sizes.append(max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block)
        w, h = max(1, w // 2), max(1, h // 2)
    return sizes


def build_swatchbin(original, width, height, mips, block, pixels):
    sizes = mip_sizes(width, height, mips, block)
    total = sum(sizes)
    if len(pixels) < total:
        raise ValueError(f"pixel data too short: {len(pixels)} < {total}")
    pixels = pixels[:total]

    meta_size = 88 + 12 * (mips - 1)
    header_size = 52 + meta_size

    head = bytearray(original[:128])
    struct.pack_into("<I", head, 8, header_size)
    struct.pack_into("<I", head, 12, header_size + total)
    struct.pack_into("<I", head, 32, header_size)
    struct.pack_into("<I", head, 36, total)
    struct.pack_into("<I", head, 40, total)
    low = struct.unpack_from("<H", original, 48)[0] & 0xF
    struct.pack_into("<H", head, 48, (meta_size << 4) | low)
    struct.pack_into("<II", head, 76, width, height)
    head[90] = mips

    table = bytearray()
    offset = 0
    for i, size in enumerate(sizes):
        nxt = 0xFFFFFFFF if i == mips - 1 else 80 + 12 * (i + 1)
        table += struct.pack("<III", size, offset, nxt)
        offset += size
    return bytes(head) + bytes(table) + pixels


def write_dds(path, width, height, mips, dxgi, block, pixels):
    mip0 = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block
    header = struct.pack("<4sIIIIIII11I", b"DDS ", 124, 0x000A1007, height, width, mip0, 0, mips, *([0] * 11))
    pixel_format = struct.pack("<II4sIIIII", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    caps = struct.pack("<IIIII", 0x401008, 0, 0, 0, 0)
    dx10 = struct.pack("<IIIII", dxgi, 3, 0, 1, 0)
    with open(path, "wb") as f:
        f.write(header + pixel_format + caps + dx10 + pixels)


def read_dds(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"DDS ":
        raise ValueError(f"{path} is not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    mips = max(1, struct.unpack_from("<I", data, 28)[0])
    offset = 148 if data[84:88] == b"DX10" else 128
    return width, height, mips, data[offset:]


# ---------------------------------------------------------------- processing

def plain_resize(entry, fmt_code):
    name = entry.lower().rsplit("/", 1)[-1]
    if fmt_code == 5 or "_motion" in name:
        return True
    return any(tag in name for tag in ("nrml", "_normal", "_norm_"))


def run(cmd):
    if stop_event.is_set():
        raise RuntimeError("stopped")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with proc_lock:
        running_procs.add(proc)
    try:
        _, err = proc.communicate()
    finally:
        with proc_lock:
            running_procs.discard(proc)
    if proc.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed ({proc.returncode}): {err.strip()[:300]}")


def kill_children():
    with proc_lock:
        for proc in list(running_procs):
            try:
                proc.kill()
            except OSError:
                pass


def local(base, entry, ext=None):
    path = os.path.join(base, *entry.split("/"))
    if ext:
        path = os.path.splitext(path)[0] + ext
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def prep_one(paths, entry):
    up_dds = local(paths.dds_up, entry, ".dds")
    if os.path.exists(up_dds):
        return "skipped"

    with zipfile.ZipFile(paths.source_zip) as z:
        data = z.read(entry)
    with open(local(paths.original, entry), "wb") as f:
        f.write(data)

    width, height, mips, fmt_code, pixels = parse_swatchbin(data)
    fmt_name, dxgi, block = FORMATS[fmt_code]
    dds_path = local(paths.dds, entry, ".dds")
    write_dds(dds_path, width, height, mips, dxgi, block, pixels)

    out_w, out_h = str(width * paths.scale), str(height * paths.scale)
    tmp_dir = tempfile.mkdtemp(dir=paths.tmp)
    try:
        if plain_resize(entry, fmt_code):
            run([TEXCONV, "-w", out_w, "-h", out_h, "-if", "CUBIC",
                 "-f", fmt_name, "-m", "0", "-ft", "dds", "-y", "-o", tmp_dir, dds_path])
        else:
            png_path = local(paths.png, entry, ".png")
            up_png = local(paths.png_up, entry, ".png")
            run([TEXCONV, "-ft", "png", "-f", "R8G8B8A8_UNORM", "-m", "1", "-y",
                 "-o", os.path.dirname(png_path), dds_path])
            run([ESRGAN, "-i", png_path, "-o", up_png, "-n", ESRGAN_MODEL, "-s", "4"])
            run([TEXCONV, "-w", out_w, "-h", out_h, "-if", "FANT",
                 "-f", fmt_name, "-m", "0", "-ft", "dds", "-y", "-o", tmp_dir, up_png])
            remove_quiet(png_path)
            remove_quiet(up_png)
        produced = os.path.join(tmp_dir, os.path.basename(up_dds))
        if not os.path.exists(produced):
            raise RuntimeError("texconv produced no output")
        os.replace(produced, up_dds)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        remove_quiet(dds_path)
    return f"{width}x{height} {fmt_name}"


def calm_pixels(original, pixels, scale, block):
    ow, oh, omips, _, opixels = parse_swatchbin(original)
    w, h = ow * scale, oh * scale
    keep = {2: 1, 4: 2}[scale]
    head = sum(mip_sizes(w, h, omips + keep, block)[:keep])
    orig_total = sum(mip_sizes(ow, oh, omips, block))
    if len(pixels) < head or len(opixels) < orig_total:
        return None
    return pixels[:head] + opixels[:orig_total], omips + keep


def estimate_seconds(width, height, entry, fmt_code):
    if plain_resize(entry, fmt_code):
        return 0.5
    side = max(width, height)
    for limit, secs in ((128, 1.5), (256, 2.0), (512, 4.0), (1024, 10.0)):
        if side <= limit:
            return secs
    return 30.0


# ---------------------------------------------------------------- commands

def cmd_detect(args):
    game = find_game(args.game)
    log(f"Game found: {game}")
    cars = glob.glob(os.path.join(game, "media", "cars", "*.zip"))
    log(f"{len(cars)} car files")


def cmd_list(args, paths, entries):
    buckets = {}
    seconds = 0.0
    done = 0
    with zipfile.ZipFile(paths.source_zip) as z:
        for e in entries:
            with z.open(e) as f:
                head = f.read(120)
            w, h = struct.unpack_from("<II", head, 76)
            code = struct.unpack_from("<I", head, 116)[0]
            key = f"{w}x{h}"
            buckets[key] = buckets.get(key, 0) + 1
            if os.path.exists(local(paths.dds_up, e, ".dds")):
                done += 1
            else:
                seconds += estimate_seconds(w, h, e, code)
    for key in sorted(buckets, key=lambda k: -buckets[k]):
        log(f"  {buckets[key]:>6}  {key}")
    log(f"{len(entries)} textures, {done} already prepared")
    log(f"Estimated prep time with {args.workers} workers: about {seconds / args.workers / 3600:.1f} hours")


def cmd_prep(args, paths, entries):
    require_tools()
    os.makedirs(paths.tmp, exist_ok=True)
    total = len(entries)
    log(f"Preparing {total} textures with {args.workers} workers. Ctrl+C stops cleanly and can be resumed.")
    done = failed = 0
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(prep_one, paths, e): e for e in entries}
    try:
        for fut in as_completed(futures):
            entry = futures[fut]
            done += 1
            try:
                log(f"[{done}/{total}] {fut.result()}  {entry}")
            except Exception as ex:
                failed += 1
                log(f"[{done}/{total}] FAILED  {entry}: {ex}")
    except KeyboardInterrupt:
        log("Stopping...")
        stop_event.set()
        pool.shutdown(wait=False, cancel_futures=True)
        kill_children()
        log("Stopped. Run the same command again to resume.")
        os._exit(1)
    pool.shutdown()
    log(f"Prep finished, {failed} failed. Next: build")


def cmd_build(args, paths, entries):
    built = missing = 0
    for entry in entries:
        orig_path = local(paths.original, entry)
        up_dds = local(paths.dds_up, entry, ".dds")
        if not (os.path.exists(orig_path) and os.path.exists(up_dds)):
            missing += 1
            continue
        with open(orig_path, "rb") as f:
            original = f.read()
        ow, oh, omips, code, opixels = parse_swatchbin(original)
        block = FORMATS[code][2]
        if build_swatchbin(original, ow, oh, omips, block, opixels) != original:
            log(f"Self-test failed for {entry} (format code {code}), skipped")
            continue
        try:
            w, h, mips, pixels = read_dds(up_dds)
            if args.calm and (w, h) == (ow * paths.scale, oh * paths.scale):
                merged = calm_pixels(original, pixels, paths.scale, block)
                if merged and merged[1] == mips:
                    pixels = merged[0]
            out = build_swatchbin(original, w, h, mips, block, pixels)
            check = parse_swatchbin(out)
        except ValueError as ex:
            log(f"Bad upscaled file for {entry} ({ex}); delete it and rerun prep")
            continue
        if check[:3] != (w, h, mips):
            log(f"Verification failed for {entry}, skipped")
            continue
        with open(local(paths.replaced, entry), "wb") as f:
            f.write(out)
        built += 1
    log(f"Built {built} of {len(entries)} ({missing} not prepared). Next: pack")


def cmd_pack(args, paths):
    replacements = {}
    for root, _, files in os.walk(paths.replaced):
        for name in files:
            if name.endswith(".swatchbin"):
                full = os.path.join(root, name)
                entry = os.path.relpath(full, paths.replaced).replace("\\", "/")
                if not any(p in entry.lower() for p in SKIP_PATTERNS):
                    replacements[entry] = full
    if not replacements:
        die("Nothing built yet. Run prep and build first.")

    with zipfile.ZipFile(paths.source_zip) as src:
        names = set(src.namelist())
        unknown = [e for e in replacements if e not in names]
        if unknown:
            die("Built files that don't exist in the game archive:\n  " + "\n  ".join(unknown[:20]))
        os.makedirs(os.path.dirname(paths.output_zip), exist_ok=True)
        tmp = paths.output_zip + ".tmp"
        with zipfile.ZipFile(tmp, "w") as dst:
            for i, info in enumerate(src.infolist(), 1):
                if info.filename in replacements:
                    with open(replacements[info.filename], "rb") as f:
                        payload = f.read()
                else:
                    payload = src.read(info.filename)
                out_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                out_info.compress_type = info.compress_type
                out_info.external_attr = info.external_attr
                dst.writestr(out_info, payload)
                if i % 3000 == 0:
                    log(f"  {i} entries written")
    try:
        os.replace(tmp, paths.output_zip)
    except PermissionError:
        die(f"The game has {paths.output_zip} open. Close the game and run pack again.")
    log(f"Installed {paths.output_zip} with {len(replacements)} upscaled textures")


def cmd_restore(args, paths):
    if os.path.exists(paths.output_zip):
        try:
            os.remove(paths.output_zip)
        except PermissionError:
            die("The game is running. Close it and run restore again.")
        log(f"Removed {paths.output_zip}")
    else:
        log("Nothing installed for this target.")


def main():
    parser = argparse.ArgumentParser(prog="fh6up", description="Upscale Forza Horizon 6 car interior and smoke textures on your own install.")
    parser.add_argument("--game", help="Folder containing the game's 'media' folder (auto-detected if omitted)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel textures during prep (default 4)")
    parser.add_argument("--scale", type=int, default=2, choices=SCALES,
                        help="Resolution multiplier per side: 2 (default, 4x the pixels) or 4 (16x the pixels, much more VRAM)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="Find the game install")
    sub.add_parser("setup-tools", help="Download texconv and Real-ESRGAN")
    for name in ("list", "prep", "build"):
        p = sub.add_parser(name)
        p.add_argument("set", help="A car name (e.g. MER_E63SAMG_18), 'all' for every car interior, or 'smoke'")
        if name == "build":
            p.add_argument("--calm", action="store_true",
                           help="Use upscaled data only for the largest mip levels and the original data below. "
                                "Keeps the extra detail up close and stops fine patterns shimmering at a distance.")
    for name in ("pack", "restore"):
        p = sub.add_parser(name)
        p.add_argument("target", nargs="?", default="cars", choices=["cars", "smoke"])

    args = parser.parse_args()

    if args.command == "setup-tools":
        cmd_setup_tools(args)
        return
    game = find_game(args.game)
    if args.command == "detect":
        cmd_detect(args)
        return

    if args.command in ("pack", "restore"):
        paths = Paths(game, "particles" if args.target == "smoke" else "cars", args.scale)
        (cmd_pack if args.command == "pack" else cmd_restore)(args, paths)
        return

    paths = Paths(game, "particles" if args.set == "smoke" else "cars", args.scale)
    entries = resolve_set(args, paths)
    {"list": cmd_list, "prep": cmd_prep, "build": cmd_build}[args.command](args, paths, entries)


if __name__ == "__main__":
    main()
