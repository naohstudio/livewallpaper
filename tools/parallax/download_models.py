"""Tải các model ComfyUI cần cho pipeline parallax (Flux Fill + upscale).

Chỉ dùng thư viện chuẩn, chạy được trước khi tạo .venv:

    python tools\\parallax\\download_models.py
    python tools\\parallax\\download_models.py --models-dir "D:/ComfyUI/models"
    python tools\\parallax\\download_models.py --only flux_fill t5     # tải một phần
    python tools\\parallax\\download_models.py --verify                # kiểm tra lại SHA256 file đã có

- Tải tiếp được nếu bị ngắt (file tạm *.part).
- Kiểm tra SHA256 với hash LFS trên HuggingFace sau khi tải xong.
- Nếu có biến môi trường HF_TOKEN thì gửi kèm (tải từ repo gốc bị gate).
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODELS_DIR = "H:/Comfy-Desktop/ComfyUI-Shared/models"
CHUNK = 8 * 1024 * 1024

# key: (thư mục con, tên file đích, [(repo, đường dẫn trong repo), ...] theo thứ tự ưu tiên)
MODELS = {
    "flux_fill": ("diffusion_models", "flux1-fill-dev-Q8_0.gguf",
                  [("YarvixPA/FLUX.1-Fill-dev-GGUF", "flux1-fill-dev-Q8_0.gguf")]),
    "clip_l": ("text_encoders", "clip_l.safetensors",
               [("comfyanonymous/flux_text_encoders", "clip_l.safetensors")]),
    "t5": ("text_encoders", "t5xxl_fp16.safetensors",
           [("comfyanonymous/flux_text_encoders", "t5xxl_fp16.safetensors")]),
    # Repo gốc của BFL bị gate (cần HF_TOKEN + đồng ý license). ffxvs/vae-flux là bản sao
    # giống hệt từng byte (cùng SHA256), dùng khi không có token.
    "vae": ("vae", "ae.safetensors",
            [("black-forest-labs/FLUX.1-Fill-dev", "ae.safetensors"),
             ("ffxvs/vae-flux", "ae.safetensors")]),
    "ultrasharp": ("upscale_models", "4x-UltraSharp.pth",
                   [("Kim2091/UltraSharp", "4x-UltraSharp.pth")]),
    "animesharp": ("upscale_models", "4x-AnimeSharp.pth",
                   [("Kim2091/AnimeSharp", "4x-AnimeSharp.pth")]),
}


def log(msg):
    print(msg, flush=True)


def request(url, token=None, headers=None):
    h = {"User-Agent": "parallax-model-downloader/1.0"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    return urllib.request.Request(url, headers=h)


def fmt_size(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def file_info(repo, path, token):
    """Lấy kích thước + SHA256 của file từ API HuggingFace. Trả về None nếu không truy cập được."""
    url = f"https://huggingface.co/api/models/{repo}?blobs=true"
    try:
        with urllib.request.urlopen(request(url, token), timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        log(f"  ! Không đọc được thông tin repo {repo} (HTTP {e.code})")
        return None
    for s in data.get("siblings", []):
        if s["rfilename"] == path:
            lfs = s.get("lfs") or {}
            return {"size": s.get("size") or lfs.get("size"), "sha256": lfs.get("sha256")}
    log(f"  ! Không thấy {path} trong {repo}")
    return None


def sha256_of(path):
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            done += len(chunk)
            print(f"\r  Đang kiểm tra SHA256... {done * 100 // max(total, 1)}%", end="", flush=True)
    print()
    return h.hexdigest()


def download(url, dest, size, token):
    """Tải về dest.part (tải tiếp nếu đã có một phần), xong thì đổi tên thành dest."""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink()
        have = 0
    if size and have == size:
        part.replace(dest)
        return

    headers = {"Range": f"bytes={have}-"} if have else {}
    if have:
        log(f"  Tải tiếp từ {fmt_size(have)}")
    try:
        resp = urllib.request.urlopen(request(url, token, headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # đã đủ
            part.replace(dest)
            return
        raise
    with resp:
        if have and resp.status != 206:  # server không hỗ trợ Range -> tải lại từ đầu
            have = 0
        mode = "ab" if have else "wb"
        done = have
        t0, d0 = time.time(), done
        with open(part, mode) as f:
            while chunk := resp.read(CHUNK):
                f.write(chunk)
                done += len(chunk)
                dt = time.time() - t0
                speed = (done - d0) / dt if dt > 0 else 0
                pct = f"{done * 100 // size}%" if size else ""
                print(f"\r  {fmt_size(done)} / {fmt_size(size or 0)} {pct}  {speed / 1e6:.1f} MB/s   ",
                      end="", flush=True)
        print()
    part.replace(dest)


def fetch(key, models_dir, token, verify):
    subdir, name, sources = MODELS[key]
    dest = models_dir / subdir / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"\n[{key}] {subdir}/{name}")

    for repo, path in sources:
        if repo.startswith("black-forest-labs/") and not token:
            continue  # repo bị gate, không có token thì bỏ qua sang bản sao
        info = file_info(repo, path, token)
        if not info:
            continue

        if dest.exists():
            if info["size"] and dest.stat().st_size != info["size"]:
                log(f"  File có sẵn sai kích thước, tải lại")
                dest.unlink()
            elif not verify:
                log(f"  Đã có ({fmt_size(dest.stat().st_size)}), bỏ qua")
                return True
            elif info["sha256"] and sha256_of(dest) == info["sha256"]:
                log("  Đã có, SHA256 khớp")
                return True
            else:
                log("  SHA256 không khớp, tải lại")
                dest.unlink()

        url = f"https://huggingface.co/{repo}/resolve/main/{path}"
        log(f"  Nguồn: {repo} ({fmt_size(info['size'] or 0)})")
        for attempt in range(1, 6):
            try:
                download(url, dest, info["size"], token)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                print()
                log(f"  ! Lỗi mạng ({e}), thử lại lần {attempt}/5 sau 10s...")
                time.sleep(10)
        else:
            log("  ! Bỏ cuộc với nguồn này")
            continue

        if info["sha256"]:
            if sha256_of(dest) != info["sha256"]:
                log("  ! SHA256 KHÔNG khớp, xoá file")
                dest.unlink()
                continue
            log("  SHA256 khớp ✓")
        return True

    log(f"  ✗ Không tải được {name}")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", default=DEFAULT_MODELS_DIR,
                    help=f"Thư mục models của ComfyUI (mặc định: {DEFAULT_MODELS_DIR})")
    ap.add_argument("--only", nargs="+", choices=list(MODELS), help="Chỉ tải các model này")
    ap.add_argument("--verify", action="store_true", help="Kiểm tra SHA256 cả các file đã có")
    args = ap.parse_args()

    models_dir = Path(os.path.expanduser(args.models_dir))
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    keys = args.only or list(MODELS)
    log(f"Thư mục models: {models_dir}")
    log(f"HF_TOKEN: {'có' if token else 'không có (dùng bản sao không gate cho ae.safetensors)'}")

    failed = [k for k in keys if not fetch(k, models_dir, token, args.verify)]
    log("")
    if failed:
        log(f"Lỗi: {', '.join(failed)}. Chạy lại script để tải tiếp.")
        sys.exit(1)
    log("Xong, đủ model. Nhớ khởi động lại ComfyUI để nhận model mới.")


if __name__ == "__main__":
    main()
