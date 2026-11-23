"""Web quản lý wallpaper (chạy local trên máy có GPU + ComfyUI).

    .venv\\Scripts\\python tools\\admin\\server.py        -> http://127.0.0.1:5000

- Live wallpaper: wallpaper_data.json + video/ + thumb/
- Parallax:       parallax_data.json + 3d/  (tạo ảnh mới qua tools/parallax/pipeline.py)

Chỉ ghi file trong repo, KHÔNG commit/push. Muốn app thấy thay đổi thì commit + push lên main.
"""
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tools" / "parallax"))
import pipeline as plx  # noqa: E402

mimetypes.add_type("image/webp", ".webp")  # Windows registry thường thiếu
mimetypes.add_type("video/mp4", ".mp4")

LIVE_FILE = REPO / "wallpaper_data.json"
LIVE_COUNTERS = HERE / "counters.json"   # số file/id live đã cấp (không dùng lại sau khi xoá)
BASE_URL = "https://livewallpaper-gamma.vercel.app"
THUMB_SIZE = (320, 569)          # đúng cỡ thumb hiện có
DARK_THRESHOLD = 0.35
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SERVE_ROOTS = ("video", "thumb", "3d", "banner", "parallax_work", "parallax_input")

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB / request
data_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify(error=e.msg), e.code


# ----------------------------------------------------------------------------- tiện ích

def cfg():
    return plx.load_config()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def rel_from_url(url):
    return url.split(".vercel.app/", 1)[-1]


def luminance(img):
    g = ImageOps.grayscale(img.convert("RGB"))
    g.thumbnail((200, 200))
    hist = g.histogram()
    return sum(i * n for i, n in enumerate(hist)) / max(1, sum(hist)) / 255.0


def flag(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "on", "yes")


def find_category(data, cid):
    for c in data["category_config"]:
        if c["id"] == cid:
            return c
    raise ApiError(f"Không có category {cid}", 404)


def add_category(data, name, sort, counter_file):
    name = (name or "").strip()
    if not name:
        raise ApiError("Tên category không được trống")
    slug = plx.slugify(name)
    if not slug:
        raise ApiError("Tên category phải có chữ hoặc số (a-z, 0-9)")
    for c in data["category_config"]:
        if plx.slugify(c["nameId"]) == slug:
            raise ApiError(f'Category "{c["nameId"]}" đã tồn tại (id {c["id"]})')
    cid = plx.bump_counter(counter_file, "category", max((c["id"] for c in data["category_config"]), default=0))
    cat = {"id": cid, "nameId": name, "sort": int(sort) if str(sort).strip() not in ("", "None") else 99}
    data["category_config"].append(cat)
    data["wallpaper_config"].setdefault(str(cid), [])
    return cat


def update_category(data, cid, body):
    cat = find_category(data, cid)
    if "nameId" in body:
        name = (body["nameId"] or "").strip()
        if not name:
            raise ApiError("Tên category không được trống")
        for c in data["category_config"]:
            if c["id"] != cid and plx.slugify(c["nameId"]) == plx.slugify(name):
                raise ApiError(f'Trùng tên với category "{c["nameId"]}"')
        cat["nameId"] = name
    if "sort" in body:
        cat["sort"] = int(body["sort"])
    return cat


def reorder_categories(data, ids):
    """Đánh lại sort = 0..n-1 theo thứ tự kéo thả, và xếp category_config theo đúng thứ tự đó."""
    ids = [int(x) for x in ids]
    cats = data["category_config"]
    if sorted(ids) != sorted(c["id"] for c in cats):
        raise ApiError("Danh sách category không khớp (dữ liệu đã đổi?), hãy tải lại trang", 409)
    by_id = {c["id"]: c for c in cats}
    for i, cid in enumerate(ids):
        by_id[cid]["sort"] = i
    data["category_config"] = [by_id[i] for i in ids]


def reorder_items(data, cid, ids):
    """Sắp xếp lại wallpaper trong category theo danh sách id (thứ tự mảng = thứ tự app hiển thị)."""
    find_category(data, cid)
    items = data["wallpaper_config"].get(str(cid), [])
    ids = [int(x) for x in ids]
    if sorted(ids) != sorted(w["id"] for w in items):
        raise ApiError("Danh sách id không khớp với category (dữ liệu đã đổi?), hãy tải lại trang", 409)
    by_id = {w["id"]: w for w in items}
    data["wallpaper_config"][str(cid)] = [by_id[i] for i in ids]


