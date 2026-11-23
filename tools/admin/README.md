# Web quản lý wallpaper (local)

Chạy trên máy có GPU + ComfyUI (cùng máy với pipeline parallax). Dùng chung `.venv` của `tools/parallax` (flask đã có trong `requirements.txt`).

```bat
tools\admin\start.bat            :: mở http://127.0.0.1:5000
```

## Live Wallpaper (`wallpaper_data.json`)

- Thêm / sửa tên / sort / xoá category. Chỉ xoá được category rỗng và không có banner.
- **Kéo thả** category ở sidebar để đổi thứ tự (đánh lại `sort` = 0..n-1 cho tất cả, xếp lại `category_config`),
  kéo thả thẻ wallpaper để đổi thứ tự trong category (thứ tự mảng trong `wallpaper_config`). Áp dụng cho cả Parallax.
- Thêm wallpaper: upload `.mp4`.
  - Tên file `<prefix>_<NN>`: prefix lấy theo file đã có trong category (vd `year_`), category mới dùng slug của tên.
  - Thumb tự tạo: **webp động từ 3 giây đầu**, 320×569, lặp vô hạn, **< 400KB**. Thử lần lượt các mức (fps, quality)
    từ 15fps/q75 xuống tới 4fps/q5, giữ fps cao trước rồi mới giảm quality, lấy mức đầu tiên dưới 400KB.
  - `isDark` tự tính theo độ sáng frame đầu của thumb (có thể chọn tay).
  - Id = `categoryId * 1000 + n`.
- Bật/tắt `Premium`, `Dark` từng wallpaper. Dưới mỗi thumb hiện dung lượng (đỏ nếu ≥ 400KB).
- **Tạo lại thumb động (<400KB)**: nút xuất hiện khi category có thumb ≥ 400KB, chạy nền, ghi đè thumb cũ.
- **Xoá** wallpaper (có hỏi xác nhận): xoá mục JSON + file video và thumb.

## Parallax (`parallax_data.json`)

- Thêm / sửa category. Category đã có wallpaper chỉ được đổi hoa/thường, vì thư mục `3d/<slug>/` và URL gắn với slug của tên.
- **Upload & tạo**: ảnh được lưu vào `parallax_input/<slug>/`, rồi tự chạy `pipeline.py prep` + `fill` (mỗi lúc một tác vụ). Ảnh đã xử lý trước đó (trùng hash) sẽ bị bỏ qua và được báo lại.
- **Chờ duyệt**: xem image / background / depth, xem thử 3D, rồi:
  - **Publish**: chọn Premium/Dark, ghi vào `3d/<slug>/<n>/` và `parallax_data.json`.
  - **Fill lại**: chạy lại Flux Fill với seed/prompt khác.
  - **Huỷ**: xoá work item, chuyển ảnh gốc sang `parallax_input/_rejected/`.
- Bật/tắt `Premium`, `Dark` các wallpaper đã publish.
- **Xoá** wallpaper (có hỏi xác nhận): xoá mục JSON + thư mục `3d/<slug>/<n>/` + work item (để upload lại được ảnh đó).
- Xoá category: chỉ khi không còn wallpaper và không còn ảnh trong “Chờ duyệt”.

## Số thứ tự không bao giờ dùng lại

Tên file, id wallpaper, số thư mục `3d/<slug>/<n>` và id category **không được cấp lại sau khi xoá**,
vì URL/id cũ có thể còn trong cache CDN hoặc trong app (yêu thích, đã tải). Số đã cấp được lưu ở
`tools/admin/counters.json` (live) và `tools/parallax/counters.json` (parallax), nên commit 2 file này vào git.
Server tự ghi nhận các số đang có mỗi lần khởi động.

## Lưu ý

- Web **chỉ ghi file trong repo, không commit/push**. Chip trên header hiện số thay đổi chưa commit. App chỉ thấy thay đổi sau khi push lên `main` (+ ~5 phút cache CDN).
- Xoá có thể khôi phục bằng git **trước khi commit** (`git checkout -- <file>`).
- Server chỉ nghe ở `127.0.0.1`, không có đăng nhập. Đừng mở ra mạng ngoài.
