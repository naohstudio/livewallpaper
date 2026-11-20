#!/usr/bin/env python3
"""Pipeline tạo wallpaper parallax hàng loạt.

Ảnh nguồn đặt trong  <input_dir>/<category>/*.jpg|png|webp
Kết quả:             3d/<category>/<n>/{image,image_background,image_depth}.jpg
                     + entry mới trong parallax_data.json

Các giai đoạn (chạy tuần tự để Python và ComfyUI không tranh VRAM):
  prep    : crop theo chủ thể -> upscale -> 1536x2752, tạo depth + mask   (Python/GPU)
  fill    : xoá chủ thể bằng Flux Fill để tạo background                 (ComfyUI)
  publish : đánh số, tạo id, copy vào 3d/, cập nhật JSON, tạo ảnh review

Lệnh:
  python tools/parallax/pipeline.py run                 # cả 3 giai đoạn
  python tools/parallax/pipeline.py prep|fill|publish   # từng giai đoạn
  python tools/parallax/pipeline.py refill anime/3 [--seed 123] [--prompt "..."]
  python tools/parallax/pipeline.py status
"""
import argparse
import hashlib
import json
import math
import random
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageOps

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# Trạng thái của một work item
PREPPED, FILLED, PUBLISHED = "prepped", "filled", "published"


# ----------------------------------------------------------------------------- tiện ích

