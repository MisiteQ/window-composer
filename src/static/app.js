// ===== Toast 通知系统 =====
function showToast(title, msg, level = "info", duration = 4000) {
    const stack = document.getElementById("toastStack");
    if (!stack) return;
    const toast = document.createElement("div");
    toast.className = `toast ${level}`;
    toast.innerHTML = `
        <div>
            <div class="toast-title">${escapeHtml(title)}</div>
            ${msg ? `<div class="toast-msg">${escapeHtml(msg)}</div>` : ""}
        </div>
    `;
    stack.appendChild(toast);
    setTimeout(() => {
        toast.classList.add("removing");
        setTimeout(() => toast.remove(), 200);
    }, duration);
}

function showApiResult(data, okTitle, errTitle) {
    if (data && data.ok) {
        showToast(okTitle, data.msg || "", "success");
    } else {
        showToast(errTitle, (data && data.msg) || "未知错误", "error");
    }
}

// ===== 配色主题 =====
// 三处必须一一对应，加主题时同时改：
//   index.css  :root[data-theme="..."] 变量块
//   main.py    UI_THEMES 白名单
//   这里        THEMES 清单（swBg / swAccent 只用来画色板圆点，不参与实际配色）
const THEMES = [
    { id: "midnight", name: "深空蓝", swBg: "#0f1419", swAccent: "#4a9eff" },
    { id: "graphite", name: "石墨灰", swBg: "#101214", swAccent: "#38bdf8" },
    { id: "pine", name: "松墨绿", swBg: "#0c1411", swAccent: "#34d399" },
    { id: "violet", name: "暮紫", swBg: "#110f18", swAccent: "#a78bfa" },
    { id: "amber", name: "暖琥珀", swBg: "#16110c", swAccent: "#f5a524" },
    { id: "daylight", name: "晨白", swBg: "#f4f6fa", swAccent: "#2563eb" },
];

function themeById(id) {
    return THEMES.find((t) => t.id === id) || THEMES[0];
}

function renderThemePicker() {
    const box = document.getElementById("themeSwatches");
    if (!box) return;
    const current = document.documentElement.dataset.theme || THEMES[0].id;
    box.innerHTML = THEMES.map((t) => `
        <button type="button"
                class="theme-swatch${t.id === current ? " active" : ""}"
                data-theme-id="${t.id}"
                style="--sw-bg:${t.swBg};--sw-accent:${t.swAccent}"
                title="配色：${escapeHtml(t.name)}"
                aria-label="配色：${escapeHtml(t.name)}"></button>
    `).join("");
    const nameEl = document.getElementById("themeName");
    if (nameEl) nameEl.textContent = themeById(current).name;
}

async function setTheme(id) {
    const theme = themeById(id);
    if (document.documentElement.dataset.theme === theme.id) return;
    // 先本地生效再发请求：换配色必须是瞬时的，不能等网络往返
    document.documentElement.dataset.theme = theme.id;
    try { localStorage.setItem("wc-theme", theme.id); } catch (e) { /* 隐私模式 */ }
    renderThemePicker();
    try {
        const resp = await fetch(
            `/api/settings/set_theme?theme=${encodeURIComponent(theme.id)}`,
            { method: "POST" }
        );
        const data = await resp.json();
        if (!data.ok) {
            showToast("配色未保存到服务端", data.msg || "未知错误", "error");
        }
    } catch (e) {
        showToast("配色未同步到服务端", "本次只在当前浏览器生效，刷新后可能还原", "warn");
    }
}

function initTheme() {
    const box = document.getElementById("themeSwatches");
    if (box) {
        box.addEventListener("click", (e) => {
            const btn = e.target.closest(".theme-swatch");
            if (btn) setTheme(btn.dataset.themeId);
        });
    }
    renderThemePicker();
}

// ===== 布局画布：多程序区域绘制 / 拖拽移动 / 缩放 / 边缘吸附 / 防重叠 =====
const canvas = document.getElementById("dragCanvas");
const CANVAS_BASE_WIDTH = 960;
// 画布坐标系 = 目标显示器（用户选定的那块屏）的像素坐标系。
// 后端 /api/windows 返回的 x/y 已换算成该屏的局部坐标（原点在屏左上角），
// 因此这里完全不关心显示器接的是 HDMI / DP / USB-C / VGA / DVI。
let OUTPUT_WIDTH = 1920;
let OUTPUT_HEIGHT = 1080;

// 区域配色（按 PID 分配，刷新后保持稳定）
const REGION_COLORS = ["#4a9eff", "#34d399", "#fbbf24", "#f472b6", "#a78bfa",
                       "#fb923c", "#22d3ee", "#f87171", "#a3e635", "#e879f9"];
const regionColorMap = {};
let regionColorIdx = 0;
function colorForRegion(pid) {
    if (!regionColorMap[pid]) {
        regionColorMap[pid] = REGION_COLORS[regionColorIdx++ % REGION_COLORS.length];
    }
    return regionColorMap[pid];
}

let scale = 1;
function updateScale() {
    scale = canvas.clientWidth > 0 ? canvas.clientWidth / OUTPUT_WIDTH : 1;
}
const toCanvas = (v) => v * scale;
const toPixels = (v) => v / scale;
const SNAP_PX = 8;                  // 画布像素吸附阈值
const MIN_W_PX = 120, MIN_H_PX = 80;

let currentRegions = [];            // 后端窗口：{pid, app_name, x, y, width, height}（HDMI 像素）
let selectedPid = null;
let webAppsCache = [];              // 可显示的 Web 应用
let layoutMode = null;              // 'draw' | 'move' | 'resize'
let dragState = null;

function applyCanvasAspect() {
    // 按真实输出分辨率设置画布纵横比（DRM 竖屏下为 3:4）
    canvas.style.aspectRatio = `${OUTPUT_WIDTH} / ${OUTPUT_HEIGHT}`;
    const badge = document.getElementById("canvasResolution");
    if (badge) {
        badge.innerText = `${OUTPUT_WIDTH} × ${OUTPUT_HEIGHT}`;
    }
    updateScale();
}

function setChip(id, text, extraClass, title) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerText = text;
    if (title !== undefined) el.title = title;
    el.classList.remove("backend-drm", "backend-headless");
    if (extraClass) el.classList.add(extraClass);
}

function updateStatusBadge(data) {
    const badge = document.getElementById("status-line");
    if (!badge) return;
    if (data && data.ok) {
        badge.classList.remove("error");
        const audio = data.default_audio_sink ? data.default_audio_sink : "未选择";
        const text = `运行中 · 背景 ${data.bg_color} · 音频 ${audio}`;
        badge.innerHTML = `<span class="dot"></span><span class="text">${escapeHtml(text)}</span>`;

        // 头部指标条（backend 由后端按 xrandr 实际输出探测，不再看环境变量）
        const isDrm = data.backend === "drm";
        const isVirtual = data.backend === "virtual";
        const outName = data.output_name ? String(data.output_name) : "";
        const outLabel = data.output_label ? String(data.output_label) : "";
        const outType = data.output_type ? String(data.output_type) : "";
        setChip("chip-backend",
                isDrm ? (outName ? `DRM · ${outName}` : "DRM 硬件输出")
                      : (isVirtual ? "虚拟输出" : "Headless 虚拟"),
                isDrm ? "backend-drm" : "backend-headless",
                isDrm
                    ? `${outLabel || outType || "已连接"} · ${data.display_width}×${data.display_height}`
                      + `（xrandr 实测，接口类型自动识别；显示器热插拔会自动点亮）`
                    : (isVirtual
                        ? "Xorg 运行在 dummy 虚拟输出上（容器未映射 /dev/dri）："
                          + "布局与截图可用，但真实显示器不会出画面"
                        : "未检测到已连接的显示输出：窗口只渲染在虚拟屏幕中，"
                          + "接上显示器后会自动出画面"));
        setChip("chip-resolution", `${data.display_width} × ${data.display_height}`,
                undefined, "目标输出当前分辨率（旋转后宽高互换）");
        setChip("chip-port", String(data.web_port));
        setChip("chip-audio", audio);
        updateRotationButtons(data.rotation || 0);
    } else {
        badge.classList.add("error");
        badge.innerHTML = `<span class="dot"></span><span class="text">状态获取失败</span>`;
    }
}

