// Web quản lý wallpaper: Live (video) + Parallax (3D)
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const relUrl = (u) => u.split(".vercel.app/").pop();
const fileUrl = (rel, w) => "/file/" + rel.split("/").map(encodeURIComponent).join("/") + (w ? `?w=${w}` : "");

const S = {
  tab: ["live", "parallax"].includes(location.hash.slice(1)) ? location.hash.slice(1) : localGet("tab") || "live",
  live: null,
  parallax: null,
  sel: { live: null, parallax: null },
  pending: { items: [], waiting: [] },
  job: null,
  jobSeq: null,
};

function localGet(k) { try { return localStorage.getItem("wa_" + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("wa_" + k, v); } catch { /* bỏ qua */ } }

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers = { "Content-Type": "application/json" };
    delete o.json;
  }
  const r = await fetch(path, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `Lỗi ${r.status}`);
  return j;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast on" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast" + (err ? " err" : "")), err ? 6000 : 2800);
}

const sortedCats = (data) =>
  [...data.category_config].sort((a, b) => a.sort - b.sort || a.id - b.id);
const itemsOf = (data, cid) => data.wallpaper_config[String(cid)] || [];

// ----------------------------------------------------------------------------- tải dữ liệu

const THUMB_MAX = 400 * 1024;

async function loadData(which = S.tab) {
  S[which] = await api(which === "live" ? "/api/live" : "/api/parallax");
  if (which === "live") S.thumbSizes = await api("/api/live/thumbsizes");
  const cats = sortedCats(S[which]);
  const saved = Number(localGet("sel_" + which));
  if (!cats.some((c) => c.id === S.sel[which])) {
    S.sel[which] = cats.some((c) => c.id === saved) ? saved : cats[0]?.id ?? null;
  }
}

async function loadPending() {
  S.pending = await api("/api/parallax/pending");
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const c = $("#chipComfy");
    c.className = "chip " + (s.comfy ? "ok" : "bad");
    c.textContent = s.comfy ? "ComfyUI ● online" : "ComfyUI ● offline";
    c.title = s.comfyUrl;
    const g = $("#chipGit");
    if (s.gitChanges === null) { g.textContent = "Git ?"; g.className = "chip"; return; }
    const n = s.gitChanges.length;
    g.className = "chip " + (n ? "warn" : "ok");
    g.textContent = n ? `${n} thay đổi chưa commit/push` : "Git: đã đồng bộ";
    g.title = n ? "App chỉ thấy thay đổi sau khi commit + push lên main:\n" + s.gitChanges.join("\n") : "";
  } catch { /* server tắt */ }
}

// ----------------------------------------------------------------------------- render chung

function render() {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  renderCats();
  if (S.tab === "live") renderLive(); else renderParallax();
}

function renderCats() {
  const data = S[S.tab];
  if (!data) return;
  $("#cats").innerHTML = sortedCats(data).map((c) => {
    const n = itemsOf(data, c.id).length;
    return `<button class="cat ${c.id === S.sel[S.tab] ? "on" : ""}" data-cid="${c.id}" draggable="true" title="Kéo thả để đổi thứ tự">
      <span>${esc(c.nameId)}</span><span class="n">${n}</span></button>`;
  }).join("") || `<div class="hint" style="padding:8px">Chưa có category</div>`;
}

function catHeader(data, cat) {
  const items = itemsOf(data, cat.id);
  const prem = items.filter((w) => w.isPremium).length;
  return `<div class="cathead" id="catHead">
    <h2>${esc(cat.nameId)}</h2>
    <span class="meta">id ${cat.id} · sort ${cat.sort} · ${items.length} wallpaper · ${prem} premium</span>
    <button class="small" data-act="editcat">Sửa tên / sort</button>
    <button class="small danger" data-act="delcat" ${items.length ? `disabled title="Còn ${items.length} wallpaper, xoá hết trước"` : ""}>Xoá category</button>
    <form class="form" id="editCat" style="display:none">
      <input type="text" name="nameId" value="${esc(cat.nameId)}" required>
      <input type="number" name="sort" value="${cat.sort}" style="width:80px">
      <button class="primary small" type="submit">Lưu</button>
      <button class="small" type="button" data-act="canceledit">Huỷ</button>
    </form>
  </div>`;
}