def update_item(data, wid, body):
    for lst in data["wallpaper_config"].values():
        for w in lst:
            if w["id"] == wid:
                for k in ("isPremium", "isDark"):
                    if k in body:
                        w[k] = bool(body[k])
                return w
    raise ApiError(f"Không có wallpaper {wid}", 404)


# ----------------------------------------------------------------------------- trang + file

@app.get("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


def safe_path(rel):
    rel = rel.replace("\\", "/").lstrip("/")
    if rel.split("/", 1)[0] not in SERVE_ROOTS:
        abort(404)
    p = (REPO / rel).resolve()
    if REPO not in p.parents or not p.is_file():
        abort(404)
    return p


@lru_cache(maxsize=512)
def _preview(path_str, mtime, w):
    im = Image.open(path_str)
    im = ImageOps.exif_transpose(im)
    im = im.convert("L" if im.mode in ("L", "I;16") else "RGB")
    im.thumbnail((w, w * 4))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@app.get("/file/<path:rel>")
def serve_file(rel):
    p = safe_path(rel)
    w = request.args.get("w", type=int)
    if w and p.suffix.lower() in IMG_EXT:
        data = _preview(str(p), p.stat().st_mtime, min(max(w, 32), 2000))
        return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=0)
    return send_file(p, max_age=0, conditional=True)


@app.get("/api/status")
def status():
    c = cfg()
    try:
        requests.get(c["comfy_url"] + "/system_stats", timeout=2).raise_for_status()
        comfy = True
    except requests.RequestException:
        comfy = False
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True,
                             timeout=20, encoding="utf-8").stdout
        changes = [l for l in out.splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        changes = None
    return jsonify(comfy=comfy, comfyUrl=c["comfy_url"], gitChanges=changes)


# ----------------------------------------------------------------------------- LIVE WALLPAPER

@app.get("/api/live")
def live_get():
    return jsonify(read_json(LIVE_FILE))


@app.post("/api/live/category")
def live_add_category():
    body = request.get_json(force=True)
    with data_lock:
        data = read_json(LIVE_FILE)
        cat = add_category(data, body.get("nameId"), body.get("sort", 99), LIVE_COUNTERS)
        write_json(LIVE_FILE, data)
    return jsonify(cat)


@app.patch("/api/live/category/<int:cid>")
def live_update_category(cid):
    with data_lock:
        data = read_json(LIVE_FILE)
        cat = update_category(data, cid, request.get_json(force=True))
        write_json(LIVE_FILE, data)
    return jsonify(cat)


@app.put("/api/live/category-order")
def live_reorder_categories():
    with data_lock:
        data = read_json(LIVE_FILE)
        reorder_categories(data, request.get_json(force=True).get("ids", []))
        write_json(LIVE_FILE, data)
    return jsonify({"ok": True})


@app.put("/api/live/category/<int:cid>/order")
def live_reorder(cid):
    with data_lock:
        data = read_json(LIVE_FILE)
        reorder_items(data, cid, request.get_json(force=True).get("ids", []))
        write_json(LIVE_FILE, data)
    return jsonify({"ok": True})


@app.patch("/api/live/wallpaper/<int:wid>")
def live_update_item(wid):
    with data_lock:
        data = read_json(LIVE_FILE)
        w = update_item(data, wid, request.get_json(force=True))
        write_json(LIVE_FILE, data)
    return jsonify(w)


def live_prefix(data, cat):
    """Prefix tên file của category: lấy theo file đã có (vd year_), không có thì slug của tên."""
    counts = {}
    for w in data["wallpaper_config"].get(str(cat["id"]), []):
        m = re.match(r"(.+)_\d+\.mp4$", rel_from_url(w["url"]).rsplit("/", 1)[-1])
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if counts:
        return max(counts, key=counts.get)
    return plx.slugify(cat["nameId"])


# Thumb động: 3 giây đầu video, webp lặp vô hạn, phải < THUMB_MAX_BYTES.
# Thử từ chất lượng cao xuống thấp, ưu tiên giữ fps (chuyển động mượt) rồi mới giảm quality.
THUMB_SECONDS = 3
THUMB_MAX_BYTES = 400 * 1024
THUMB_LADDER = [(15, 75), (15, 60), (12, 55), (12, 40), (10, 40), (10, 25), (8, 25), (8, 15),
                (6, 25), (6, 15), (5, 10), (4, 5)]


def make_anim_thumb(video, out):
    """Tạo thumb webp động từ 3s đầu video. Trả về thông số đã dùng."""
    w, h = THUMB_SIZE
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "t.webp"
        for fps, q in THUMB_LADDER:
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-t", str(THUMB_SECONDS), "-i", str(video), "-an",
                                "-vf", f"fps={fps},{vf}", "-c:v", "libwebp_anim", "-loop", "0", "-q:v", str(q),
                                "-compression_level", "6", "-preset", "picture", str(tmp)],
                               capture_output=True, text=True, timeout=300)
            if not tmp.exists():
                raise ApiError(f"Không tạo được thumb (ffmpeg): {r.stderr[-500:]}")
            size = tmp.stat().st_size
            if size < THUMB_MAX_BYTES:
                shutil.move(str(tmp), str(out))
                return {"fps": fps, "quality": q, "size": size}
    raise ApiError(f"Không nén được thumb dưới {THUMB_MAX_BYTES // 1024}KB kể cả ở mức thấp nhất")