// ---------- 显示输出（HDMI / DP / USB-C / VGA / DVI 通用） ----------
// 热插拔在一次轮询里可能同时产生多个事件（接入 + 点亮 + 回屏），
// 旧实现只看最后一条，用户会漏掉前面的提示甚至连续弹 N 条 toast。
// 这里记录"已见过的事件集合"，每帧只把新事件汇总成一条 toast。
let _seenDisplayEventKeys = null;
const DISPLAY_EVENT_KEEP_KEYS = 60;

function displayEventKey(ev) {
    return `${ev.time}|${ev.type}|${ev.output || ""}|${ev.count || 0}|${ev.width || 0}x${ev.height || 0}`;
}

const OUTPUT_EVENT_TOAST = {
    connected: ["显示器接入", "info"],
    disconnected: ["显示器断开", "warn"],
    enabled: ["自动点亮输出", "success"],
    changed: ["分辨率变化", "info"],
    target_changed: ["目标屏自动改选", "warn"],
    reflow: ["窗口回屏", "info"],
};
const _TOAST_LEVEL_RANK = { info: 0, success: 0, warn: 1, error: 2 };

function outputStateLabel(o) {
    if (!o.connected) return "未接显示器";
    if (!o.enabled) return "已连接 · 未激活";
    return "使用中";
}

function renderOutputs(outputs, active) {
    const list = document.getElementById("output-list");
    const countEl = document.getElementById("output-count");
    const curEl = document.getElementById("output-current");
    const typeEl = document.getElementById("output-type");
    const items = Array.isArray(outputs) ? outputs : [];
    if (countEl) countEl.innerText = `${items.length} 个接口`;

    const current = items.find(o => o.name === active) || null;
    if (curEl) {
        curEl.innerText = current
            ? `${current.name}${current.width ? ` · ${current.width}×${current.height}` : ""}`
            : "未检测到显示器";
    }
    if (typeEl) typeEl.innerText = current ? (current.type_label || "—") : "—";

    if (!list) return;
    list.innerHTML = "";
    if (items.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.innerText = "未探测到任何显示输出：请确认容器已映射 /dev/dri，"
            + "或该显卡未被内核 DRM 驱动接管";
        list.appendChild(empty);
        return;
    }
    items.forEach(o => {
        const btn = document.createElement("button");
        const isActive = o.name === active;
        btn.className = "output-item" + (isActive ? " active" : "")
            + (o.connected ? "" : " disconnected");
        const size = (o.width && o.height) ? `${o.width} × ${o.height}` : "无信号";
        const tags = [o.type_label || "其他接口"];
        if (o.primary) tags.push("primary");
        if (o.rotation) tags.push(`旋转 ${o.rotation}°`);
        btn.innerHTML =
            `<div class="output-name">${escapeHtml(o.name)}` +
            (isActive ? `<span class="output-badge">布局基准</span>` : "") + `</div>` +
            `<div class="output-desc">${escapeHtml(size)} · ${escapeHtml(outputStateLabel(o))}</div>` +
            `<div class="output-tags">` +
            tags.map(t => `<span class="output-tag">${escapeHtml(t)}</span>`).join("") +
            `</div>`;
        btn.disabled = !o.connected;
        btn.title = o.connected
            ? `点击把 ${o.name} 设为布局基准显示器（窗口坐标以该屏左上角为原点）`
            : `${o.name}（${o.type_label}）当前没接显示器`;
        btn.onclick = () => setTargetOutput(o.name);
        list.appendChild(btn);
    });
}

function describeDisplayEvent(ev) {
    const label = ev.label
        ? `${ev.output}（${ev.label}）`
        : (ev.output || "");
    if (ev.type === "reflow") {
        return `${ev.count} 个越界窗口已回到可见区域`;
    }
    if (ev.type === "target_changed") {
        return `${ev.from} 不可用 → ${label}`;
    }
    if (ev.width) {
        return `${label} ${ev.width}×${ev.height}`;
    }
    return label;
}

function handleDisplayEvents(events) {
    if (!Array.isArray(events)) return;
    // 首帧只建立基线，不把服务端重启前的历史事件弹成 toast
    if (_seenDisplayEventKeys === null) {
        _seenDisplayEventKeys = new Set(events.map(displayEventKey));
        return;
    }
    const fresh = events.filter(ev => !_seenDisplayEventKeys.has(displayEventKey(ev)));
    events.forEach(ev => _seenDisplayEventKeys.add(displayEventKey(ev)));
    // 集合是增量增长的，周期性用当前窗口（后端最多保留 10 条）重建，防止泄漏
    if (_seenDisplayEventKeys.size > DISPLAY_EVENT_KEEP_KEYS) {
        _seenDisplayEventKeys = new Set(events.map(displayEventKey));
    }
    if (fresh.length === 0) return;

    // 一批事件汇总成一条 toast：标题按类型归并，正文最多列 4 条明细
    const titles = [];
    const details = [];
    let level = "info";
    fresh.forEach(ev => {
        const info = OUTPUT_EVENT_TOAST[ev.type];
        if (!info) return;
        if (!titles.includes(info[0])) titles.push(info[0]);
        if (_TOAST_LEVEL_RANK[info[1]] > _TOAST_LEVEL_RANK[level]) level = info[1];
        details.push(describeDisplayEvent(ev));
    });
    if (titles.length > 0) {
        const shown = details.slice(0, 4);
        const extra = details.length > 4 ? ` 等 ${details.length} 条` : "";
        showToast(`显示变更（${fresh.length}）：${titles.join("、")}`,
                  shown.join("；") + extra, level, 6000);
    }
    // 输出集合或几何变了：立刻同步列表与画布分辨率
    if (fresh.some(ev =>
        ["connected", "disconnected", "enabled", "changed"].includes(ev.type))) {
        refresh();
    }
}

async function setTargetOutput(name) {
    const resp = await fetch(
        `/api/display/set_output?name=${encodeURIComponent(name)}`,
        { method: "POST" }
    );
    const data = await resp.json();
    showApiResult(data, "目标显示器已切换", "切换失败");
    if (data.ok) {
        // 坐标系原点换到新屏了，画布尺寸与窗口坐标都要重新拉一遍
        await loadStatus();
        await refresh();
    }
}

async function refreshDisplays() {
    const btn = document.getElementById("outputs-refresh");
    if (btn) { btn.disabled = true; btn.textContent = "探测中…"; }
    try {
        const resp = await fetch("/api/display/refresh", { method: "POST" });
        const data = await resp.json();
        if (!data.ok) {
            showToast("刷新显示输出失败", data.msg || "未知错误", "error");
            return;
        }
        const extra = (data.activated && data.activated.length)
            ? `，已点亮 ${data.activated.join("、")}` : "";
        const reflow = data.reflowed ? `，回屏 ${data.reflowed} 个窗口` : "";
        showToast("显示输出已刷新",
                  `当前 ${data.active || "无输出"} ${data.width || 0}×${data.height || 0}` +
                  `${extra}${reflow}`,
                  "success");
        renderOutputs(data.outputs, data.active);
        await loadStatus();
        await refresh();
    } catch (e) {
        showToast("刷新显示输出失败", "请求异常，请查看运行日志", "error");
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "↻ 重新探测"; }
    }
}

// ---------- 屏幕方向旋转 ----------
function updateRotationButtons(deg) {
    document.querySelectorAll(".rot-btn").forEach(btn => {
        btn.classList.toggle("active", parseInt(btn.dataset.deg, 10) === parseInt(deg, 10));
    });
}

async function setDisplayRotation(deg) {
    if (![0, 90, 180, 270].includes(deg)) return;
    updateRotationButtons(deg);  // 先乐观高亮
    const resp = await fetch(`/api/display/rotate?degrees=${deg}`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
        const extra = data.reflowed ? `，${data.reflowed} 个越界窗口已拉回屏幕内` : "";
        showToast("屏幕方向已调整", `${deg}° · ${data.width} × ${data.height}${extra}`, "success");
        // 旋转后宽高可能互换，刷新状态与画布
        await refresh();
    } else {
        showToast("方向调整失败", data.msg || "", "error");
        // 回退高亮
        refresh();
    }
}