function toggles(w) {
  return `<div class="toggles">
    <label><input type="checkbox" data-flag="isPremium" data-id="${w.id}" ${w.isPremium ? "checked" : ""}> Premium</label>
    <label><input type="checkbox" data-flag="isDark" data-id="${w.id}" ${w.isDark ? "checked" : ""}> Dark</label>
  </div>`;
}

// ----------------------------------------------------------------------------- LIVE

function renderLive() {
  const data = S.live;
  const cat = data && data.category_config.find((c) => c.id === S.sel.live);
  if (!cat) { $("#main").innerHTML = `<div class="empty">Chọn hoặc tạo một category</div>`; return; }
  const items = itemsOf(data, cat.id);
  const big = items.filter((w) => (S.thumbSizes?.[w.id] ?? 0) >= THUMB_MAX);
  $("#main").innerHTML = catHeader(data, cat) + `
    <div class="card">
      <h3>Thêm live wallpaper vào “${esc(cat.nameId)}”</h3>
      <form class="form" id="addLive">
        <label>Video (.mp4) <input type="file" name="video" accept="video/mp4,.mp4" required></label>
        <label>Dark <select name="isDark"><option value="auto">Tự động</option><option value="true">Có</option><option value="false">Không</option></select></label>
        <label><input type="checkbox" name="isPremium"> Premium</label>
        <button class="primary" type="submit">Upload</button>
      </form>
      <div class="hint" style="margin-top:8px">Thumb được tạo tự động: webp động từ 3 giây đầu video, 320×569, dưới 400KB (tự giảm fps/chất lượng nếu cần, mất vài giây). Tên file và id được tự đánh số tiếp theo trong category.</div>
      <div class="progress"><div></div></div>
    </div>
    ${big.length ? `<div class="card form" style="border-color:var(--warn)">
      <span>${big.length} thumb trong category này lớn hơn 400KB.</span>
      <button class="small" data-act="regen" data-ids="${big.map((w) => w.id).join(",")}" ${S.job?.running ? "disabled" : ""}>Tạo lại thumb động (&lt;400KB)</button>
    </div>` : ""}
    <div id="jobBox"></div>
    ${items.length > 1 ? `<div class="hint" style="margin-bottom:8px">Kéo thả thẻ để đổi thứ tự hiển thị trong app.</div>` : ""}
    <div class="grid">${items.map(liveCard).join("") || `<div class="empty">Chưa có wallpaper</div>`}</div>`;
  renderJob();
}

function liveCard(w) {
  const thumb = relUrl(w.thumb), video = relUrl(w.url);
  const size = S.thumbSizes?.[w.id];
  const kb = size == null ? "thiếu thumb" : `${Math.round(size / 1024)}KB`;
  return `<div class="item ${w.isPremium ? "premium" : ""}" draggable="true" data-id="${w.id}">
    <div class="thumbwrap" data-video="${esc(video)}" data-title="${w.id}">
      <img draggable="false" loading="lazy" src="${fileUrl(thumb)}?v=${size}" alt="">
      ${w.isPremium ? `<span class="badge">PREMIUM</span>` : ""}
    </div>
    <div class="body">
      <div class="id">${w.id} <span class="meta" style="font-weight:400;font-size:11px;${size == null || size >= THUMB_MAX ? "color:var(--bad)" : ""}">· thumb ${kb}</span></div>
      <div class="file" title="${esc(video)}">${esc(video.split("/").pop())}</div>
      ${toggles(w)}
      <button class="small danger" data-act="delitem" data-id="${w.id}">Xoá</button>
    </div>
  </div>`;
}