def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def load_config():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    for k in ("input_dir", "work_dir", "output_dir", "data_file"):
        cfg[k] = (REPO / cfg[k]).resolve()
    cfg["target_w"], cfg["target_h"] = cfg["target_size"]
    return cfg


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def file_hash(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def read_meta(item):
    return json.loads((item / "meta.json").read_text(encoding="utf-8"))


def write_meta(item, meta):
    (item / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def work_items(cfg, status=None):
    if not cfg["work_dir"].exists():
        return []
    items = sorted(p for p in cfg["work_dir"].iterdir() if (p / "meta.json").exists())
    return [p for p in items if status is None or read_meta(p)["status"] == status]


def dilate(mask_u8, px):
    """Nới rộng mask nhị phân (uint8 0/255) thêm px pixel."""
    import cv2
    if px <= 0:
        return mask_u8
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask_u8, k)


# ----------------------------------------------------------------------------- PREP

class Prep:
    """Các model chạy trực tiếp bằng PyTorch: BiRefNet, Depth Anything V2, upscaler (spandrel)."""

    def __init__(self, cfg):
        import torch
        self.torch = torch
        self.cfg = cfg
        self.cuda = torch.cuda.is_available()
        self.dev = "cuda" if self.cuda else "cpu"
        if not self.cuda:
            log("CẢNH BÁO: không thấy CUDA, chạy trên CPU sẽ rất chậm")
        self._seg = None
        self._depth = None
        self._up = {}

    # -- tách chủ thể (BiRefNet)
    def subject_mask(self, img):
        """Trả về mask float32 HxW trong [0,1], 1 = chủ thể."""
        torch = self.torch
        if self._seg is None:
            from transformers import AutoModelForImageSegmentation
            log(f"Load {self.cfg['seg_model']}")
            torch.set_float32_matmul_precision("high")
            m = AutoModelForImageSegmentation.from_pretrained(self.cfg["seg_model"], trust_remote_code=True)
            m.to(self.dev).eval()
            if self.cuda:
                m.half()
            self._seg = m
        size = self.cfg["seg_size"]
        x = np.asarray(img.convert("RGB").resize((size, size), Image.BILINEAR), dtype=np.float32) / 255.0
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        t = torch.from_numpy(x).permute(2, 0, 1)[None].to(self.dev, torch.float16 if self.cuda else torch.float32)
        with torch.no_grad():
            pred = self._seg(t)[-1].sigmoid()[0, 0].float().cpu().numpy()
        m = Image.fromarray((pred * 255).astype(np.uint8)).resize(img.size, Image.BILINEAR)
        return np.asarray(m, dtype=np.float32) / 255.0

    # -- depth (Depth Anything V2): trắng = gần
    def _depth_once(self, img):
        torch = self.torch
        proc, model = self._depth
        n = self.cfg["depth_input_size"]
        pv = proc(images=img, return_tensors="pt", size={"height": n, "width": n})["pixel_values"]
        pv = pv.to(self.dev, torch.float16 if self.cuda else torch.float32)
        with torch.no_grad():
            pd = model(pixel_values=pv).predicted_depth
        pd = pd.reshape(1, 1, *pd.shape[-2:]).float()
        return torch.nn.functional.interpolate(pd, size=(img.height, img.width), mode="bicubic", align_corners=False)

    def depth(self, img):
        torch = self.torch
        if self._depth is None:
            from transformers import AutoImageProcessor, AutoModelForDepthEstimation
            name = self.cfg["depth_model"]
            log(f"Load {name}")
            model = AutoModelForDepthEstimation.from_pretrained(
                name, torch_dtype=torch.float16 if self.cuda else torch.float32).to(self.dev).eval()
            self._depth = (AutoImageProcessor.from_pretrained(name), model)
        d = self._depth_once(img)
        if self.cfg.get("depth_flip_tta"):
            # chạy thêm trên ảnh lật ngang rồi lấy trung bình -> depth ổn định hơn
            d = (d + self._depth_once(ImageOps.mirror(img)).flip(-1)) / 2
        d = d[0, 0].cpu().numpy()
        lo, hi = np.percentile(d, 1), np.percentile(d, 99.5)
        d = np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1)
        out = Image.fromarray((d * 255).astype(np.uint8), "L")
        blur = self.cfg.get("depth_blur", 0)
        if blur:
            from PIL import ImageFilter
            out = out.filter(ImageFilter.GaussianBlur(blur))
        return out

    # -- upscale (spandrel, chạy theo tile)
    def _upscaler(self, model_file):
        if model_file not in self._up:
            from spandrel import ModelLoader
            path = Path(self.cfg["upscale_models_dir"]).expanduser() / model_file
            if not path.exists():
                raise FileNotFoundError(f"Không thấy upscaler {path} — kiểm tra upscale_models_dir trong config.json")
            log(f"Load upscaler {path.name}")
            desc = ModelLoader().load_from_file(str(path))
            desc.to(self.dev).eval()
            if self.cuda and desc.supports_half:
                desc.half()
            self._up[model_file] = desc
        return self._up[model_file]

    def upscale(self, img, model_file):
        torch = self.torch
        desc = self._upscaler(model_file)
        s = desc.scale
        half = self.cuda and desc.supports_half
        x = torch.from_numpy(np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0).permute(2, 0, 1)[None]
        _, _, H, W = x.shape
        tile, ov = self.cfg.get("upscale_tile", 512), 32

        def starts(n):
            if n <= tile:
                return [0]
            return list(range(0, n - tile, tile - ov)) + [n - tile]

        out = torch.zeros(1, 3, H * s, W * s)
        wsum = torch.zeros(1, 1, H * s, W * s)
        for y in starts(H):
            for xx in starts(W):
                y1, x1 = min(y + tile, H), min(xx + tile, W)
                patch = x[:, :, y:y1, xx:x1].to(self.dev, torch.float16 if half else torch.float32)
                with torch.no_grad():
                    r = desc(patch).float().clamp(0, 1).cpu()
                out[:, :, y * s:y1 * s, xx * s:x1 * s] += r
                wsum[:, :, y * s:y1 * s, xx * s:x1 * s] += 1
        out = (out / wsum)[0].permute(1, 2, 0).numpy()
        return Image.fromarray((out * 255 + 0.5).astype(np.uint8))

    def release(self):
        self._seg = self._depth = None
        self._up = {}
        import gc
        gc.collect()
        if self.cuda:
            self.torch.cuda.empty_cache()