async function loadStatus() {
    let data = null;
    try {
        const resp = await fetch("/api/status");
        data = await resp.json();
        if (data.display_width) OUTPUT_WIDTH = data.display_width;
        if (data.display_height) OUTPUT_HEIGHT = data.display_height;
    } catch (e) {
        // 静默失败，沿用默认值
    }
    applyCanvasAspect();
    updateStatusBadge(data);
    if (data) {
        renderOutputs(data.outputs, data.output_name);
        handleDisplayEvents(data.display_events);
        renderShotInterval(data.screenshot_interval);
    }
    const reso = document.getElementById("status-resolution");
    if (reso && data) reso.innerText = `${data.display_width} × ${data.display_height}`;
}

// ---------- 持久区域渲染 ----------
function regionLabel(r) {
    return (r && r.app_name) ? String(r.app_name).replace(/^web:/, "") : `PID ${r && r.pid}`;
}

function renderRegions() {
    if (layoutMode) return;  // 交互过程中不重绘，避免抢事件
    const layer = document.getElementById("regionLayer");
    if (!layer) return;
    layer.innerHTML = "";
    currentRegions.forEach(r => {
        const color = colorForRegion(r.pid);
        const box = document.createElement("div");
        box.className = "region-box" + (r.pid === selectedPid ? " selected" : "");
        box.dataset.pid = r.pid;
        box.style.left = toCanvas(r.x) + "px";
        box.style.top = toCanvas(r.y) + "px";
        box.style.width = toCanvas(r.width) + "px";
        box.style.height = toCanvas(r.height) + "px";
        box.style.borderColor = color;
        box.style.background = color + "22";
        box.innerHTML =
            `<span class="region-label" style="background:${color}">${escapeHtml(regionLabel(r))} · ${r.width}×${r.height}</span>` +
            `<span class="region-resize-handle" title="拖拽调整大小"></span>`;
        layer.appendChild(box);
    });
    updateSelectedName();
}

function updateSelectedName() {
    const el = document.getElementById("selectedRegionName");
    if (!el) return;
    const r = currentRegions.find(x => x.pid === selectedPid);
    el.innerText = r
        ? `已选中：${regionLabel(r)}（${r.width}×${r.height}）`
        : "点击画布中的区域可选中，拖动区域可移动，拖右下角可缩放";
}

function findRegionBox(pid) {
    return document.querySelector(`#regionLayer .region-box[data-pid="${pid}"]`);
}

// ---------- 吸附参考线 ----------
function showGuides(guides) {
    const layer = document.getElementById("snapLayer");
    if (!layer) return;
    layer.innerHTML = "";
    guides.forEach(g => {
        const d = document.createElement("div");
        d.className = g.type === "v" ? "snap-guide-v" : "snap-guide-h";
        if (g.type === "v") d.style.left = g.pos + "px";
        else d.style.top = g.pos + "px";
        layer.appendChild(d);
    });
}
function clearGuides() { showGuides([]); }

// ---------- 吸附计算（屏幕边框 + 其他区域边缘，移动时含中心对齐） ----------
function buildSnapTargets(excludePid, includeCenter) {
    const cw = canvas.clientWidth, ch = canvas.clientHeight;
    const vx = [0, cw], hy = [0, ch];
    currentRegions.forEach(r => {
        if (r.pid === excludePid) return;
        const x = toCanvas(r.x), y = toCanvas(r.y), w = toCanvas(r.width), h = toCanvas(r.height);
        vx.push(x, x + w);
        hy.push(y, y + h);
        if (includeCenter) { vx.push(x + w / 2); hy.push(y + h / 2); }
    });
    if (includeCenter) { vx.push(cw / 2); hy.push(ch / 2); }
    return { vx, hy };
}

function nearestSnap(value, candidates) {
    let best = null, bestDist = SNAP_PX + 1;
    for (const c of candidates) {
        const d = Math.abs(value - c);
        if (d < bestDist) { bestDist = d; best = c; }
    }
    return best;
}

// g：画布像素矩形，就地修改；返回吸附参考线
function applySnap(g, mode, excludePid) {
    const targets = buildSnapTargets(excludePid, mode === "move");
    const guides = [];
    if (mode === "move") {
        // 左边 / 右边 / 中心三个候选，选距离最近的吸附点
        const snapL = nearestSnap(g.x, targets.vx);
        const snapR = nearestSnap(g.x + g.w, targets.vx);
        const snapC = nearestSnap(g.x + g.w / 2, targets.vx);
        const xCands = [
            [snapL, snapL === null ? 1e9 : Math.abs(g.x - snapL), "l"],
            [snapR, snapR === null ? 1e9 : Math.abs(g.x + g.w - snapR), "r"],
            [snapC, snapC === null ? 1e9 : Math.abs(g.x + g.w / 2 - snapC), "c"]
        ];
        xCands.sort((a, b) => a[1] - b[1]);
        if (xCands[0][0] !== null) {
            const [val, , kind] = xCands[0];
            if (kind === "l") g.x = val;
            else if (kind === "r") g.x = val - g.w;
            else g.x = val - g.w / 2;
            guides.push({ type: "v", pos: val });
        }
        const snapT = nearestSnap(g.y, targets.hy);
        const snapB = nearestSnap(g.y + g.h, targets.hy);
        const snapYC = nearestSnap(g.y + g.h / 2, targets.hy);
        const yCands = [
            [snapT, snapT === null ? 1e9 : Math.abs(g.y - snapT), "t"],
            [snapB, snapB === null ? 1e9 : Math.abs(g.y + g.h - snapB), "b"],
            [snapYC, snapYC === null ? 1e9 : Math.abs(g.y + g.h / 2 - snapYC), "c"]
        ];
        yCands.sort((a, b) => a[1] - b[1]);
        if (yCands[0][0] !== null) {
            const [val, , kind] = yCands[0];
            if (kind === "t") g.y = val;
            else if (kind === "b") g.y = val - g.h;
            else g.y = val - g.h / 2;
            guides.push({ type: "h", pos: val });
        }
    } else {
        // draw / resize：左、右边分别吸附，取距离更近的一边
        const l = nearestSnap(g.x, targets.vx);
        const r = nearestSnap(g.x + g.w, targets.vx);
        if (l !== null && (r === null || Math.abs(g.x - l) <= Math.abs(g.x + g.w - r))) {
            g.x = l;
            guides.push({ type: "v", pos: l });
        } else if (r !== null) {
            g.x = r - g.w;
            guides.push({ type: "v", pos: r });
        }
        const t = nearestSnap(g.y, targets.hy);
        const b = nearestSnap(g.y + g.h, targets.hy);
        if (t !== null && (b === null || Math.abs(g.y - t) <= Math.abs(g.y + g.h - b))) {
            g.y = t;
            guides.push({ type: "h", pos: t });
        } else if (b !== null) {
            g.y = b - g.h;
            guides.push({ type: "h", pos: b });
        }
    }
    return guides;
}

// ---------- AABB 重叠检测 ----------
function rectsOverlap(a, b) {
    return a.x < b.x + b.w - 1 && a.x + a.w > b.x + 1 &&
           a.y < b.y + b.h - 1 && a.y + a.h > b.y + 1;
}

function findOverlap(g, excludePid) {
    for (const r of currentRegions) {
        if (r.pid === excludePid) continue;
        const rg = { x: toCanvas(r.x), y: toCanvas(r.y), w: toCanvas(r.width), h: toCanvas(r.height) };
        if (rectsOverlap(g, rg)) return r;
    }
    return null;
}

// HDMI 像素防重叠：只移动/收缩 rect 自身（MTV 最小位移推出），不推动其他窗口
function resolveNoOverlap(rect, excludePid) {
    const others = currentRegions.filter(r => r.pid !== excludePid);
    rect.w = Math.min(rect.w, OUTPUT_WIDTH);
    rect.h = Math.min(rect.h, OUTPUT_HEIGHT);
    const overlapsH = (a, b) =>
        a.x < b.x + b.w - 1 && a.x + a.w > b.x + 1 &&
        a.y < b.y + b.h - 1 && a.y + a.h > b.y + 1;
    for (let pass = 0; pass < 8; pass++) {
        let moved = false;
        for (const o of others) {
            if (!overlapsH(rect, o)) continue;
            const dLeft = o.x + o.w - rect.x;
            const dRight = rect.x + rect.w - o.x;
            const dTop = o.y + o.h - rect.y;
            const dBottom = rect.y + rect.h - o.y;
            const m = Math.min(dLeft, dRight, dTop, dBottom);
            if (m === dLeft) rect.x -= dLeft;
            else if (m === dRight) rect.x += dRight;
            else if (m === dTop) rect.y -= dTop;
            else rect.y += dBottom;
            moved = true;
        }
        rect.x = Math.max(0, Math.min(rect.x, OUTPUT_WIDTH - rect.w));
        rect.y = Math.max(0, Math.min(rect.y, OUTPUT_HEIGHT - rect.h));
        if (!moved) break;
    }
    return others.some(o => overlapsH(rect, o));
}

