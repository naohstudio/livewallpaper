# Pipeline parallax hàng loạt (chạy trên PC RTX 3060)

```
parallax_input/<thể loại>/*.jpg|png|webp
   │  prep    : BiRefNet tìm chủ thể → crop 1536:2752 theo chủ thể → upscale (spandrel) → depth (Depth Anything V2) + mask
   │  fill    : Flux Fill qua ComfyUI xoá chủ thể → background
   │  publish : 3d/<thể loại>/<n>/ + id + parallax_data.json + ảnh review
   ▼
3d/<thể loại>/<n>/{image, image_background, image_depth}.jpg
```

Tên thư mục con trong `parallax_input/` chính là thể loại (`anime`, `car`, …). Thể loại mới sẽ tự được thêm vào `category_config`.

## Cài đặt (một lần)

### 1. ComfyUI Desktop: tải model vào thư mục `models/`

Với ComfyUI Desktop, thư mục model nằm trong thư mục cài đặt bạn chọn lúc cài, mặc định là `C:\Users\<tên>\Documents\ComfyUI\models\`. Có thể xem lại trong ComfyUI Desktop: menu → *Open Folder* → *Models*.

Cấu hình mặc định là chất lượng cao nhất chạy được trên 3060 12GB (dùng cá nhân):

| File | Thư mục | Nguồn |
|---|---|---|
| `flux1-fill-dev-Q8_0.gguf` | `diffusion_models/` (hoặc `unet/`) | Tìm "FLUX.1-Fill-dev gguf" trên HuggingFace |
| `clip_l.safetensors`, `t5xxl_fp16.safetensors` | `text_encoders/` (bản cũ: `clip/`) | HF `comfyanonymous/flux_text_encoders` |
| `ae.safetensors` | `vae/` | HF `black-forest-labs/FLUX.1-Fill-dev` (cần đồng ý license trên HF) |
| `4x-UltraSharp.pth`, `4x-AnimeSharp.pth` | `upscale_models/` | OpenModelDB |

Có thể tải tự động toàn bộ bảng trên bằng script (chỉ cần Python, tải tiếp được khi bị ngắt, có kiểm tra SHA256):

```bat
python tools\parallax\download_models.py --models-dir "<thư mục models của ComfyUI>"
```

Custom node cần cài: **ComfyUI-GGUF** (city96), để load file `.gguf`. Cài qua Manager có sẵn trong ComfyUI Desktop (nút **Manager** → *Custom Nodes Manager* → tìm "ComfyUI-GGUF" → Install → khởi động lại app). Ngoài node này, workflow chỉ dùng node có sẵn của ComfyUI. Nên chạy thử Flux Fill một lần trong giao diện ComfyUI để chắc model load được.

Vì sao chọn như vậy:
- **Q8_0** gần như không khác bản gốc bf16. Bản này hơi lớn hơn 12GB nên ComfyUI sẽ đẩy một phần sang RAM. Chạy chậm hơn fp8 một chút nhưng nét hơn.
- **T5 fp16** giúp Flux bám prompt tốt hơn bản fp8.
- **RAM hệ thống cần khoảng 32GB.** Nếu PC chỉ có 16GB, đổi `flux.t5` sang `t5xxl_fp8_e4m3fn.safetensors`. Nếu vẫn thiếu thì đổi `flux.unet` sang bản `Q6_K`.

Các model upscale khác nét hơn (DAT, HAT… trên OpenModelDB) đều dùng được, miễn là spandrel hỗ trợ. Chỉ cần thả file vào `upscale_models/` rồi đổi tên trong `config.json`.

### 2. Môi trường Python riêng (không dùng chung `.venv` của ComfyUI Desktop)

Cần Python 3.10–3.12 cài riêng trên máy (python.org).

```bat
cd <repo>
python -m venv .venv
.venv\Scripts\activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
pip install -r tools\parallax\requirements.txt
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