def crop_to_aspect(img, mask, aspect):
    """Cắt ảnh về đúng tỉ lệ, đặt tâm theo trọng tâm của chủ thể."""
    W, H = img.size
    if W / H > aspect:
        cw, ch = round(H * aspect), H
    else:
        cw, ch = W, round(W / aspect)
    wsum = mask.sum()
    if wsum > 0.002 * mask.size:
        ys, xs = np.mgrid[0:H, 0:W]
        cx, cy = (mask * xs).sum() / wsum, (mask * ys).sum() / wsum
        sub = np.argwhere(mask > 0.5)
        if len(sub):
            (y0, x0), (y1, x1) = sub.min(0), sub.max(0)
            if x1 - x0 > cw or y1 - y0 > ch:
                log("  lưu ý: chủ thể lớn hơn khung crop, một phần sẽ bị cắt")
    else:
        cx, cy = W / 2, H / 2
    left = int(min(max(cx - cw / 2, 0), W - cw))
    top = int(min(max(cy - ch / 2, 0), H - ch))
    return img.crop((left, top, left + cw, top + ch))


def stage_prep(cfg):
    sources = []
    if cfg["input_dir"].exists():
        for cat_dir in sorted(p for p in cfg["input_dir"].iterdir() if p.is_dir() and not p.name.startswith("_")):
            for f in sorted(cat_dir.iterdir()):
                if f.suffix.lower() in IMG_EXT:
                    sources.append((slugify(cat_dir.name), f))
    known = {read_meta(p)["hash"] for p in work_items(cfg)}
    todo = []
    for slug, f in sources:
        h = file_hash(f)
        if h in known:
            log(f"Bỏ qua (đã xử lý): {f.name}")
            continue
        known.add(h)
        todo.append((slug, f, h))
    if not todo:
        log("prep: không có ảnh mới")
        return
    log(f"prep: {len(todo)} ảnh")

    tw, th = cfg["target_w"], cfg["target_h"]
    prep = Prep(cfg)
    try:
        for i, (slug, src, h) in enumerate(todo, 1):
            log(f"[{i}/{len(todo)}] {slug}/{src.name}")
            try:
                img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
                # 1. crop theo chủ thể (mask thô trên ảnh gốc)
                img = crop_to_aspect(img, prep.subject_mask(img), tw / th)
                if img.width < tw * cfg["min_source_ratio"]:
                    log(f"  lưu ý: ảnh nguồn nhỏ ({img.width}x{img.height}), chất lượng có thể kém")
                # 2. upscale bằng model cho tới khi đủ lớn, rồi resize chính xác
                model_file = cfg["upscale_models"].get(slug, cfg["upscale_models"]["default"])
                passes = 0
                while img.width < tw and passes < 2:
                    img = prep.upscale(img, model_file)
                    passes += 1
                    log(f"  upscale x{passes} -> {img.width}x{img.height}")
                img = img.resize((tw, th), Image.LANCZOS)
                # 3. mask chính xác + depth trên ảnh cuối
                mask = prep.subject_mask(img)
                depth = prep.depth(img)

                item = cfg["work_dir"] / f"{slug}__{slugify(src.stem)[:40]}__{h}"
                item.mkdir(parents=True, exist_ok=True)
                img.save(item / "image.png")
                Image.fromarray((mask * 255).astype(np.uint8), "L").save(item / "mask.png")
                depth.save(item / "depth.png")
                lum = np.asarray(img.convert("L"), dtype=np.float32).mean() / 255.0
                write_meta(item, {
                    "status": PREPPED, "category": slug, "source": str(src.relative_to(cfg["input_dir"])),
                    "hash": h, "isDark": bool(lum < cfg["dark_threshold"]),
                })
            except Exception as e:  # noqa: BLE001
                log(f"  LỖI: {e!r} — bỏ qua ảnh này")
    finally:
        prep.release()


# ----------------------------------------------------------------------------- FILL (ComfyUI)