function uploadForm(url, fd, bar) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    bar.style.display = "block";
    x.upload.onprogress = (e) => { if (e.lengthComputable) bar.firstElementChild.style.width = (e.loaded / e.total * 100) + "%"; };
    x.onload = () => {
      bar.style.display = "none";
      bar.firstElementChild.style.width = "0";
      let j = {};
      try { j = JSON.parse(x.responseText); } catch { /* */ }
      x.status < 300 ? resolve(j) : reject(new Error(j.error || `Lỗi ${x.status}`));
    };
    x.onerror = () => { bar.style.display = "none"; reject(new Error("Mất kết nối server")); };
    x.send(fd);
  });
}

// ----------------------------------------------------------------------------- PARALLAX

function renderParallax() {
  const data = S.parallax;
  const cat = data && data.category_config.find((c) => c.id === S.sel.parallax);
  const items = cat ? itemsOf(data, cat.id) : [];
  $("#main").innerHTML = (cat ? catHeader(data, cat) + `
    <div class="card">
      <h3>Tạo parallax mới cho “${esc(cat.nameId)}”</h3>
      <form class="form" id="addPlx">
        <label>Ảnh <input type="file" name="images" accept="image/*" multiple required></label>
        <button class="primary" type="submit">Upload &amp; tạo</button>
      </form>
      <div class="hint" style="margin-top:8px">Ảnh sẽ được crop theo chủ thể, upscale, tạo depth (Python) rồi xoá chủ thể bằng Flux Fill (ComfyUI, ~3 phút/ảnh). Xong sẽ hiện ở “Chờ duyệt” để bạn xem và publish. Cần mở ComfyUI.</div>
      <div class="progress"><div></div></div>
    </div>` : `<div class="empty">Chọn hoặc tạo một category</div>`) +
    `<div id="jobBox"></div>
    <div id="pendingBox"></div>` +
    (cat ? `<div class="section-title"><h3>Đã publish</h3><span class="meta">${items.length} wallpaper${items.length > 1 ? " · kéo thả để đổi thứ tự" : ""}</span></div>
    <div class="grid">${items.map(plxCard).join("") || `<div class="empty">Chưa có wallpaper</div>`}</div>` : "");
  renderJob();
  renderPending();
}

function plxCard(w) {
  const img = relUrl(w.image);
  return `<div class="item ${w.isPremium ? "premium" : ""}" draggable="true" data-id="${w.id}">
    <div class="thumbwrap" data-plx="${w.id}">
      <img draggable="false" loading="lazy" src="${fileUrl(img, 320)}" alt="">
      ${w.isPremium ? `<span class="badge">PREMIUM</span>` : ""}
    </div>
    <div class="body">
      <div class="id">${w.id}</div>
      <div class="file">${esc(img.replace(/\/image\.jpg$/, ""))}</div>
      ${toggles(w)}
      <button class="small danger" data-act="delitem" data-id="${w.id}">Xoá</button>
    </div>
  </div>`;
}

function renderJob() {
  const box = $("#jobBox");
  if (!box) return;
  const j = S.job;
  if (!j || !j.seq) { box.innerHTML = ""; return; }
  const cls = j.running ? "" : j.ok ? "ok" : "fail";
  const t = Math.round(((j.ended || Date.now() / 1000) - j.started));
  const head = j.running
    ? `<span class="spinner"></span> ${esc(j.title)} · ${t}s`
    : `${j.ok ? "✓" : "✗"} ${esc(j.title)} · ${j.ok ? "xong" : j.errors ? `có ${j.errors} lỗi` : "lỗi"} sau ${t}s`;
  const open = box.querySelector("details")?.open ?? true;
  box.innerHTML = `<div class="card job ${cls}"><details ${open ? "open" : ""}><summary><strong>${head}</strong></summary>
    <pre id="jobLog">${esc(j.log.join("\n"))}</pre></details></div>`;
  const pre = $("#jobLog");
  if (pre) pre.scrollTop = pre.scrollHeight;
}