def thumb_is_dark(path):
    im = Image.open(path)
    im.seek(0)
    return luminance(im) < DARK_THRESHOLD


def probe_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,codec_name:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        j = json.loads(r.stdout)
        s = j["streams"][0]
        return {"width": s["width"], "height": s["height"], "codec": s["codec_name"],
                "duration": round(float(j["format"]["duration"]), 2)}
    except Exception:  # noqa: BLE001
        raise ApiError("File tải lên không phải video hợp lệ")


@app.post("/api/live/wallpaper")
def live_add_item():
    cid = request.form.get("categoryId", type=int)
    video = request.files.get("video")
    if not video or not video.filename:
        raise ApiError("Chưa chọn file video")
    if not video.filename.lower().endswith(".mp4"):
        raise ApiError("Video phải là file .mp4")

    with data_lock:
        data = read_json(LIVE_FILE)
        cat = find_category(data, cid)
        prefix = live_prefix(data, cat)
        lst = data["wallpaper_config"].setdefault(str(cid), [])
        if max((w["id"] - cid * 1000 for w in lst), default=0) + 1 >= 1000:
            raise ApiError("Category đã đủ 999 wallpaper")
        # số file và id không bao giờ dùng lại (kể cả sau khi xoá) -> tránh CDN/app cache nội dung cũ
        nums = [int(m.group(1)) for f in (REPO / "video").glob(f"{prefix}_*.mp4")
                if (m := re.fullmatch(re.escape(prefix) + r"_(\d+)\.mp4", f.name))]
        nn = plx.bump_counter(LIVE_COUNTERS, f"file:{prefix}", max(nums, default=0))
        name = f"{prefix}_{nn:02d}"
        n = plx.bump_counter(LIVE_COUNTERS, f"id:{cid}", max((w["id"] - cid * 1000 for w in lst), default=0))
        if n >= 1000:
            raise ApiError("Category đã dùng hết số id (999)")
        wid = cid * 1000 + n

        vpath, tpath = REPO / "video" / f"{name}.mp4", REPO / "thumb" / f"{name}.webp"
        video.save(vpath)
        try:
            info = probe_video(vpath)
            thumb_info = make_anim_thumb(vpath, tpath)
        except Exception:
            vpath.unlink(missing_ok=True)
            tpath.unlink(missing_ok=True)
            raise

        dark = request.form.get("isDark", "auto")
        is_dark = thumb_is_dark(tpath) if dark == "auto" else flag(dark)
        entry = {"categoryId": cid, "id": wid, "isDark": bool(is_dark),
                 "isPremium": flag(request.form.get("isPremium")),
                 "thumb": f"{BASE_URL}/thumb/{name}.webp", "url": f"{BASE_URL}/video/{name}.mp4"}
        lst.append(entry)
        lst.sort(key=lambda e: e["id"])
        write_json(LIVE_FILE, data)
    return jsonify(entry=entry, video=info, thumb=thumb_info, file=name)