class Comfy:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def upload(self, img, name):
        import io
        buf = io.BytesIO()
        img.save(buf, "PNG")
        r = requests.post(f"{self.url}/upload/image", files={"image": (name, buf.getvalue(), "image/png")},
                          data={"overwrite": "true", "type": "input"}, timeout=120)
        r.raise_for_status()
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    def run(self, wf, out_node, timeout=1800):
        r = requests.post(f"{self.url}/prompt", json={"prompt": wf, "client_id": self.client_id}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"ComfyUI từ chối workflow: {r.text[:1500]}")
        pid = r.json()["prompt_id"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            h = requests.get(f"{self.url}/history/{pid}", timeout=30).json()
            if pid in h:
                st = h[pid].get("status", {})
                if st.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI lỗi: {json.dumps(st.get('messages', []))[:1500]}")
                im = h[pid]["outputs"][out_node]["images"][0]
                v = requests.get(f"{self.url}/view", params=im, timeout=120)
                v.raise_for_status()
                import io
                return Image.open(io.BytesIO(v.content)).convert("RGB")
            time.sleep(1.5)
        raise TimeoutError("ComfyUI quá thời gian")

    def free(self):
        try:
            requests.post(f"{self.url}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        except requests.RequestException:
            pass


def flux_fill_workflow(cfg, img_name, mask_name, prompt, seed, upscale):
    f = cfg["flux"]
    wf = {
        "1": ({"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": f["unet"]}}  # cần node ComfyUI-GGUF
              if f["unet"].endswith(".gguf") else
              {"class_type": "UNETLoader", "inputs": {"unet_name": f["unet"], "weight_dtype": f["weight_dtype"]}}),
        "2": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": f["clip_l"], "clip_name2": f["t5"], "type": "flux"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": f["vae"]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": img_name}},
        "5": {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "7": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["6", 0], "guidance": f["guidance"]}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "9": {"class_type": "InpaintModelConditioning",
              "inputs": {"positive": ["7", 0], "negative": ["8", 0], "vae": ["3", 0],
                         "pixels": ["4", 0], "mask": ["5", 0], "noise_mask": True}},
        "10": {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "seed": seed, "steps": f["steps"], "cfg": 1.0,
                          "sampler_name": f["sampler"], "scheduler": f["scheduler"],
                          "positive": ["9", 0], "negative": ["9", 1], "latent_image": ["9", 2], "denoise": 1.0}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
    }
    last = ["11", 0]
    if upscale and f.get("upscale_model"):
        wf["12"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": f["upscale_model"]}}
        wf["13"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["12", 0], "image": last}}
        last = ["13", 0]
    wf["20"] = {"class_type": "PreviewImage", "inputs": {"images": last}}
    return wf


def fill_item(cfg, comfy, item, seed=None, prompt=None):
    """Tạo background.png cho một work item bằng Flux Fill (chỉ inpaint vùng quanh chủ thể)."""
    import cv2
    meta = read_meta(item)
    img = Image.open(item / "image.png").convert("RGB")
    W, H = img.size
    soft = np.asarray(Image.open(item / "mask.png"), dtype=np.uint8)
    hard = ((soft > cfg["mask_threshold"] * 255) * 255).astype(np.uint8)
    d_px = max(1, round(W * cfg["mask_dilate"]))
    feather = max(1, round(W * cfg["mask_feather"]))
    grown = dilate(hard, d_px)

    if grown.max() == 0:
        log("  không có chủ thể -> background = ảnh gốc")
        img.save(item / "background.png")
    else:
        # vùng crop = bbox của mask + context xung quanh
        ys, xs = np.nonzero(grown)
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        pad = max(128, int(cfg["fill_context"] * max(x1 - x0, y1 - y0)))
        x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
        cw, ch = x1 - x0, y1 - y0
        s = min(1.0, math.sqrt(cfg["flux"]["megapixels"] * 1e6 / (cw * ch)))
        fw, fh = max(16, round(cw * s / 16) * 16), max(16, round(ch * s / 16) * 16)

        region = img.crop((x0, y0, x1, y1)).resize((fw, fh), Image.LANCZOS)
        rmask = Image.fromarray(grown[y0:y1, x0:x1]).resize((fw, fh), Image.NEAREST).convert("RGB")
        tag = item.name[-10:] + "_" + uuid.uuid4().hex[:6]
        img_name = comfy.upload(region, f"plx_{tag}.png")
        mask_name = comfy.upload(rmask, f"plx_{tag}_mask.png")

        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        if prompt is None:
            prompt = cfg["prompts"].get(meta["category"], cfg["prompts"]["default"])
        log(f"  Flux Fill {fw}x{fh} (crop {cw}x{ch}) seed={seed}")
        wf = flux_fill_workflow(cfg, img_name, mask_name, prompt, seed, upscale=s < 0.95)
        out = comfy.run(wf, "20").resize((cw, ch), Image.LANCZOS)

        # ghép lại: alpha mềm ở mép, phần lõi (chắc chắn có chủ thể) = 1
        alpha = cv2.GaussianBlur(grown.astype(np.float32) / 255.0, (0, 0), feather / 2)
        core = dilate(hard, max(0, d_px - 2 * feather)) > 0
        alpha[core] = 1.0
        a = alpha[y0:y1, x0:x1, None]
        base = np.asarray(img, dtype=np.float32)
        patch = base[y0:y1, x0:x1] * (1 - a) + np.asarray(out, dtype=np.float32) * a
        base[y0:y1, x0:x1] = patch
        Image.fromarray(np.clip(base + 0.5, 0, 255).astype(np.uint8)).save(item / "background.png")
        meta["fill"] = {"seed": seed, "prompt": prompt}

    if meta["status"] == PREPPED:
        meta["status"] = FILLED
    write_meta(item, meta)


def stage_fill(cfg):
    items = work_items(cfg, PREPPED)
    if not items:
        log("fill: không có item chờ")
        return
    comfy = Comfy(cfg["comfy_url"])
    log(f"fill: {len(items)} item")
    try:
        for i, item in enumerate(items, 1):
            log(f"[{i}/{len(items)}] {item.name}")
            try:
                fill_item(cfg, comfy, item)
            except Exception as e:  # noqa: BLE001
                log(f"  LỖI: {e!r} — bỏ qua")
    finally:
        comfy.free()


# ----------------------------------------------------------------------------- PUBLISH

def load_data(cfg):
    p = cfg["data_file"]
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"category_config": [], "wallpaper_config": {}}