Phải cài **cả torch và torchvision** từ index CUDA. Nếu chỉ cài torch thì `requirements.txt` sẽ kéo torchvision từ PyPI và ghi đè torch bằng bản chỉ chạy CPU. Dòng kiểm tra cuối phải in ra `True`. Bản `cu130` cần driver NVIDIA hỗ trợ CUDA 13 (xem `nvidia-smi`). Driver cũ hơn thì dùng `cu126`.

Lần chạy đầu sẽ tự tải BiRefNet_HR và Depth-Anything-V2-Large từ HuggingFace.

### 3. Sửa `tools/parallax/config.json`

- `upscale_models_dir`: mặc định `~/Documents/ComfyUI/models/upscale_models` (`~` là thư mục user). Sửa lại nếu bạn cài ComfyUI Desktop ở chỗ khác.
- `flux.*`: tên file model đúng như trong ComfyUI.
- `comfy_url`: mặc định `http://127.0.0.1:8000` (cổng của ComfyUI Desktop). Kiểm tra trong *Settings → Server-Config → Port*.
- `upscale_models`: model upscale theo thể loại, ví dụ `"anime": "4x-AnimeSharp.pth"`. Thể loại không có trong danh sách dùng `default`.
- `prompts`: prompt mô tả nền cho Flux theo thể loại, ví dụ `"car": "empty road and city background, ..."`.
- `category_names`: tên hiển thị cho thể loại mới, ví dụ `{"new_year": "New Year"}`.

## Sử dụng

```bat
:: mở ComfyUI Desktop trước (để app chạy nền), rồi:
python tools\parallax\pipeline.py run          :: chạy cả 3 giai đoạn
python tools\parallax\pipeline.py status       :: xem số lượng ở mỗi giai đoạn
```

1. Mở ảnh review trong `parallax_work/review/` (mỗi dòng: ảnh gốc | background | depth).
2. Background nào lỗi (Flux vẽ lại nhân vật, có vệt mờ…) thì chạy lại với seed hoặc prompt khác:
   ```bat
   python tools\parallax\pipeline.py refill anime/3 --seed 42
   python tools\parallax\pipeline.py refill anime/3 --prompt "empty night sky with clouds and rocky cliffs"
   ```
3. Hài lòng thì `git add 3d parallax_data.json && git commit && git push`.

Chạy lại `run` an toàn: ảnh đã xử lý được chuyển vào `parallax_input/_done/` và nhận diện qua hash, nên không bị tạo trùng. Ảnh bị lỗi chỉ ghi log rồi bỏ qua, không làm dừng cả lô.

Có thể chạy từng giai đoạn: `prep` / `fill` / `publish`. Ví dụ prep cả lô buổi tối, rồi fill sau.

## Tinh chỉnh chất lượng (`config.json`)

| Tham số | Ý nghĩa |
|---|---|
| `mask_dilate` | Nới mask (tỉ lệ theo chiều rộng). Tăng lên nếu còn sót viền hoặc quầng sáng quanh nhân vật |
| `mask_threshold` | Ngưỡng mask BiRefNet. Giảm xuống để lấy cả khói, tóc mờ |
| `fill_context` | Lượng nền xung quanh gửi cho Flux tham chiếu |
| `flux.megapixels` | Độ phân giải Flux xử lý (1.5MP hợp với 12GB). Kết quả được upscale lại bằng `flux.upscale_model` |
| `flux.steps`, `flux.guidance` | 30 / 30 là giá trị khuyến nghị cho Flux Fill |
| `depth_input_size` | Độ phân giải đưa vào model depth (bội số của 14). Lớn hơn thì chi tiết hơn, nhưng quá lớn (>1400) có thể làm depth tổng thể méo |
| `depth_flip_tta` | Chạy depth thêm một lần trên ảnh lật ngang rồi lấy trung bình |
| `depth_blur` | Làm mềm depth để parallax không bị răng cưa |