function renderPending() {
  const box = $("#pendingBox");
  if (!box) return;
  const { items, waiting } = S.pending;
  if (!items.length && !waiting.length) { box.innerHTML = ""; return; }
  const busy = S.job?.running;
  const ver = (i) => Math.round(i.mtime);
  box.innerHTML = `<div class="section-title"><h3>Chờ duyệt</h3><span class="meta">tất cả category</span></div>
    ${waiting.length ? `<div class="card form"><span>${waiting.length} ảnh đang chờ xử lý: ${waiting.map((w) => esc(w.path)).join(", ")}</span>
      <button class="small primary" data-act="run" ${busy ? "disabled" : ""}>Chạy pipeline</button></div>` : ""}
    <div class="pending">${items.map((i) => `
      <div class="pitem" data-name="${esc(i.name)}">
        <div class="triple">
          ${["image", "background", "depth"].map((n) => (n !== "background" || i.status === "filled")
            ? `<figure><img src="${fileUrl(`parallax_work/${i.name}/${n}.png`, 240)}&v=${ver(i)}" data-zoom="${n}"><figcaption>${n}</figcaption></figure>`
            : `<figure><div class="empty" style="width:96px;aspect-ratio:1536/2752;padding:4px;font-size:11px;display:grid;place-items:center">chưa fill</div><figcaption>background</figcaption></figure>`).join("")}
        </div>
        <div class="pinfo">
          <div><span class="st ${i.status}">${i.status === "filled" ? "Đã fill" : "Chờ fill"}</span>
            <strong style="margin-left:6px">${esc(i.categoryName)}</strong>
            <span class="meta"> · ${esc(i.source)}${i.fill ? ` · seed ${i.fill.seed}` : ""}</span></div>
          <div class="toggles">
            <label><input type="checkbox" name="isPremium"> Premium</label>
            <label><input type="checkbox" name="isDark" ${i.isDark ? "checked" : ""}> Dark</label>
          </div>
          <div class="actions">
            <button class="primary small" data-act="publish" ${i.status !== "filled" || busy ? "disabled" : ""}>Publish</button>
            <button class="small" data-act="showrefill" ${busy ? "disabled" : ""}>Fill lại…</button>
            ${i.status === "filled" ? `<button class="small" data-act="preview">Xem thử 3D</button>` : ""}
            <button class="small danger" data-act="discard" ${busy ? "disabled" : ""}>Huỷ</button>
          </div>
          <form class="refill">
            <input type="number" name="seed" placeholder="seed (trống = ngẫu nhiên)">
            <button class="small primary" type="submit">Chạy fill lại</button>
            <textarea name="prompt" placeholder="prompt (trống = prompt mặc định của category)">${esc(i.fill?.prompt || "")}</textarea>
          </form>
        </div>
      </div>`).join("")}
    </div>`;
}

// ----------------------------------------------------------------------------- job polling

let jobTimer = null;
async function pollJob() {
  clearTimeout(jobTimer);
  try {
    const j = await api("/api/job");
    const wasRunning = S.job?.running;
    S.job = j;
    renderJob();
    if (wasRunning && !j.running) {
      toast(j.ok ? `${j.title}: xong` : `${j.title}: có lỗi, xem log`, !j.ok);
      await loadPending();
      if (S.tab === "parallax") renderPending();
      else { await loadData("live"); render(); }
      refreshStatus();
    }
    if (j.running) jobTimer = setTimeout(pollJob, 1500);
  } catch { jobTimer = setTimeout(pollJob, 5000); }
}

// ----------------------------------------------------------------------------- dialog xem

function openDialog(html) {
  const d = $("#dlg");
  d.innerHTML = `<div class="dlg">${html}</div>`;
  d.showModal();
}

$("#dlg").addEventListener("click", (e) => {
  if (e.target.id === "dlg" || e.target.dataset.act === "close") $("#dlg").close();
});
$("#dlg").addEventListener("close", () => { $("#dlg").innerHTML = ""; stopGL(); });

function showVideo(rel, title) {
  openDialog(`<div class="top"><strong>${esc(title)}</strong><button class="small" data-act="close">Đóng</button></div>
    <video src="${fileUrl(rel)}" autoplay loop muted controls playsinline></video>`);
}