def find_item(data, wid):
    for key, lst in data["wallpaper_config"].items():
        for i, w in enumerate(lst):
            if w["id"] == wid:
                return key, i, w
    raise ApiError(f"Không có wallpaper {wid}", 404)


def repo_file(url):
    """Đường dẫn file trong repo từ URL tuyệt đối, chỉ cho phép trong các thư mục asset."""
    p = (REPO / rel_from_url(url)).resolve()
    if REPO not in p.parents or rel_from_url(url).split("/", 1)[0] not in ("video", "thumb", "3d"):
        raise ApiError(f"URL không nằm trong repo: {url}")
    return p


@app.get("/api/live/thumbsizes")
def live_thumb_sizes():
    data = read_json(LIVE_FILE)
    out = {}
    for lst in data["wallpaper_config"].values():
        for w in lst:
            p = repo_file(w["thumb"])
            out[w["id"]] = p.stat().st_size if p.exists() else None
    return jsonify(out)


@app.delete("/api/live/wallpaper/<int:wid>")
def live_delete_item(wid):
    with data_lock:
        data = read_json(LIVE_FILE)
        key, i, w = find_item(data, wid)
        files = [repo_file(w["url"]), repo_file(w["thumb"])]
        del data["wallpaper_config"][key][i]
        write_json(LIVE_FILE, data)
        for f in files:
            f.unlink(missing_ok=True)
    return jsonify(deleted=wid, files=[str(f.relative_to(REPO)) for f in files])


@app.delete("/api/live/category/<int:cid>")
def live_delete_category(cid):
    with data_lock:
        data = read_json(LIVE_FILE)
        cat = find_category(data, cid)
        if data["wallpaper_config"].get(str(cid)):
            raise ApiError("Category còn wallpaper, xoá hết wallpaper trước")
        if any(b["categoryId"] == cid for b in data["banner_config"]):
            raise ApiError("Category đang có banner trong banner_config, không xoá được")
        data["category_config"].remove(cat)
        data["wallpaper_config"].pop(str(cid), None)
        write_json(LIVE_FILE, data)
    return jsonify(deleted=cid)


def regen_thumbs(ids):
    """Tác vụ nền: tạo lại thumb động (< 400KB) cho các wallpaper id."""
    for n, wid in enumerate(ids, 1):
        data = read_json(LIVE_FILE)
        _, _, w = find_item(data, wid)
        video, thumb = repo_file(w["url"]), repo_file(w["thumb"])
        old = thumb.stat().st_size if thumb.exists() else 0
        tmp = thumb.with_name(thumb.stem + ".new.webp")
        try:
            info = make_anim_thumb(video, tmp)
            tmp.replace(thumb)
            jobs.log(f"[{n}/{len(ids)}] {thumb.name}: {old // 1024}KB -> {info['size'] // 1024}KB "
                     f"({info['fps']}fps q{info['quality']})")
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            jobs.log(f"[{n}/{len(ids)}] {thumb.name}: LỖI {getattr(e, 'msg', e)}")


@app.post("/api/live/regen-thumbs")
def live_regen_thumbs():
    body = request.get_json(force=True)
    ids = [int(x) for x in body.get("ids", [])]
    if not ids:
        raise ApiError("Không có wallpaper nào cần tạo lại thumb")
    if not jobs.start_fn(f"Tạo lại {len(ids)} thumb", regen_thumbs, ids):
        raise ApiError("Đang có tác vụ khác chạy")
    return jsonify(ok=True)


# ----------------------------------------------------------------------------- PARALLAX

@app.get("/api/parallax")
def plx_get():
    return jsonify(plx.load_data(cfg()))


@app.post("/api/parallax/category")
def plx_add_category():
    body = request.get_json(force=True)
    c = cfg()
    with data_lock:
        data = plx.load_data(c)
        cat = add_category(data, body.get("nameId"), body.get("sort", 99), plx.COUNTER_FILE)
        plx.save_data(c, data)
    return jsonify(cat)