// ---------- 待添加程序选择 ----------
function getPendingApp() {
    const sel = document.getElementById("pendingApp");
    const opt = sel && sel.selectedOptions[0];
    if (!opt || !opt.value) return null;
    return { appName: opt.dataset.app || "", url: opt.value };
}

function populatePendingApp() {
    const sel = document.getElementById("pendingApp");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = '<option value="">— 选择要添加的程序 —</option>';
    webAppsCache.forEach(app => {
        const o = document.createElement("option");
        o.value = app.url;
        o.dataset.app = app.label;
        o.innerText = `[${app.group}] ${app.label}${app.port ? ` :${app.port}` : ""}`;
        sel.appendChild(o);
    });
    if (prev) sel.value = prev;
}

function flashCanvas() {
    canvas.classList.remove("pending-flash");
    void canvas.offsetWidth;  // 重置动画
    canvas.classList.add("pending-flash");
    setTimeout(() => canvas.classList.remove("pending-flash"), 1400);
}

function selectRegion(pid) {
    selectedPid = pid;
    renderRegions();
    canvas.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------- 拖拽三段式状态机：start / update / finish ----------
const dragBox = document.getElementById("dragBox");

function getPointer(e) {
    const rect = canvas.getBoundingClientRect();
    return {
        x: Math.min(Math.max(e.clientX - rect.left, 0), rect.width),
        y: Math.min(Math.max(e.clientY - rect.top, 0), rect.height)
    };
}

// 使用 Pointer Events 而不是 mouse* 事件：一套 API 同时覆盖鼠标、触屏与触控笔，
// 配合 CSS #dragCanvas { touch-action: none; }，手指在画布上拖拽时浏览器
// 不会把手势抢走做页面滚动/缩放。pointermove/pointerup 仍绑在 document 上，
// 手指/鼠标移出画布后拖拽不中断。
function onPointerDown(e) {
    // 鼠标只响应主键；触屏/触控笔的 pointerdown button 本来就是 0
    if (e.pointerType === "mouse" && e.button !== 0) return;
    updateScale();
    // 防御：若上一次交互异常残留，先干净复位
    if (layoutMode) {
        layoutMode = null;
        dragState = null;
        clearGuides();
        dragBox.style.width = "0px";
        dragBox.style.height = "0px";
        renderRegions();
    }
    const p = getPointer(e);
    const handleEl = e.target.closest && e.target.closest(".region-resize-handle");
    const regionEl = !handleEl && e.target.closest ? e.target.closest(".region-box") : null;

    if (handleEl) {
        // 缩放已有区域（不重建 DOM，避免正在交互的元素被分离）
        const pid = parseInt(handleEl.closest(".region-box").dataset.pid, 10);
        const r = currentRegions.find(x => x.pid === pid);
        if (!r) return;
        selectedPid = pid;
        document.querySelectorAll(".region-box").forEach(el => el.classList.toggle("selected", parseInt(el.dataset.pid, 10) === pid));
        updateSelectedName();
        layoutMode = "resize";
        dragState = { pid, sx: p.x, sy: p.y, ox: r.x, oy: r.y, ow: r.width, oh: r.height };
    } else if (regionEl) {
        // 移动已有区域：只切换选中样式，不重建节点
        const pid = parseInt(regionEl.dataset.pid, 10);
        const r = currentRegions.find(x => x.pid === pid);
        if (!r) return;
        selectedPid = pid;
        document.querySelectorAll(".region-box").forEach(el => el.classList.toggle("selected", parseInt(el.dataset.pid, 10) === pid));
        updateSelectedName();
        layoutMode = "move";
        dragState = { pid, sx: p.x, sy: p.y, ox: r.x, oy: r.y, w: r.width, h: r.height };
    } else {
        // 画新区域：必须先选择程序
        const app = getPendingApp();
        if (!app) {
            showToast("请先选择程序", "在上方下拉框选择要显示的程序，或直接点击应用卡片", "warn");
            flashCanvas();
            return;
        }
        layoutMode = "draw";
        dragState = { sx: p.x, sy: p.y, app };
        dragBox.style.left = p.x + "px";
        dragBox.style.top = p.y + "px";
        dragBox.style.width = "0px";
        dragBox.style.height = "0px";
        dragBox.classList.remove("overlap");
        const label = document.getElementById("dragBoxLabel");
        if (label) label.innerText = "";
    }
    e.preventDefault();
}
canvas.addEventListener("pointerdown", onPointerDown);

// move/up 挂在 document 上：指针移出画布仍可持续拖拽并可靠抬起
function onPointerMove(e) {
    if (!layoutMode) return;
    const p = getPointer(e);

    if (layoutMode === "draw") {
        const g = {
            x: Math.min(p.x, dragState.sx),
            y: Math.min(p.y, dragState.sy),
            w: Math.abs(p.x - dragState.sx),
            h: Math.abs(p.y - dragState.sy)
        };
        const guides = applySnap(g, "draw", null);
        dragBox.style.left = g.x + "px";
        dragBox.style.top = g.y + "px";
        dragBox.style.width = g.w + "px";
        dragBox.style.height = g.h + "px";
        showGuides(guides);
        dragBox.classList.toggle("overlap", !!findOverlap(g, null));
        const label = document.getElementById("dragBoxLabel");
        if (label) {
            label.innerText = g.w > 30 && g.h > 16
                ? `${dragState.app.appName} ${Math.round(toPixels(g.w))}×${Math.round(toPixels(g.h))}`
                : "";
        }
    } else {
        // move / resize：HDMI 整数坐标 → 画布像素吸附/碰撞预览
        let nx, ny, nw, nh;
        if (layoutMode === "move") {
            nx = Math.max(0, Math.min(dragState.ox + Math.round(toPixels(p.x - dragState.sx)), OUTPUT_WIDTH - dragState.w));
            ny = Math.max(0, Math.min(dragState.oy + Math.round(toPixels(p.y - dragState.sy)), OUTPUT_HEIGHT - dragState.h));
            nw = dragState.w;
            nh = dragState.h;
        } else {
            nx = dragState.ox;
            ny = dragState.oy;
            nw = Math.max(MIN_W_PX, Math.min(dragState.ow + Math.round(toPixels(p.x - dragState.sx)), OUTPUT_WIDTH - nx));
            nh = Math.max(MIN_H_PX, Math.min(dragState.oh + Math.round(toPixels(p.y - dragState.sy)), OUTPUT_HEIGHT - ny));
        }
        const g = { x: toCanvas(nx), y: toCanvas(ny), w: toCanvas(nw), h: toCanvas(nh) };
        const guides = applySnap(g, layoutMode, dragState.pid);
        const rect = {
            x: Math.round(toPixels(g.x)), y: Math.round(toPixels(g.y)),
            w: Math.round(toPixels(g.w)), h: Math.round(toPixels(g.h))
        };
        showGuides(guides);
        const el = findRegionBox(dragState.pid);
        if (el) {
            el.style.left = g.x + "px";
            el.style.top = g.y + "px";
            el.style.width = g.w + "px";
            el.style.height = g.h + "px";
            el.classList.toggle("overlap", !!findOverlap(g, dragState.pid));
            const r0 = currentRegions.find(r => r.pid === dragState.pid);
            const lbl = el.querySelector(".region-label");
            if (lbl && r0) lbl.innerText = `${regionLabel(r0)} · ${rect.w}×${rect.h}`;
        }
        dragState._rect = rect;
    }
}
document.addEventListener("pointermove", onPointerMove);

async function onPointerUp() {
    if (!layoutMode) return;
    const mode = layoutMode;
    layoutMode = null;
    clearGuides();

    if (mode === "draw") {
        const left = parseInt(dragBox.style.left, 10);
        const top = parseInt(dragBox.style.top, 10);
        const wPx = parseInt(dragBox.style.width, 10);
        const hPx = parseInt(dragBox.style.height, 10);
        dragBox.classList.remove("overlap");
        dragBox.style.width = "0px";
        dragBox.style.height = "0px";
        if (toPixels(wPx) < MIN_W_PX || toPixels(hPx) < MIN_H_PX) {
            const label0 = document.getElementById("dragBoxLabel");
            if (label0) label0.innerText = "";
            showToast("区域太小", `显示区域至少 ${MIN_W_PX}×${MIN_H_PX} 像素`, "warn");
            return;
        }
        const rect = {
            x: Math.round(toPixels(left)), y: Math.round(toPixels(top)),
            w: Math.round(toPixels(wPx)), h: Math.round(toPixels(hPx))
        };
        const stillOverlap = resolveNoOverlap(rect, null);
        const { appName, url } = dragState.app;
        const resp = await fetch(
            `/api/app/launch_web?app_name=${encodeURIComponent(appName)}&url=${encodeURIComponent(url)}&x=${rect.x}&y=${rect.y}&w=${rect.w}&h=${rect.h}`,
            { method: "POST" }
        );
        const data = await resp.json();
        if (data.ok) {
            selectedPid = data.pid;
            if (stillOverlap) {
                showToast("空间不足", "已自动调整到最近的无重叠位置", "warn");
            } else {
                showToast("已加入布局", `${appName} → (${rect.x},${rect.y}) ${rect.w}×${rect.h}，可继续选择其他程序`, "success");
            }
        } else {
            showToast("显示失败", data.msg || appName, "error");
        }
    } else {
        const rect = dragState._rect;
        const el = findRegionBox(dragState.pid);
        if (el) el.classList.remove("overlap");
        if (rect) {
            const stillOverlap = resolveNoOverlap(rect, dragState.pid);
            const resp = await fetch(
                `/api/window/setrect?pid=${dragState.pid}&x=${rect.x}&y=${rect.y}&w=${rect.w}&h=${rect.h}`,
                { method: "POST" }
            );
            const data = await resp.json();
            if (!data.ok) {
                showToast("调整失败", data.msg || "", "error");
            } else if (stillOverlap) {
                showToast("空间不足", "已自动调整到最近的无重叠位置", "warn");
            }
        }
    }
    dragState = null;
    await refresh();
    setTimeout(refresh, 2500);  // chromium 窗口创建有延迟，稍后再同步一次
}
document.addEventListener("pointerup", onPointerUp);
// 指针被浏览器手势/系统打断（如触屏来电话中断）时也要复位，避免拖拽状态卡死
document.addEventListener("pointercancel", onPointerUp);

async function startApp(name, args) {
    const base = `/api/app/start?app_name=${encodeURIComponent(name)}`;
    const url = args ? `${base}&args=${encodeURIComponent(args)}` : base;
    const resp = await fetch(url, { method: "POST" });
    const data = await resp.json();
    if (!resp.ok || data.pid == null) {
        showToast("启动失败", data.msg || name, "error");
    } else {
        showToast("已启动", `${name} PID=${data.pid}`, "success");
        // 稍后刷新应用卡片上的运行中标记
        setTimeout(() => loadInstalledApps(false), 1500);
    }
    refresh();
}

let _appGridBound = false;

async function onAppCardClick(e) {
    // 点击应用卡片 = 选中该程序，随后在画布上拖出显示区域
    const card = e.target.closest(".app-card");
    if (!card || card.disabled) return;
    const url = card.dataset.url;
    const label = card.dataset.label || card.dataset.app;
    if (!url) {
        showToast("无访问地址", "该应用暂无 Web 访问入口", "warn");
        return;
    }
    const sel = document.getElementById("pendingApp");
    if (sel) {
        if (!Array.from(sel.options).some(o => o.value === url)) {
            await loadInstalledApps(false);
        }
        sel.value = url;
    }
    selectedPid = null;
    renderRegions();
    flashCanvas();
    canvas.scrollIntoView({ behavior: "smooth", block: "center" });
    showToast(`已选择「${label}」`, "在屏幕画布上按住鼠标拖拽，画出该程序的显示区域（自动吸附边缘、区域互不重叠）", "info", 3200);
}

async function launchWebApp(appName, url) {
    // 双击应用卡片：立即整屏显示（后端按真实分辨率取整屏几何 + wmctrl 真全屏）
    const resp = await fetch(
        `/api/app/launch_web?app_name=${encodeURIComponent(appName)}&url=${encodeURIComponent(url)}&fullscreen=1`,
        { method: "POST" }
    );
    const data = await resp.json();
    if (!resp.ok || data.pid == null) {
        showToast("显示失败", data.msg || appName, "error");
    } else {
        showToast("已全屏显示到显示器", `${appName} · PID ${data.pid}`, "success");
        setTimeout(() => loadInstalledApps(false), 1500);
    }
    refresh();
}

async function onAppCardDblClick(e) {
    // 双击卡片 = 直接全屏显示，跳过画布拖拽（最常用的"单个应用独占屏幕"场景）
    const card = e.target.closest(".app-card");
    if (!card || card.disabled) return;
    const url = card.dataset.url;
    const label = card.dataset.label || card.dataset.app;
    if (!url) {
        showToast("无访问地址", "该应用暂无 Web 访问入口", "warn");
        return;
    }
    await launchWebApp(label, url);
}

async function loadInstalledApps(force) {
    const grid = document.getElementById("app-grid");
    const countEl = document.getElementById("apps-count");
    const btn = document.getElementById("apps-refresh");
    if (!grid) return;

    if (force && btn) {
        btn.disabled = true;
        btn.textContent = "扫描中…";
    }
    let data = null;
    try {
        const resp = await fetch("/api/apps/installed" + (force ? "?refresh=1" : ""));
        data = await resp.json();
    } catch (err) {
        grid.innerHTML = '<span class="quick-empty">扫描失败，可使用下方手动启动</span>';
    } finally {
        if (force && btn) {
            btn.disabled = false;
            btn.textContent = "↻ 重新扫描";
        }
    }
    if (!data) return;

    const apps = data.apps || [];
    if (countEl) {
        let text = `共 ${data.count ?? apps.length} 个 · ${data.running_count ?? 0} 个运行中`;
        if (data.docker_available) {
            text += ` · Docker ${data.docker_count ?? 0} 个`;
        } else {
            // socket 没挂进来时不是错误，但用户会疑惑"我的容器去哪了"，给个明确指向
            text += " · Docker 未接入";
        }
        countEl.textContent = text;
    }

    // 缓存可显示的 Web 应用，供「添加程序」下拉框使用
    webAppsCache = apps.filter(a => a.has_url && a.url);
    populatePendingApp();

    if (apps.length === 0) {
        grid.innerHTML = '<span class="quick-empty">未检测到已安装应用。' +
            '飞牛应用请确认容器已挂载宿主 /var/apps；' +
            'Docker 容器请确认已挂载 /var/run/docker.sock。' +
            '也可点击"重新扫描"。</span>';
        return;
    }

    // 按分组渲染
    const groups = new Map();
    apps.forEach(app => {
        if (!groups.has(app.group)) groups.set(app.group, []);
        groups.get(app.group).push(app);
    });

    let html = "";
    // 飞牛应用一个都没有时（最典型：测试用 docker run 漏挂 /var/apps，
    // 或 appcenter 软链目标未挂导致扫不到系统应用），给出明确排障指向，
    // 避免用户像"只扫到 Docker"一样困惑
    const fnosCount = apps.filter(a => a.source !== "docker").length;
    if (fnosCount === 0) {
        let warn;
        if (data.host_apps_mounted === false) {
            warn = "未扫描到飞牛应用：容器缺少宿主 /var/apps 挂载（当前仅显示 Docker 容器）。"
                 + "飞牛应用中心安装的正式 compose 已包含该挂载；自行 docker run/compose "
                 + "时请补挂 /var/apps:/host_apps:ro 及 appcenter 软链目标后重启容器。";
        } else {
            warn = "已挂载 /var/apps 但未解析到飞牛应用：通常是应用目录的 target 绝对软链"
                 + "（指向 /volN/@appcenter、/usr/local/apps/@appcenter）在容器内断裂，"
                 + "请按安装说明补挂软链目标目录后重启容器。";
        }
        html += `<div class="apps-source-warn">${warn}</div>`;
    }
    for (const [group, list] of groups) {
        html += `<div class="app-group">`;
        html += `<div class="app-group-name">${escapeHtml(group)}</div>`;
        html += `<div class="app-cards">`;
        list.forEach(app => {
            const cls = app.running ? "app-card is-running" : "app-card";
            const status = app.running
                ? '<span class="run-state"><span class="run-dot"></span>运行中</span>'
                : '<span class="run-state run-state-idle">未运行</span>';
            const urlAttr = app.url ? `data-url="${escapeHtml(app.url)}"` : "";
            // 卡片副标题：Docker 应用显示镜像引用（比容器名更能说明它是什么），
            // 飞牛应用只在展示名与目录名不同时显示目录名
            let sub = "";
            if (app.source === "docker" && app.image) {
                sub = `<span class="app-card-cmd">${escapeHtml(app.image)}</span>`;
            } else if (app.label !== app.name) {
                sub = `<span class="app-card-cmd">${escapeHtml(app.name)}</span>`;
            }
            let title = app.url
                ? `单击选中 ${escapeHtml(app.label)}，在画布上拖出显示区域；双击直接全屏显示`
                : escapeHtml(app.label);
            if (app.source === "docker") {
                // 卡片上的镜像引用是单行截断的，完整信息放 tooltip 里
                if (app.image) title += `（镜像：${escapeHtml(app.image)}）`;
                if (app.status) title += `（容器状态：${escapeHtml(app.status)}）`;
            }
            html += `<button class="${cls}" data-app="${escapeHtml(app.name)}"
                data-label="${escapeHtml(app.label)}" ${urlAttr}
                title="${title}">
                <span class="app-card-label">${escapeHtml(app.label)}</span>
                ${sub}
                ${app.port ? `<span class="app-card-port">:${app.port}</span>` : ""}
                ${status}
            </button>`;
        });
        html += `</div></div>`;
    }
    grid.innerHTML = html;

    if (!_appGridBound) {
        grid.addEventListener("click", onAppCardClick);
        grid.addEventListener("dblclick", onAppCardDblClick);
        _appGridBound = true;
    }
}

async function startCustomApp() {
    const appName = document.getElementById("app-name-input").value.trim();
    if (!appName) {
        showToast("未输入程序名", "请输入要启动的程序名称", "warn");
        return;
    }
    const args = document.getElementById("app-args-input").value.trim();
    await startApp(appName, args);
}

async function stopApp(pid) {
    // 停止进程不可逆（未保存内容会丢），必须二次确认；
    // 关闭窗口（closeWin）只是关 X 窗口，相对温和，不再加确认
    if (!confirm(`确定停止进程 PID ${pid}？\n应用会被终止，未保存的内容将丢失。`)) return;
    const resp = await fetch(`/api/app/stop?pid=${pid}`, { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok === false) {
        showToast("停止失败", data.msg || "未知错误", "error");
    } else {
        showToast("已停止", `进程 ${pid} 已停止`, "info");
    }
    refresh();
}

async function closeWin(pid) {
    const resp = await fetch(`/api/window/close?pid=${pid}`, { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok === false) {
        showToast("关闭失败", data.msg || "未知错误", "error");
    } else {
        showToast("已关闭", `窗口 ${pid} 已关闭`, "info");
    }
    refresh();
}

async function minimizeWin(pid) {
    const resp = await fetch(`/api/window/minimize?pid=${pid}&minimized=true`,
        { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast("已最小化", `PID ${pid}，可在窗口列表点"恢复窗口"找回`, "info");
    } else {
        showToast("最小化失败", (data && data.msg) || "未知错误", "error");
    }
    refresh();
}

async function activateWin(pid) {
    const resp = await fetch(`/api/window/activate?pid=${pid}`, { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast("窗口已恢复", `PID ${pid}`, "success");
    } else {
        showToast("恢复失败", (data && data.msg) || "未知错误", "error");
    }
    refresh();
}

async function toggleAbove(pid, above) {
    const resp = await fetch(
        `/api/window/set_above?pid=${pid}&above=${above ? "true" : "false"}`,
        { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast(above ? "已置顶" : "已取消置顶", `PID ${pid}`, "success");
    } else {
        showToast("置顶操作失败", (data && data.msg) || "未知错误", "error");
    }
    refresh();
}

async function setFullscreen(pid, fullscreen) {
    const resp = await fetch(`/api/window/fullscreen?pid=${pid}&fullscreen=${fullscreen}`, { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
        showToast(fullscreen ? "已设为全屏" : "已退出全屏", `PID ${pid}`, "success");
    } else {
        showToast("操作失败", (data && data.msg) || "未知错误", "error");
    }
    refresh();
}

function setFullscreenFromSelect(fullscreen) {
    if (!selectedPid) {
        showToast("未选中区域", "请先在画布上点击要操作的程序区域（或在下方窗口列表中点击）", "warn");
        return;
    }
    setFullscreen(selectedPid, fullscreen);
}

async function stopAllApps() {
    if (!confirm("确定关闭所有应用窗口？显示服务不会停止，HDMI 背景画面保留")) return;
    const resp = await fetch("/api/app/stop_all", { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "已关闭所有应用", "关闭失败");
    refresh();
}

async function saveLayout() {
    const resp = await fetch("/api/layout/save", { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "布局已保存", "保存失败");
}

async function applyLayout() {
    // 把已保存布局应用回屏幕：已有窗口重定位，缺失的应用自动拉起（无需重启容器）
    showToast("正在应用布局", "已有窗口重新定位，缺失的应用正在拉起…", "info", 3000);
    let data = null;
    try {
        const resp = await fetch("/api/layout/restore", { method: "POST" });
        data = await resp.json();
    } catch (e) {
        showToast("应用布局失败", "请求异常，请查看运行日志", "error");
        return;
    }
    if (data && data.ok) {
        showToast("布局已应用", data.msg || "", "success");
    } else {
        showToast("应用布局失败", (data && data.msg) || "未知错误", "error");
    }
    await refresh();
    setTimeout(refresh, 2500);  // 应用拉起有延迟，稍后再同步一次
}

// ---------- 多套布局方案 ----------
async function loadProfiles(preselectName) {
    const sel = document.getElementById("profileSelect");
    if (!sel) return;
    let items = [];
    try {
        const resp = await fetch("/api/layout/profiles");
        const data = await resp.json();
        if (data.ok) items = data.profiles || [];
    } catch (e) {
        // 下拉框保留旧内容，不打扰用户
        return;
    }
    const want = preselectName || sel.value;
    sel.innerHTML = '<option value="">— 选择布局方案 —</option>' +
        items.map(p =>
            `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}（${p.windows} 窗）</option>`
        ).join("");
    if (want && items.some(p => p.name === want)) sel.value = want;
}

async function saveProfileAs() {
    const name = prompt("给这套布局起个名字（如：影视模式 / 工作台）", "");
    if (name === null) return;
    const trimmed = name.trim();
    if (!trimmed) {
        showToast("未命名", "方案名不能为空", "warn");
        return;
    }
    const resp = await fetch(
        `/api/layout/save_profile?name=${encodeURIComponent(trimmed)}`,
        { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast(data.overwritten ? "方案已覆盖" : "方案已保存", data.msg || "", "success");
        await loadProfiles(trimmed);
    } else {
        showToast("保存方案失败", (data && data.msg) || "未知错误", "error");
    }
}

async function applySelectedProfile() {
    const sel = document.getElementById("profileSelect");
    const name = sel && sel.value;
    if (!name) {
        showToast("未选择方案", "请先在下拉框选择一套布局方案", "warn");
        return;
    }
    showToast("正在应用方案", `「${name}」：已有窗口重定位，缺失应用正在拉起…`, "info", 3000);
    let data = null;
    try {
        const resp = await fetch(
            `/api/layout/apply_profile?name=${encodeURIComponent(name)}`,
            { method: "POST" });
        data = await resp.json();
    } catch (e) {
        showToast("应用方案失败", "请求异常，请查看运行日志", "error");
        return;
    }
    if (data.ok) {
        showToast("方案已应用", data.msg || "", "success");
    } else {
        showToast("应用方案失败", data.msg || "未知错误", "error");
    }
    await refresh();
    setTimeout(refresh, 2500);
}

async function deleteSelectedProfile() {
    const sel = document.getElementById("profileSelect");
    const name = sel && sel.value;
    if (!name) {
        showToast("未选择方案", "请先在下拉框选择要删除的方案", "warn");
        return;
    }
    if (!confirm(`确定删除布局方案「${name}」？\n（只删除方案，当前布局与正在运行的窗口都不受影响）`)) return;
    const resp = await fetch(
        `/api/layout/delete_profile?name=${encodeURIComponent(name)}`,
        { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast("方案已删除", data.msg || "", "success");
        await loadProfiles("");
    } else {
        showToast("删除方案失败", (data && data.msg) || "未知错误", "error");
    }
}

// ---------- 定时截图 ----------
function renderShotInterval(seconds) {
    const sec = parseInt(seconds, 10);
    const cur = document.getElementById("shot-interval-current");
    const input = document.getElementById("shot-interval");
    if (cur) {
        cur.innerText = (!isNaN(sec) && sec >= 10)
            ? `每 ${sec} 秒（约 ${Math.round(sec / 60 * 10) / 10} 分钟）`
            : "已关闭";
    }
    if (input && document.activeElement !== input) input.value = isNaN(sec) ? 0 : sec;
}

async function setScreenshotInterval() {
    const input = document.getElementById("shot-interval");
    const seconds = parseInt(input && input.value, 10);
    if (isNaN(seconds) || (seconds !== 0 && (seconds < 10 || seconds > 3600))) {
        showToast("间隔不合法", "0 表示关闭；开启时必须在 10~3600 秒之间", "warn");
        return;
    }
    const resp = await fetch(
        `/api/settings/set_screenshot_interval?seconds=${seconds}`,
        { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast("设置已保存", data.msg || "", "success");
        renderShotInterval(seconds);
    } else {
        showToast("保存失败", (data && data.msg) || "未知错误", "error");
    }
}

async function setWebPort() {
    const newPort = parseInt(document.getElementById("new-port").value);
    if (isNaN(newPort) || newPort < 1024 || newPort > 65535) {
        showToast("端口格式错误", "端口必须为 1024~65535", "warn");
        return;
    }
    const resp = await fetch(`/api/settings/set_web_port?new_port=${newPort}`, { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "端口已保存", "保存失败");
}

async function setBgColor() {
    const color = document.getElementById("bg-color-input").value.trim();
    if (!/^#[0-9A-Fa-f]{6}$/.test(color)) {
        showToast("格式错误", "背景色应为 #RRGGBB", "warn");
        return;
    }
    const resp = await fetch(`/api/settings/set_bg_color?bg_color=${encodeURIComponent(color)}`, { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "背景色已保存", "保存失败");
    if (data.ok) {
        const cur = document.getElementById("cur-bg-color");
        if (cur) cur.innerText = color;
        const swatch = document.getElementById("cur-bg-swatch");
        if (swatch) swatch.style.background = color;
    }
}

async function setAutoStart(enable) {
    const resp = await fetch(`/api/settings/set_auto_start?enable=${enable}`, { method: "POST" });
    const data = await resp.json();
    showApiResult(data, enable ? "已开启自启" : "已关闭自启", "操作失败");
    if (data.ok) setTimeout(() => location.reload(), 800);
}

// ---------------- 访问令牌 ----------------
async function revealAccessToken() {
    const box = document.getElementById("access-token-box");
    const btn = event.target;
    const resp = await fetch("/api/access/token");
    const data = await resp.json();
    if (!data.ok) { showApiResult(data, "", "获取令牌失败"); return; }
    if (box.type === "password") {
        box.value = data.token;
        box.type = "text";
        btn.textContent = "隐藏";
    } else {
        box.value = "••••••••••••";
        box.type = "password";
        btn.textContent = "显示";
    }
}

async function copyAccessToken() {
    const resp = await fetch("/api/access/token");
    const data = await resp.json();
    if (!data.ok) { showApiResult(data, "", "获取令牌失败"); return; }
    try {
        await navigator.clipboard.writeText(data.token);
        showToast("已复制", "访问令牌已复制到剪贴板", "info");
    } catch (e) {
        showToast("复制失败", "请点【显示】后手动选择复制", "warn");
    }
}

async function rotateAccessToken() {
    if (!confirm("确定随机重置访问令牌吗？旧令牌与已登录设备将立即失效。")) return;
    const resp = await fetch("/api/access/rotate", { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "令牌已随机重置", "重置失败");
    if (data.ok) {
        const box = document.getElementById("access-token-box");
        box.value = data.token;
        box.type = "text";
        document.querySelector(".default-token-warn")?.remove();
    }
}

async function setAccessToken() {
    const inp1 = document.getElementById("new-access-token");
    const inp2 = document.getElementById("new-access-token2");
    const t1 = (inp1.value || "").trim();
    const t2 = (inp2.value || "").trim();
    if (!t1) {
        showToast("令牌为空", "请输入 6~64 位的新令牌", "warn");
        return;
    }
    if (t1.length < 6 || t1.length > 64) {
        showToast("长度不符", "令牌长度需为 6~64 个字符", "warn");
        return;
    }
    if (t1 !== t2) {
        showToast("两次输入不一致", "请在两个输入框中填写相同的新令牌", "warn");
        return;
    }
    if (!confirm("确定把访问令牌改为自定义令牌吗？修改后其他设备需用新令牌重新登录。")) return;
    const resp = await fetch(`/api/access/set_token?token=${encodeURIComponent(t1)}`,
                             { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
        inp1.value = "";
        inp2.value = "";
        showApiResult(data, "令牌已更新，页面即将刷新", "修改失败");
        setTimeout(() => location.reload(), 1200);
    } else {
        showApiResult(data, "", "修改失败");
    }
}

async function setTokenRequired(enable) {
    if (!enable && !confirm("关闭后，同一局域网内任何人都能直接操作本面板。确定关闭？")) return;
    const resp = await fetch(`/api/access/set_required?enable=${enable}`, { method: "POST" });
    const data = await resp.json();
    showApiResult(data, enable ? "访问保护已开启" : "访问保护已关闭", "操作失败");
    if (data.ok) setTimeout(() => location.reload(), 800);
}

async function setDisplayResolution() {
    const w = parseInt(document.getElementById("disp-w").value);
    const h = parseInt(document.getElementById("disp-h").value);
    if (isNaN(w) || isNaN(h) || w <= 0 || h <= 0) {
        showToast("分辨率错误", "宽高必须为正整数", "warn");
        return;
    }
    const resp = await fetch(`/api/settings/set_display_resolution?width=${w}&height=${h}`, { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "分辨率已保存", "保存失败");
    if (data.ok) {
        await loadStatus();
    }
}

async function setAudioSink(sinkId) {
    const resp = await fetch(`/api/audio/set_sink?sink_id=${encodeURIComponent(sinkId)}`, { method: "POST" });
    const data = await resp.json();
    showApiResult(data, "音频设备已切换", "切换失败");
    refresh();
}

// 拖动滑块时 input 事件每秒触发几十次：本地立即更新数字，网络请求去抖，
// 停顿 120ms 后才真正下发 pactl；最后一次保证不丢
const _volTimers = {};
function onSinkVolumeInput(inputEl, sinkId) {
    const card = inputEl.closest(".sink-card");
    const valEl = card && card.querySelector(".sink-vol-val");
    if (valEl) valEl.textContent = `${inputEl.value}%`;
    const key = sinkId;
    if (_volTimers[key]) clearTimeout(_volTimers[key]);
    _volTimers[key] = setTimeout(async () => {
        const resp = await fetch(
            `/api/audio/set_volume?sink_id=${encodeURIComponent(sinkId)}&volume=${inputEl.value}`,
            { method: "POST" });
        const data = await resp.json().catch(() => null);
        if (data && !data.ok) {
            showToast("音量设置失败", data.msg || "未知错误", "error");
        }
    }, 120);
}

async function toggleSinkMute(sinkId, mute) {
    const resp = await fetch(
        `/api/audio/set_mute?sink_id=${encodeURIComponent(sinkId)}&muted=${mute ? "true" : "false"}`,
        { method: "POST" });
    const data = await resp.json().catch(() => null);
    if (data && data.ok) {
        showToast(mute ? "已静音" : "已取消静音", sinkId, "info", 1800);
    } else {
        showToast("静音操作失败", (data && data.msg) || "未知错误", "error");
    }
    refresh();
}

async function takeScreenshot() {
    showToast("正在截图", "请稍候...", "info", 1500);
    const resp = await fetch("/api/screenshot", { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
        const ts = Date.now();
        const img = document.getElementById("shotImg");
        const link = document.getElementById("shotOpenNew");
        if (img) img.src = `/screenshot.png?t=${ts}`;
        if (link) link.href = `/screenshot.png?t=${ts}`;
        document.getElementById("shotModal").style.display = "flex";
    } else {
        showToast("截图失败", data.msg || "未知错误", "error");
    }
}

async function openLogModal() {
    const resp = await fetch("/api/logs");
    const data = await resp.json();
    document.getElementById("logContent").innerText = data.logs || "(无日志)";
    document.getElementById("logModal").style.display = "flex";
}

function closeLogModal() {
    document.getElementById("logModal").style.display = "none";
}

function closeShotModal() {
    document.getElementById("shotModal").style.display = "none";
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}

async function refresh() {
    const [winResp, sinkResp, statusResp] = await Promise.all([
        fetch("/api/windows"),
        fetch("/api/audio/sinks"),
        fetch("/api/status")
    ]);
    const winData = await winResp.json();
    const sinkData = await sinkResp.json();
    let statusData = null;
    try { statusData = await statusResp.json(); } catch (e) { /* ignore */ }
    if (statusData) {
        if (statusData.display_width) OUTPUT_WIDTH = statusData.display_width;
        if (statusData.display_height) OUTPUT_HEIGHT = statusData.display_height;
        updateStatusBadge(statusData);
        applyCanvasAspect();
        renderOutputs(statusData.outputs, statusData.output_name);
        handleDisplayEvents(statusData.display_events);
        const reso = document.getElementById("status-resolution");
        if (reso) reso.innerText = `${statusData.display_width} × ${statusData.display_height}`;
        renderShotInterval(statusData.screenshot_interval);
    }

    // 同步画布上的持久区域（HDMI 实际窗口）。
    // visible=false 是已最小化的窗口：不画到画布上（几何无意义），
    // 但仍保留在下方窗口列表里，提供"恢复窗口"入口
    const windows = winData.windows || [];
    const visibleWindows = windows.filter(w => w.visible !== false);
    updateScale();
    currentRegions = visibleWindows;
    if (selectedPid !== null && !visibleWindows.some(w => w.pid === selectedPid)) {
        selectedPid = null;
    }
    renderRegions();

    // 窗口列表（可见窗口点击条目 = 在画布上选中；最小化条目只提供恢复操作）
    const winList = document.getElementById("win-list");
    const winCount = document.getElementById("win-count");
    const hiddenCount = windows.length - visibleWindows.length;
    if (winCount) {
        winCount.innerText = hiddenCount
            ? `${visibleWindows.length} 显示 / ${hiddenCount} 最小化`
            : `${windows.length}`;
    }
    if (winList) {
        winList.innerHTML = "";
        if (windows.length === 0) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.innerText = "暂无显示区域：先在上方选择程序，再在画布上拖出区域";
            winList.appendChild(empty);
        } else {
            windows.forEach(w => {
                winList.appendChild(buildWinItem(w));
            });
        }
    }

    // 音频设备列表（音量滑块拖动时不要重建，否则触屏拖拽会被 3 秒轮询打断）
    renderSinkList(sinkData.sinks || []);
}

function buildWinItem(w) {
    const div = document.createElement("div");
    const appLabel = w.app_name
        ? escapeHtml(String(w.app_name).replace(/^web:/, ""))
        : `PID ${w.pid}`;
    const stop = (label) =>
        `<button class="danger" onclick="event.stopPropagation();stopApp(${w.pid})">${label}</button>`;

    if (w.visible === false) {
        // 已最小化：没有屏幕几何，不参与画布选中，只给恢复/停止两个动作
        div.className = "win-item is-minimized";
        div.innerHTML =
            `<div class="win-info">` +
                `<span class="pid">${appLabel}</span>` +
                `<span class="min-badge">已最小化</span>` +
            `</div>` +
            `<div class="coords"><span class="coord-chip">不在屏幕上</span></div>` +
            `<span class="actions">` +
                `<button class="primary" onclick="event.stopPropagation();activateWin(${w.pid})">恢复窗口</button>` +
                stop("停止进程") +
            `</span>`;
        return div;
    }

    // 全屏状态以后端 _NET_WM_STATE 为准，尺寸判断只作兜底
    const isFs = w.fullscreen || (w.width >= OUTPUT_WIDTH && w.height >= OUTPUT_HEIGHT);
    div.className = "win-item" + (isFs ? " is-fullscreen" : "") +
                    (w.pid === selectedPid ? " is-selected" : "");
    div.title = "点击在画布上选中该区域";
    div.addEventListener("click", () => selectRegion(w.pid));
    div.innerHTML =
        `<div class="win-info">` +
            `<span class="pid">${appLabel}</span>` +
            (isFs ? `<span class="fs-badge">全屏中</span>` : "") +
            (w.above ? `<span class="above-badge">已置顶</span>` : "") +
        `</div>` +
        `<div class="coords">` +
            `<span class="coord-chip">X ${w.x}</span>` +
            `<span class="coord-chip">Y ${w.y}</span>` +
            `<span class="coord-chip coord-size">${w.width} × ${w.height}</span>` +
        `</div>` +
        `<span class="actions">` +
            (isFs
                ? `<button onclick="event.stopPropagation();setFullscreen(${w.pid}, false)">退出全屏</button>`
                : `<button class="primary" onclick="event.stopPropagation();setFullscreen(${w.pid}, true)">全屏</button>`) +
            `<button class="${w.above ? "warn" : ""}" ` +
                `onclick="event.stopPropagation();toggleAbove(${w.pid}, ${w.above ? "false" : "true"})">` +
                `${w.above ? "取消置顶" : "置顶"}</button>` +
            `<button onclick="event.stopPropagation();minimizeWin(${w.pid})">最小化</button>` +
            `<button class="warn" onclick="event.stopPropagation();closeWin(${w.pid})">关闭窗口</button>` +
            stop("停止进程") +
        `</span>`;
    return div;
}

function renderSinkList(sinks) {
    const sinkList = document.getElementById("audio-sink-list");
    if (!sinkList) return;
    // 用户正在拖滑块/点静音时跳过本轮重建：触摸拖拽期间 DOM 被替换会丢指针
    const active = document.activeElement;
    if (active && sinkList.contains(active)) return;
    sinkList.innerHTML = "";
    if (sinks.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.innerText = "未检测到音频设备";
        sinkList.appendChild(empty);
        return;
    }
    sinks.forEach(sink => {
        const vol = (typeof sink.volume === "number" && sink.volume >= 0) ? sink.volume : 0;
        const card = document.createElement("div");
        card.className = "sink-card" + (sink.is_default ? "active" : "");
        card.dataset.sinkId = sink.id;
        // sink.id 是 pactl 设备名；事件全部走闭包绑定，不拼内联 onclick
        card.innerHTML =
            `<button type="button" class="sink-main">` +
                `<div class="sink-name">${escapeHtml(sink.name)}</div>` +
                `<div class="sink-desc">${escapeHtml(sink.desc)}${sink.is_default ? " · 默认输出" : ""}</div>` +
            `</button>` +
            `<div class="sink-vol-row">` +
                `<button type="button" class="sink-mute${sink.muted ? " is-muted" : ""}" ` +
                    `title="静音 / 取消静音">${sink.muted ? "🔇" : "🔊"}</button>` +
                `<input type="range" class="sink-vol" min="0" max="100" value="${vol}">` +
                `<span class="sink-vol-val">${vol}%</span>` +
            `</div>`;
        card.querySelector(".sink-main").onclick = () => setAudioSink(sink.id);
        card.querySelector(".sink-mute").onclick = (e) => {
            e.stopPropagation();
            toggleSinkMute(sink.id, !sink.muted);
        };
        const range = card.querySelector(".sink-vol");
        range.oninput = () => onSinkVolumeInput(range, sink.id);
        sinkList.appendChild(card);
    });
}

window.onload = function () {
    initTheme();
    loadStatus().then(() => {
        refresh();
        setInterval(refresh, 3000);
    });
    loadInstalledApps();
    loadProfiles();
    window.addEventListener("resize", applyCanvasAspect);

    // Modal: 点 backdrop 关闭 + ESC 关闭（日志与截图两个弹窗）
    const modal = document.getElementById("logModal");
    if (modal) {
        modal.addEventListener("click", (e) => {
            if (e.target === modal) closeLogModal();
        });
    }
    const shotModal = document.getElementById("shotModal");
    if (shotModal) {
        shotModal.addEventListener("click", (e) => {
            if (e.target === shotModal) closeShotModal();
        });
    }
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        closeLogModal();
        closeShotModal();
    });
};