function showParallax(title, src) {
  openDialog(`<div class="top"><strong>${esc(title)}</strong><button class="small" data-act="close">Đóng</button></div>
    <div class="imgs">
      <figure><canvas id="gl"></canvas><figcaption>Xem thử 3D (di chuột lên ảnh, gần đúng)</figcaption></figure>
      <figure><img src="${src.image}"><figcaption>image</figcaption></figure>
      <figure><img src="${src.background}"><figcaption>background</figcaption></figure>
      <figure><img src="${src.depth}"><figcaption>depth</figcaption></figure>
    </div>`);
  startGL($("#gl"), src);
}

// Xem thử parallax bằng WebGL: lớp trước (image) dịch theo depth, chỗ hở lấy từ background.
let glStop = null;
function stopGL() { if (glStop) glStop(); glStop = null; }
function startGL(canvas, src) {
  const gl = canvas.getContext("webgl");
  if (!gl) return;
  canvas.width = 540; canvas.height = Math.round(540 * 2752 / 1536);
  canvas.style.aspectRatio = "1536/2752";
  const vs = `attribute vec2 p; varying vec2 uv; void main(){ uv = vec2(p.x*.5+.5, .5-p.y*.5); gl_Position = vec4(p,0,1); }`;
  const fs = `precision mediump float; varying vec2 uv; uniform sampler2D img, bg, dep; uniform vec2 m;
    void main(){
      // tìm điểm trên ảnh gốc rơi vào pixel này sau khi dịch (vài bước lặp đơn giản)
      vec2 q = uv; float d = 0.0;
      for (int i = 0; i < 6; i++) { d = texture2D(dep, q).r; q = uv - m * (d - 0.35) * 0.04; }
      // vùng xa lấy từ background (đã xoá chủ thể) để mép chủ thể không bị kéo giãn
      gl_FragColor = mix(texture2D(bg, q), texture2D(img, q), smoothstep(0.3, 0.5, d));
    }`;
  const sh = (t, s) => { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o); return o; };
  const pr = gl.createProgram();
  gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(pr); gl.useProgram(pr);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(pr, "p");
  gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const names = ["img", "bg", "dep"], urls = [src.image, src.background, src.depth];
  let loaded = 0, raf = 0, mx = 0, my = 0, tx = 0, ty = 0, alive = true;
  urls.forEach((u, i) => {
    const im = new Image();
    im.onload = () => {
      const t = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, im);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.uniform1i(gl.getUniformLocation(pr, names[i]), i);
      if (++loaded === 3) loop();
    };
    im.src = u;
  });
  const um = gl.getUniformLocation(pr, "m");
  const onMove = (e) => {
    const r = canvas.getBoundingClientRect();
    tx = ((e.clientX - r.left) / r.width - 0.5) * 2;
    ty = ((e.clientY - r.top) / r.height - 0.5) * 2;
  };
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerleave", () => { tx = ty = 0; });
  function loop() {
    if (!alive) return;
    mx += (tx - mx) * 0.12; my += (ty - my) * 0.12;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(um, mx, my);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    raf = requestAnimationFrame(loop);
  }
  glStop = () => { alive = false; cancelAnimationFrame(raf); };
}

// ----------------------------------------------------------------------------- sự kiện

document.querySelector(".tabs").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b || b.dataset.tab === S.tab) return;
  S.tab = b.dataset.tab;
  localSet("tab", S.tab);
  history.replaceState(null, "", "#" + S.tab);
  if (!S[S.tab]) await loadData();
  if (S.tab === "parallax") { await loadPending(); pollJob(); }
  render();
});

$("#cats").addEventListener("click", (e) => {
  const b = e.target.closest(".cat");
  if (!b) return;
  S.sel[S.tab] = Number(b.dataset.cid);
  localSet("sel_" + S.tab, S.sel[S.tab]);
  render();
});

$("#addCat").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const cat = await api(`/api/${S.tab}/category`, { method: "POST", json: { nameId: f.nameId.value, sort: f.sort.value } });
    f.nameId.value = "";
    await loadData();
    S.sel[S.tab] = cat.id;
    localSet("sel_" + S.tab, cat.id);
    render(); refreshStatus();
    toast(`Đã thêm category “${cat.nameId}” (id ${cat.id})`);
  } catch (err) { toast(err.message, true); }
});