@app.patch("/api/parallax/category/<int:cid>")
def plx_update_category(cid):
    c = cfg()
    body = request.get_json(force=True)
    with data_lock:
        data = plx.load_data(c)
        old = plx.slugify(find_category(data, cid)["nameId"])
        if "nameId" in body and plx.slugify(body["nameId"] or "") != old and data["wallpaper_config"].get(str(cid)):
            # thư mục 3d/<slug>/ và URL gắn với slug của tên -> chỉ cho đổi tên khi slug giữ nguyên
            raise ApiError("Category đã có wallpaper: chỉ được đổi hoa/thường hoặc dấu cách, "
                           f'tên mới phải có slug "{old}"')
        cat = update_category(data, cid, body)
        plx.save_data(c, data)
    return jsonify(cat)


@app.put("/api/parallax/category-order")
def plx_reorder_categories():
    c = cfg()
    with data_lock:
        data = plx.load_data(c)
        reorder_categories(data, request.get_json(force=True).get("ids", []))
        plx.save_data(c, data)
    return jsonify({"ok": True})


@app.put("/api/parallax/category/<int:cid>/order")
def plx_reorder(cid):
    c = cfg()
    with data_lock:
        data = plx.load_data(c)
        reorder_items(data, cid, request.get_json(force=True).get("ids", []))
        plx.save_data(c, data)
    return jsonify({"ok": True})


@app.patch("/api/parallax/wallpaper/<int:wid>")
def plx_update_item(wid):
    c = cfg()
    with data_lock:
        data = plx.load_data(c)
        w = update_item(data, wid, request.get_json(force=True))
        plx.save_data(c, data)
    return jsonify(w)


@app.delete("/api/parallax/wallpaper/<int:wid>")
def plx_delete_item(wid):
    if jobs.running():
        raise ApiError("Đang có tác vụ chạy, đợi xong rồi xoá")
    c = cfg()
    with data_lock:
        data = plx.load_data(c)
        key, i, w = find_item(data, wid)
        folder = repo_file(w["image"]).parent          # 3d/<slug>/<n>
        rel = folder.relative_to(c["output_dir"]).as_posix()
        del data["wallpaper_config"][key][i]
        plx.save_data(c, data)
        if folder.is_dir():
            shutil.rmtree(folder)
        # xoá work item tương ứng để có thể upload lại đúng ảnh đó
        for p in plx.work_items(c):
            if plx.read_meta(p).get("published") == rel:
                shutil.rmtree(p)
    return jsonify(deleted=wid, folder=f"3d/{rel}")


@app.delete("/api/parallax/category/<int:cid>")
def plx_delete_category(cid):
    c = cfg()
    with data_lock:
        data = plx.load_data(c)
        cat = find_category(data, cid)
        if data["wallpaper_config"].get(str(cid)):
            raise ApiError("Category còn wallpaper, xoá hết wallpaper trước")
        slug = plx.slugify(cat["nameId"])
        if any(plx.read_meta(p)["category"] == slug and plx.read_meta(p)["status"] != plx.PUBLISHED
               for p in plx.work_items(c)):
            raise ApiError("Category còn ảnh trong “Chờ duyệt”, publish hoặc huỷ trước")
        data["category_config"].remove(cat)
        data["wallpaper_config"].pop(str(cid), None)
        plx.save_data(c, data)
        d = c["output_dir"] / slug
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return jsonify(deleted=cid)