def save_data(cfg, data):
    tmp = cfg["data_file"].with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(cfg["data_file"])


def get_category(cfg, data, slug):
    cats = data["category_config"]
    for c in cats:
        if slugify(c["nameId"]) == slug:
            return c["id"]
    cid = max((c["id"] for c in cats), default=0) + 1
    name = cfg.get("category_names", {}).get(slug, slug.replace("_", " ").title())
    cats.append({"id": cid, "nameId": name, "sort": max((c["sort"] for c in cats), default=-1) + 1})
    log(f"  tạo category mới: {cid} {name}")
    return cid


def stage_publish(cfg):
    items = work_items(cfg, FILLED)
    if not items:
        log("publish: không có item chờ")
        return
    data = load_data(cfg)
    base = cfg["base_url"].rstrip("/")
    published = []
    for item in items:
        meta = read_meta(item)
        slug = meta["category"]
        cid = get_category(cfg, data, slug)
        cat_dir = cfg["output_dir"] / slug
        cat_dir.mkdir(parents=True, exist_ok=True)
        n = max((int(p.name) for p in cat_dir.iterdir() if p.is_dir() and p.name.isdigit()), default=0) + 1
        if n >= 1000:
            raise RuntimeError(f"category {slug} vượt quá 999 wallpaper, cần đổi quy ước id")
        wid = 100000 + cid * 1000 + n
        dest = cat_dir / str(n)
        dest.mkdir()
        q = cfg["jpg_quality"]
        Image.open(item / "image.png").convert("RGB").save(dest / "image.jpg", quality=q, optimize=True)
        Image.open(item / "background.png").convert("RGB").save(dest / "image_background.jpg", quality=q, optimize=True)
        Image.open(item / "depth.png").convert("L").save(dest / "image_depth.jpg", quality=q, optimize=True)

        rel = f"{cfg['output_dir'].name}/{slug}/{n}"
        lst = data["wallpaper_config"].setdefault(str(cid), [])
        lst.append({
            "categoryId": cid, "id": wid, "isDark": meta["isDark"], "isPremium": False,
            "image": f"{base}/{rel}/image.jpg",
            "background": f"{base}/{rel}/image_background.jpg",
            "depth": f"{base}/{rel}/image_depth.jpg",
        })
        lst.sort(key=lambda e: e["id"])
        save_data(cfg, data)

        meta.update(status=PUBLISHED, published=f"{slug}/{n}", id=wid)
        write_meta(item, meta)
        src = cfg["input_dir"] / meta["source"]
        if src.exists():
            done = cfg["input_dir"] / "_done" / slug
            done.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(done / src.name))
        log(f"  {rel}  id={wid}  ({meta['source']})")
        published.append(item)
    review_sheet(cfg, published)