$("#main").addEventListener("change", async (e) => {
  const cb = e.target.closest("input[data-flag]");
  if (!cb) return;
  const key = cb.dataset.flag, id = Number(cb.dataset.id);
  cb.disabled = true;
  try {
    await api(`/api/${S.tab}/wallpaper/${id}`, { method: "PATCH", json: { [key]: cb.checked } });
    await loadData();
    render(); refreshStatus();
    toast(`${id}: ${key === "isPremium" ? "Premium" : "Dark"} = ${cb.checked ? "bật" : "tắt"}`);
  } catch (err) { cb.checked = !cb.checked; cb.disabled = false; toast(err.message, true); }
});

$("#main").addEventListener("click", async (e) => {
  const t = e.target;
  const act = t.closest("[data-act]")?.dataset.act;

  if (act === "editcat") { $("#editCat").style.display = "flex"; t.style.display = "none"; return; }
  if (act === "canceledit") { render(); return; }

  if (act === "delcat") {
    const data = S[S.tab], cat = data.category_config.find((c) => c.id === S.sel[S.tab]);
    if (!confirm(`Xoá category “${cat.nameId}” (id ${cat.id}) khỏi ${S.tab === "live" ? "Live Wallpaper" : "Parallax"}?`)) return;
    try {
      await api(`/api/${S.tab}/category/${cat.id}`, { method: "DELETE" });
      S.sel[S.tab] = null;
      await loadData(); render(); refreshStatus();
      toast(`Đã xoá category “${cat.nameId}”`);
    } catch (err) { toast(err.message, true); }
    return;
  }

  if (act === "delitem") {
    const id = Number(t.dataset.id);
    const w = Object.values(S[S.tab].wallpaper_config).flat().find((x) => x.id === id);
    const what = S.tab === "live"
      ? `video/${relUrl(w.url).split("/").pop()} và thumb/${relUrl(w.thumb).split("/").pop()}`
      : `thư mục ${relUrl(w.image).replace(/\/image\.jpg$/, "")} (3 ảnh)`;
    if (!confirm(`Xoá wallpaper ${id}${w.isPremium ? " (PREMIUM)" : ""}?\n\nSẽ xoá mục trong JSON và xoá file ${what}.\nCó thể khôi phục bằng git trước khi commit.`)) return;
    t.disabled = true;
    try {
      await api(`/api/${S.tab}/wallpaper/${id}`, { method: "DELETE" });
      await loadData(); render(); refreshStatus();
      toast(`Đã xoá wallpaper ${id}`);
    } catch (err) { t.disabled = false; toast(err.message, true); }
    return;
  }

  if (act === "regen") {
    const ids = t.dataset.ids.split(",").map(Number);
    if (!confirm(`Tạo lại thumb động (3 giây đầu, < 400KB) cho ${ids.length} wallpaper? Thumb cũ sẽ bị ghi đè.`)) return;
    try {
      await api("/api/live/regen-thumbs", { method: "POST", json: { ids } });
      toast("Đang tạo lại thumb…"); pollJob();
    } catch (err) { toast(err.message, true); }
    return;
  }

  const vid = t.closest("[data-video]");
  if (vid) return showVideo(vid.dataset.video, vid.dataset.title);

  const plx = t.closest("[data-plx]");
  if (plx) {
    const id = Number(plx.dataset.plx);
    const w = Object.values(S.parallax.wallpaper_config).flat().find((x) => x.id === id);
    return showParallax(`${id} · ${relUrl(w.image).replace(/\/image\.jpg$/, "")}`,
      { image: fileUrl(relUrl(w.image), 1080), background: fileUrl(relUrl(w.background), 1080), depth: fileUrl(relUrl(w.depth), 1080) });
  }

  if (act === "run") {
    try { await api("/api/parallax/run", { method: "POST" }); toast("Đã bắt đầu chạy pipeline"); pollJob(); }
    catch (err) { toast(err.message, true); }
    return;
  }

  const p = t.closest(".pitem");
  if (!p) return;
  const name = p.dataset.name;
  const item = S.pending.items.find((i) => i.name === name);
  const work = (n) => fileUrl(`parallax_work/${name}/${n}.png`, 1080) + `&v=${Math.round(item.mtime)}`;

  if (t.dataset.zoom || act === "preview") {
    if (item.status !== "filled") return openDialog(`<div class="top"><strong>${esc(name)}</strong><button class="small" data-act="close">Đóng</button></div><div class="imgs"><img src="${work(t.dataset.zoom || "image")}"></div>`);
    return showParallax(name, { image: work("image"), background: work("background"), depth: work("depth") });
  }
  if (act === "showrefill") { p.querySelector(".refill").classList.toggle("on"); return; }
  if (act === "publish") {
    t.disabled = true;
    try {
      const entry = await api(`/api/parallax/item/${encodeURIComponent(name)}/publish`, {
        method: "POST", json: { isPremium: p.querySelector("[name=isPremium]").checked, isDark: p.querySelector("[name=isDark]").checked },
      });
      await Promise.all([loadData("parallax"), loadPending()]);
      S.sel.parallax = entry.categoryId;
      render(); refreshStatus();
      toast(`Đã publish ${relUrl(entry.image).replace(/\/image\.jpg$/, "")} (id ${entry.id})`);
    } catch (err) { t.disabled = false; toast(err.message, true); }
    return;
  }
  if (act === "discard") {
    if (!confirm("Huỷ ảnh này? Ảnh gốc sẽ được chuyển vào parallax_input/_rejected/.")) return;
    try {
      await api(`/api/parallax/item/${encodeURIComponent(name)}/discard`, { method: "POST" });
      await loadPending(); renderPending(); toast("Đã huỷ");
    } catch (err) { toast(err.message, true); }
  }
});