@app.post("/api/parallax/upload")
def plx_upload():
    c = cfg()
    cid = request.form.get("categoryId", type=int)
    files = [f for f in request.files.getlist("images") if f and f.filename]
    if not files:
        raise ApiError("Chưa chọn ảnh")
    data = plx.load_data(c)
    slug = plx.slugify(find_category(data, cid)["nameId"])
    for f in files:
        if Path(f.filename).suffix.lower() not in IMG_EXT:
            raise ApiError(f"{f.filename}: chỉ nhận ảnh {', '.join(sorted(IMG_EXT))}")
    dest = c["input_dir"] / slug
    dest.mkdir(parents=True, exist_ok=True)
    # pipeline nhận diện ảnh theo hash -> ảnh đã xử lý (hoặc đang chờ) sẽ bị bỏ qua, báo luôn cho người dùng
    known = {plx.read_meta(p)["hash"] for p in plx.work_items(c)}
    if c["input_dir"].exists():
        known |= {plx.file_hash(x) for x in c["input_dir"].glob("*/*")
                  if x.suffix.lower() in IMG_EXT and not x.parent.name.startswith("_")}
    saved, duplicates = [], []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        stem = plx.slugify(Path(f.filename).stem)[:40] or "img"
        p = dest / f"{stem}{ext}"
        i = 1
        while p.exists():
            p = dest / f"{stem}_{i}{ext}"
            i += 1
        f.save(p)
        try:
            Image.open(p).verify()
        except Exception:  # noqa: BLE001
            p.unlink(missing_ok=True)
            raise ApiError(f"{f.filename}: không đọc được ảnh")
        h = plx.file_hash(p)
        if h in known:
            p.unlink()
            duplicates.append(f.filename)
            continue
        known.add(h)
        saved.append(f"{slug}/{p.name}")
    started = False
    if saved and flag(request.form.get("autoRun"), True):
        started = jobs.start("Tạo parallax (prep + fill)", [["prep"], ["fill"]])
    return jsonify(saved=saved, duplicates=duplicates, jobStarted=started)


@app.get("/api/parallax/pending")
def plx_pending():
    c = cfg()
    data = plx.load_data(c)
    names = {plx.slugify(x["nameId"]): x["nameId"] for x in data["category_config"]}
    items = []
    for p in plx.work_items(c):
        m = plx.read_meta(p)
        if m["status"] == plx.PUBLISHED:
            continue
        items.append({"name": p.name, "status": m["status"], "category": m["category"],
                      "categoryName": names.get(m["category"], m["category"]), "isDark": m["isDark"],
                      "source": m["source"], "fill": m.get("fill"),
                      "mtime": max(f.stat().st_mtime for f in p.iterdir())})
    known = {plx.read_meta(p)["hash"] for p in plx.work_items(c)}
    waiting = []
    if c["input_dir"].exists():
        for d in sorted(x for x in c["input_dir"].iterdir() if x.is_dir() and not x.name.startswith("_")):
            for f in sorted(d.iterdir()):
                if f.suffix.lower() in IMG_EXT and plx.file_hash(f) not in known:
                    waiting.append({"path": f"{d.name}/{f.name}", "category": d.name})
    return jsonify(items=items, waiting=waiting)


def work_item(name):
    c = cfg()
    p = (c["work_dir"] / name).resolve()
    if p.parent != c["work_dir"] or not (p / "meta.json").exists():
        raise ApiError("Không tìm thấy item", 404)
    return c, p


@app.post("/api/parallax/item/<name>/publish")
def plx_publish(name):
    if jobs.running():
        raise ApiError("Đang có tác vụ chạy, đợi xong rồi publish")
    body = request.get_json(silent=True) or {}
    c, item = work_item(name)
    with data_lock:
        data = plx.load_data(c)
        try:
            entry = plx.publish_item(c, data, item, is_premium=flag(body.get("isPremium")),
                                     is_dark=body.get("isDark"))
        except RuntimeError as e:
            raise ApiError(str(e))
        plx.save_data(c, data)
    return jsonify(entry)


@app.post("/api/parallax/item/<name>/refill")
def plx_refill(name):
    body = request.get_json(silent=True) or {}
    work_item(name)
    args = ["refill", name]
    if str(body.get("seed", "")).strip():
        args += ["--seed", str(int(body["seed"]))]
    if (body.get("prompt") or "").strip():
        args += ["--prompt", body["prompt"].strip()]
    if not jobs.start(f"Fill lại {name}", [args]):
        raise ApiError("Đang có tác vụ khác chạy")
    return jsonify(ok=True)


@app.post("/api/parallax/item/<name>/discard")
def plx_discard(name):
    if jobs.running():
        raise ApiError("Đang có tác vụ chạy, đợi xong rồi huỷ")
    c, item = work_item(name)
    meta = plx.read_meta(item)
    if meta["status"] == plx.PUBLISHED:
        raise ApiError("Item đã publish, không huỷ được ở đây")
    src = c["input_dir"] / meta["source"]
    if src.exists():  # chuyển ảnh gốc sang _rejected để prep không làm lại
        rej = c["input_dir"] / "_rejected" / meta["category"]
        rej.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(rej / src.name))
    shutil.rmtree(item)
    return jsonify(ok=True)