def review_sheet(cfg, items):
    """Ảnh ghép image | background | depth cho từng item để duyệt nhanh."""
    if not items:
        return
    cw, ch = 240, round(240 * cfg["target_h"] / cfg["target_w"])
    row_h = ch + 24
    sheet = Image.new("RGB", (cw * 3 + 40, row_h * len(items) + 10), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    for r, item in enumerate(items):
        meta = read_meta(item)
        y = 10 + r * row_h
        for c, name in enumerate(("image.png", "background.png", "depth.png")):
            im = Image.open(item / name).convert("RGB").resize((cw, ch), Image.BILINEAR)
            sheet.paste(im, (10 + c * (cw + 10), y))
        draw.text((10, y + ch + 4), f"{meta.get('published')}  id={meta.get('id')}  {meta['source']}", fill=(230, 230, 230))
    out = cfg["work_dir"] / "review" / time.strftime("review_%Y%m%d_%H%M%S.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    log(f"Ảnh review: {out}")


# ----------------------------------------------------------------------------- REFILL / STATUS

def cmd_refill(cfg, target, seed, prompt):
    match = [p for p in work_items(cfg) if read_meta(p).get("published") == target.strip("/")]
    if not match:
        sys.exit(f"Không tìm thấy work item cho {target}")
    item = match[0]
    comfy = Comfy(cfg["comfy_url"])
    try:
        fill_item(cfg, comfy, item, seed=seed, prompt=prompt)
    finally:
        comfy.free()
    dest = cfg["output_dir"] / target.strip("/")
    Image.open(item / "background.png").convert("RGB").save(
        dest / "image_background.jpg", quality=cfg["jpg_quality"], optimize=True)
    review_sheet(cfg, [item])
    log(f"Đã cập nhật {dest / 'image_background.jpg'}")


def cmd_status(cfg):
    counts = {}
    for p in work_items(cfg):
        st = read_meta(p)["status"]
        counts[st] = counts.get(st, 0) + 1
    pending = sum(1 for d in cfg["input_dir"].glob("*/*") if d.suffix.lower() in IMG_EXT
                  and not d.parent.name.startswith("_")) if cfg["input_dir"].exists() else 0
    print(f"Ảnh trong input: {pending}")
    for st in (PREPPED, FILLED, PUBLISHED):
        print(f"{st:10s}: {counts.get(st, 0)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "prep", "fill", "publish", "refill", "status"])
    ap.add_argument("target", nargs="?", help="cho refill: <category>/<n>, ví dụ anime/3")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--prompt")
    a = ap.parse_args()
    cfg = load_config()

    if a.cmd == "status":
        cmd_status(cfg)
    elif a.cmd == "refill":
        if not a.target:
            sys.exit("refill cần <category>/<n>")
        cmd_refill(cfg, a.target, a.seed, a.prompt)
    else:
        if a.cmd in ("run", "prep"):
            Comfy(cfg["comfy_url"]).free()  # giải phóng VRAM ComfyUI trước khi chạy model Python
            stage_prep(cfg)
        if a.cmd in ("run", "fill"):
            stage_fill(cfg)
        if a.cmd in ("run", "publish"):
            stage_publish(cfg)


if __name__ == "__main__":
    main()