$("#main").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;

  if (f.id === "editCat") {
    try {
      await api(`/api/${S.tab}/category/${S.sel[S.tab]}`, { method: "PATCH", json: { nameId: f.nameId.value, sort: Number(f.sort.value) } });
      await loadData(); render(); refreshStatus(); toast("Đã lưu category");
    } catch (err) { toast(err.message, true); }
    return;
  }

  if (f.id === "addLive") {
    const fd = new FormData(f);
    fd.set("categoryId", S.sel.live);
    fd.set("isPremium", f.isPremium.checked ? "true" : "false");
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "Đang xử lý…";
    try {
      const r = await uploadForm("/api/live/wallpaper", fd, f.parentElement.querySelector(".progress"));
      await loadData("live"); render(); refreshStatus();
      const v = r.video, th = r.thumb;
      const warn = v.width !== 720 || v.height !== 1280 ? ` — lưu ý: video ${v.width}×${v.height}, các video khác là 720×1280` : "";
      toast(`Đã thêm ${r.file} (id ${r.entry.id}, ${v.duration}s) · thumb ${Math.round(th.size / 1024)}KB, ${th.fps}fps q${th.quality}${warn}`, !!warn);
    } catch (err) { btn.disabled = false; btn.textContent = "Upload"; toast(err.message, true); }
    return;
  }

  if (f.id === "addPlx") {
    const fd = new FormData(f);
    fd.set("categoryId", S.sel.parallax);
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const r = await uploadForm("/api/parallax/upload", fd, f.parentElement.querySelector(".progress"));
      f.reset(); btn.disabled = false;
      await loadPending(); renderPending();
      const dup = r.duplicates.length ? ` Bỏ qua ${r.duplicates.length} ảnh đã xử lý trước đó: ${r.duplicates.join(", ")}.` : "";
      if (!r.saved.length) toast("Không có ảnh mới." + dup, true);
      else toast((r.jobStarted ? `Đã upload ${r.saved.length} ảnh, đang xử lý…` : `Đã upload ${r.saved.length} ảnh — đang có tác vụ khác, bấm “Chạy pipeline” sau.`) + dup, !!dup);
      pollJob();
    } catch (err) { btn.disabled = false; toast(err.message, true); }
    return;
  }

  if (f.classList.contains("refill")) {
    const name = f.closest(".pitem").dataset.name;
    try {
      await api(`/api/parallax/item/${encodeURIComponent(name)}/refill`, { method: "POST", json: { seed: f.seed.value, prompt: f.prompt.value } });
      toast("Đang fill lại…"); pollJob();
    } catch (err) { toast(err.message, true); }
  }
});