@app.post("/api/parallax/run")
def plx_run():
    if not jobs.start("Tạo parallax (prep + fill)", [["prep"], ["fill"]]):
        raise ApiError("Đang có tác vụ khác chạy")
    return jsonify(ok=True)


# ----------------------------------------------------------------------------- tác vụ nền (GPU)

class Jobs:
    """Chạy pipeline.py trong process con, mỗi lúc một tác vụ (tránh tranh VRAM)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "title": None, "started": None, "ended": None, "ok": None, "seq": 0}
        self.lines = deque(maxlen=400)

    def running(self):
        return self.state["running"]

    def _begin(self, title):
        with self.lock:
            if self.state["running"]:
                return False
            self.lines.clear()
            self.state.update(running=True, title=title, started=time.time(), ended=None, ok=None, errors=0,
                              seq=self.state["seq"] + 1)
            return True

    def start(self, title, steps):
        """Chạy lần lượt các lệnh pipeline.py (process con)."""
        if not self._begin(title):
            return False
        threading.Thread(target=self._run, args=(steps,), daemon=True).start()
        return True

    def start_fn(self, title, fn, *args):
        """Chạy một hàm Python trong thread nền, hàm ghi log qua jobs.log()."""
        if not self._begin(title):
            return False

        def run():
            ok = True
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                ok = False
                self.log(f"! {getattr(e, 'msg', None) or repr(e)}")
            finally:
                errors = sum(1 for l in self.lines if "LỖI" in l)
                with self.lock:
                    self.state.update(running=False, ended=time.time(), ok=ok and errors == 0, errors=errors)

        threading.Thread(target=run, daemon=True).start()
        return True

    def log(self, line):
        self.lines.append(time.strftime("[%H:%M:%S] ") + line)

    def _emit(self, line):
        self.lines.append(line.rstrip())

    def _run(self, steps):
        ok = True
        try:
            for args in steps:
                cmd = [sys.executable, "-u", str(REPO / "tools" / "parallax" / "pipeline.py"), *args]
                self._emit(f"$ pipeline.py {' '.join(args)}")
                env = {**os.environ,"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                       "HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
                       "TRANSFORMERS_VERBOSITY": "error", "PYTHONWARNINGS": "ignore"}
                p = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", env=env)
                for line in p.stdout:
                    if line.strip():
                        self._emit(line)
                p.wait()
                if p.returncode != 0:
                    ok = False
                    self._emit(f"! pipeline.py {args[0]} thoát với mã {p.returncode}")
                    break
        except Exception as e:  # noqa: BLE001
            ok = False
            self._emit(f"! {e!r}")
        finally:
            # pipeline bắt lỗi từng ảnh và chỉ ghi "LỖI" vào log -> vẫn tính là có lỗi
            errors = sum(1 for l in self.lines if "LỖI" in l)
            with self.lock:
                self.state.update(running=False, ended=time.time(), ok=ok and errors == 0, errors=errors)

    def snapshot(self):
        return {**self.state, "log": list(self.lines)}


jobs = Jobs()


@app.get("/api/job")
def job_state():
    return jsonify(jobs.snapshot())


def seed_live_counters():
    data = read_json(LIVE_FILE)
    cur = {"category": max((c["id"] for c in data["category_config"]), default=0)}
    for key, lst in data["wallpaper_config"].items():
        cid = int(key)
        cur[f"id:{cid}"] = max((w["id"] - cid * 1000 for w in lst), default=0)
    for f in (REPO / "video").glob("*.mp4"):
        if m := re.fullmatch(r"(.+)_(\d+)\.mp4", f.name):
            k = f"file:{m.group(1)}"
            cur[k] = max(cur.get(k, 0), int(m.group(2)))
    plx.seed_counters(LIVE_COUNTERS, cur)


if __name__ == "__main__":
    import argparse
    seed_live_counters()
    plx.seed_parallax_counters(cfg())
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"Web quản lý wallpaper: http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, threaded=True, debug=False)