// ----------------------------------------------------------------------------- kéo thả sắp xếp category

let dragCat = null;
$("#cats").addEventListener("dragstart", (e) => {
  const b = e.target.closest?.(".cat[data-cid]");
  if (!b) return;
  dragCat = b;
  b.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", b.dataset.cid);
});
$("#cats").addEventListener("dragover", (e) => {
  if (!dragCat) return;
  e.preventDefault();
  const over = e.target.closest(".cat[data-cid]");
  if (!over || over === dragCat) return;
  const r = over.getBoundingClientRect();
  const ref = e.clientY > r.top + r.height / 2 ? over.nextSibling : over;
  if (ref !== dragCat && ref !== dragCat.nextSibling) over.parentElement.insertBefore(dragCat, ref);
});
$("#cats").addEventListener("drop", (e) => { if (dragCat) e.preventDefault(); });
$("#cats").addEventListener("dragend", async () => {
  if (!dragCat) return;
  const name = dragCat.querySelector("span").textContent, moved = Number(dragCat.dataset.cid);
  dragCat.classList.remove("dragging");
  dragCat = null;
  const tab = S.tab;
  const ids = [...$("#cats").querySelectorAll(".cat[data-cid]")].map((b) => Number(b.dataset.cid));
  if (ids.join() === sortedCats(S[tab]).map((c) => c.id).join()) return;
  try {
    await api(`/api/${tab}/category-order`, { method: "PUT", json: { ids } });
    toast(`Đã lưu thứ tự category: “${name}” → vị trí ${ids.indexOf(moved) + 1}`);
  } catch (err) { toast(err.message, true); }
  await loadData(tab); render(); refreshStatus();
});

// ----------------------------------------------------------------------------- kéo thả sắp xếp

let dragEl = null;
$("#main").addEventListener("dragstart", (e) => {
  const it = e.target.closest?.(".grid > .item[data-id]");
  if (!it || e.target.closest("input, button, select")) return;
  dragEl = it;
  it.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", it.dataset.id);
});
$("#main").addEventListener("dragover", (e) => {
  if (!dragEl) return;
  e.preventDefault();
  const over = e.target.closest(".item[data-id]");
  if (!over || over === dragEl || over.parentElement !== dragEl.parentElement) return;
  const r = over.getBoundingClientRect();
  const after = e.clientY > r.bottom ? true : e.clientY < r.top ? false : e.clientX > r.left + r.width / 2;
  const ref = after ? over.nextSibling : over;
  if (ref !== dragEl && ref !== dragEl.nextSibling) over.parentElement.insertBefore(dragEl, ref);
});
$("#main").addEventListener("drop", (e) => { if (dragEl) e.preventDefault(); });
$("#main").addEventListener("dragend", async () => {
  if (!dragEl) return;
  const grid = dragEl.parentElement, moved = Number(dragEl.dataset.id);
  dragEl.classList.remove("dragging");
  dragEl = null;
  const tab = S.tab, cid = S.sel[tab];
  const ids = [...grid.querySelectorAll(":scope > .item[data-id]")].map((el) => Number(el.dataset.id));
  if (ids.join() === itemsOf(S[tab], cid).map((w) => w.id).join()) return;
  try {
    await api(`/api/${tab}/category/${cid}/order`, { method: "PUT", json: { ids } });
    toast(`Đã lưu thứ tự: ${moved} → vị trí ${ids.indexOf(moved) + 1}`);
  } catch (err) { toast(err.message, true); }
  await loadData(tab); render(); refreshStatus();
});

// ----------------------------------------------------------------------------- khởi động

(async () => {
  try {
    await loadData();
    if (S.tab === "parallax") await loadPending();
    render();
    await pollJob();
  } catch (err) { $("#main").innerHTML = `<div class="empty">Không kết nối được server: ${esc(err.message)}</div>`; }
  refreshStatus();
  setInterval(refreshStatus, 15000);
})();
