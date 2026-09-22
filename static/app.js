/* Webhook 事件回放沙箱 — 原生前端（无框架、无构建） */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { view: "new", currentId: null, pollTimer: null, boxes: [] };

/* ---------------- 基础工具 ---------------- */

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return resp.status === 204 ? null : resp.json();
}

const action = async (path, opts) => {
  await api(path, opts);
  if (state.view === "detail") await refreshDetail();
  if (state.view === "sessions") await refreshSessions();
};

function fmtTime(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  return d.toLocaleTimeString("zh-CN", { hour12: false }) +
    "." + String(d.getMilliseconds()).padStart(3, "0");
}

function fmtClock(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function statusChip(s) {
  const labels = {
    created: "未开始", running: "运行中", paused: "已暂停", pausing: "暂停中",
    succeeded: "已完成", failed: "失败", stopped: "已停止",
  };
  return `<span class="chip ${esc(s)}">${labels[s] || esc(s)}</span>`;
}

/* ---------------- 视图切换 / 轮询 ---------------- */

function show(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + view)
  );
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view)
  );
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  if (view === "sessions") {
    refreshSessions();
    state.pollTimer = setInterval(refreshSessions, 1500);
  } else if (view === "detail") {
    refreshDetail();
    state.pollTimer = setInterval(refreshDetail, 1000);
  } else if (view === "receivers") {
    refreshBoxes();
  }
}

document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => show(b.dataset.view))
);

/* ---------------- 新建回放：示例 / 预览 / 创建 ---------------- */

const EXAMPLE = `[
  {"id": "evt_001", "type": "order.created", "timestamp": "2026-09-22T10:00:00Z",
   "order_id": "o-1001", "amount": 99.0, "currency": "CNY"},
  {"id": "evt_002", "type": "payment.intent", "timestamp": "2026-09-22T10:00:03Z",
   "order_id": "o-1001", "channel": "alipay"},
  {"id": "evt_003", "type": "order.paid", "timestamp": "2026-09-22T10:00:09Z",
   "order_id": "o-1001", "amount": 99.0},
  {"id": "evt_004", "type": "invoice.issued", "timestamp": "2026-09-22T10:00:20Z",
   "order_id": "o-1001", "invoice_no": "INV-20260922-0001"},
  {"id": "evt_005", "type": "order.fulfilled", "timestamp": "2026-09-22T10:00:36Z",
   "order_id": "o-1001", "warehouse": "WH-SH-1"}
]`;

$("btnExample").addEventListener("click", () => { $("eventsText").value = EXAMPLE; });

$("btnPreview").addEventListener("click", async () => {
  const box = $("preview");
  box.classList.remove("hidden");
  box.innerHTML = "解析中…";
  try {
    const r = await api("/api/parse-preview", {
      method: "POST",
      body: JSON.stringify({ events_text: $("eventsText").value }),
    });
    const rows = r.events
      .slice(0, 50)
      .map(
        (e) => `<tr><td>${e.idx}</td><td>${esc(e.type)}</td>
        <td class="mono">${esc(e.timestamp)}</td><td class="mono">${esc(JSON.stringify(e.event_id))}</td></tr>`
      )
      .join("");
    box.innerHTML = `
      <div class="pv-head">
        <span>事件数 <b>${r.count}</b></span>
        <span>起始 <b>${esc(r.start)}</b></span>
        <span>结束 <b>${esc(r.end)}</b></span>
        <span>原始时间跨度 <b>${(r.span_ms / 1000).toFixed(1)}s</b></span>
      </div>
      <table><thead><tr><th>#</th><th>类型</th><th>时间戳</th><th>事件 ID</th></tr></thead>
      <tbody>${rows}</tbody></table>
      ${r.count > 50 ? `<p class="hint">仅预览前 50 条</p>` : ""}`;
  } catch (e) {
    box.innerHTML = `<span style="color:var(--red)">✗ ${esc(e.message)}</span>`;
  }
});

$("btnCreate").addEventListener("click", async () => {
  const body = {
    name: $("fName").value,
    events_text: $("eventsText").value,
    target_url: $("fTarget").value,
    secret: $("fSecret").value,
    signature_scheme: $("fScheme").value,
    header_prefix: $("fPrefix").value || "X-Webhook",
    speed: parseFloat($("fSpeed").value) || 1,
    retry_max: parseInt($("fRetryMax").value, 10),
    retry_base_ms: parseInt($("fRetryBase").value, 10),
    retry_factor: parseFloat($("fRetryFactor").value) || 2,
    on_failure: $("fOnFailure").value,
    autostart: $("fAutostart").checked,
  };
  try {
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify(body) });
    toast("回放会话已创建");
    state.currentId = s.id;
    show("detail");
  } catch (e) {
    toast(e.message, true);
  }
});

/* ---------------- 会话列表 ---------------- */

async function refreshSessions() {
  const list = await api("/api/sessions");
  if (!list.length) {
    $("sessionList").innerHTML =
      '<p class="muted">还没有回放会话，去「新建回放」创建一个吧。</p>';
    return;
  }
  $("sessionList").innerHTML = list
    .map(
      (s) => `
      <div class="item" data-id="${s.id}">
        <div class="line1">
          <span class="name">${esc(s.name)}</span>
          ${statusChip(s.status)}
          <span class="muted" style="font-size:12px">${s.delivered_count}/${s.event_count} 已投递</span>
          <span class="actions" data-stop>
            <button class="btn small ghost act-open">打开</button>
          </span>
        </div>
        <div class="sub">${esc(s.target_url)}</div>
        <div class="sub">创建于 ${fmtClock(s.created_at)} · 速度 ${s.speed}x · 重试 ${s.retry_max} 次 / ${s.retry_base_ms}ms ×${s.retry_factor}</div>
      </div>`
    )
    .join("");
  $("sessionList").querySelectorAll(".item").forEach((el) =>
    el.addEventListener("click", () => {
      state.currentId = el.dataset.id;
      show("detail");
    })
  );
}
$("btnRefreshSessions").addEventListener("click", refreshSessions);

/* ---------------- 会话详情 / 时间轴 ---------------- */

$("btnBack").addEventListener("click", () => show("sessions"));

["Start", "Pause", "Step", "Resume", "Stop"].forEach((name) => {
  $("btn" + name).addEventListener("click", async () => {
    const map = {
      Start: "start", Pause: "pause", Step: "step", Resume: "resume", Stop: "stop",
    };
    try {
      await action(`/api/sessions/${state.currentId}/${map[name]}`, { method: "POST" });
    } catch (e) {
      toast(e.message, true);
    }
  });
});

$("dSpeed").addEventListener("change", async () => {
  const speed = parseFloat($("dSpeed").value);
  if (!speed || speed <= 0) return;
  try {
    await api(`/api/sessions/${state.currentId}`, {
      method: "PATCH",
      body: JSON.stringify({ speed }),
    });
    toast(`回放速度 ${speed}x`);
  } catch (e) {
    toast(e.message, true);
  }
});

$("btnDelete").addEventListener("click", async () => {
  if (!confirm("确定删除该回放会话及其所有投递记录？")) return;
  try {
    await api(`/api/sessions/${state.currentId}`, { method: "DELETE" });
    toast("已删除");
    show("sessions");
  } catch (e) {
    toast(e.message, true);
  }
});

$("btnExportJson").addEventListener("click", (e) => {
  e.currentTarget.href = `/api/sessions/${state.currentId}/export?format=json`;
});
$("btnExportCsv").addEventListener("click", (e) => {
  e.currentTarget.href = `/api/sessions/${state.currentId}/export?format=csv`;
});

async function refreshDetail() {
  const id = state.currentId;
  if (!id) return show("sessions");
  let s;
  try {
    s = await api(`/api/sessions/${id}`);
  } catch (e) {
    show("sessions");
    return;
  }
  $("detailTitle").textContent = s.name;
  $("detailStatus").outerHTML = (() => {
    const el = document.createElement("span");
    el.id = "detailStatus";
    el.className = "chip " + s.status;
    el.textContent = { created: "未开始", running: "运行中", paused: "已暂停",
      succeeded: "已完成", failed: "失败", stopped: "已停止" }[s.status] || s.status;
    return el;
  })();

  // controls
  const active = ["running", "paused", "pausing"].includes(s.status);
  $("btnStart").disabled = active;
  $("btnPause").disabled = s.status !== "running";
  $("btnStep").disabled = !active;
  $("btnResume").disabled = s.status !== "paused";
  $("btnStop").disabled = !active;
  if (document.activeElement !== $("dSpeed")) $("dSpeed").value = s.speed;

  // meta
  const signLabel = { "hmac-sha256": "HMAC-SHA256", "hmac-sha1": "HMAC-SHA1",
    "hmac-sha512": "HMAC-SHA512", none: "不签名" }[s.signature_scheme];
  $("detailMeta").innerHTML = `
    <div><span class="k">目标 URL</span><span class="v">${esc(s.target_url)}</span></div>
    <div><span class="k">签名</span><span class="v">${signLabel} · 前缀 ${esc(s.header_prefix)}</span></div>
    <div><span class="k">密钥</span><span class="v">${s.secret ? esc(s.secret) : "<空>"}</span></div>
    <div><span class="k">重试策略</span><span class="v">最多 ${s.retry_max} 次 · ${s.retry_base_ms}ms ×${s.retry_factor} · ${s.on_failure === "abort" ? "失败中止" : "失败继续"}</span></div>
    <div><span class="k">进度</span><span class="v">${s.cursor} / ${s.events.length}</span></div>
    <div><span class="k">开始 / 结束</span><span class="v">${fmtClock(s.started_at)} / ${fmtClock(s.finished_at)}</span></div>
    ${s.error ? `<div style="grid-column:1/-1"><span class="k">错误</span><span class="v" style="color:var(--red)">${esc(s.error)}</span></div>` : ""}
  `;

  renderTimeline(s);
}

function renderTimeline(s) {
  const delivByIdx = Object.fromEntries(s.deliveries.map((d) => [d.event_idx, d]));
  const head = `
    <div class="tl-head">
      <span>#</span><span>原始时间戳</span><span>事件</span>
      <span>状态</span><span>响应码</span><span>尝试次数</span>
    </div>`;
  const rows = s.events
    .map((ev) => {
      const d = delivByIdx[ev.idx];
      const cursorAt = ev.idx === s.cursor && s.status !== "succeeded";
      let stateBadge = '<span class="badge pending">等待中</span>';
      let code = '<span class="tl-code c0">—</span>';
      let attempts = "";
      let detail = "";
      if (d) {
        const stateText = { delivering: "投递中", retrying: "重试中…", success: "成功",
          failed: "失败", stopped: "已停止", pending: "等待" }[d.state] || d.state;
        stateBadge = `<span class="badge ${d.state}">${stateText}</span>`;
        if (d.status_code != null) {
          const cls = d.status_code < 300 ? "c2" : d.status_code < 500 ? "c4" : "c5";
          code = `<span class="tl-code ${cls}">${d.status_code}</span>`;
        } else if (d.state === "delivering" || d.state === "retrying") {
          code = '<span class="tl-code c0">…</span>';
        }
        const retryMs = d.attempts.slice(1).map((a) => a.scheduled_in_ms).filter(Boolean);
        attempts = `${d.attempt_count}/${d.max_attempts}` +
          (retryMs.length ? `<div class="muted" style="font-size:11px">${retryMs.map((m) => m + "ms").join(" → ")}</div>` : "");
        detail = renderAttempts(d, ev);
      } else if (cursorAt && s.status === "running") {
        stateBadge = '<span class="badge delivering">投递中</span>';
      }
      return `
        <div class="tl-row ${detail ? "clickable" : ""}" data-idx="${ev.idx}">
          <span class="tl-idx">${ev.idx}${cursorAt ? " ▸" : ""}</span>
          <span class="tl-time">${esc(fmtTime(ev.timestamp))}<br>
            <span class="muted" style="font-size:10.5px">虚拟 ${d && d.virtual_ts ? esc(fmtTime(d.virtual_ts)) : "—"}</span></span>
          <span class="tl-type">${esc(ev.type)}<span class="eid">${esc(JSON.stringify(ev.event_id))}</span></span>
          <span>${stateBadge}</span>
          <span>${code}</span>
          <span class="tl-attempts">${attempts}</span>
          <div class="tl-detail">${detail}</div>
        </div>`;
    })
    .join("");
  $("timeline").innerHTML = head + rows;

  $("timeline").querySelectorAll(".tl-row").forEach((row) => {
    if (!row.querySelector(".tl-detail").children.length) return;
    row.style.cursor = "pointer";
    row.addEventListener("click", (e) => {
      if (e.target.closest("button, pre, a")) return; // 复制按钮等不折叠
      row.classList.toggle("open");
    });
  });
}

function renderAttempts(d, ev) {
  const payload = JSON.stringify(ev.payload, null, 2);
  const sigBlock = d.signature
    ? `<div class="sig-line">🔑 ${esc(d.signature)}
         <button class="copy-btn" data-copy="${esc(d.signature)}">复制</button></div>`
    : '<div class="muted" style="font-size:11px">未签名</div>';
  const attemptsHtml = d.attempts
    .map((a) => {
      const codeTxt = a.status_code != null
        ? `<span class="tl-code ${a.status_code < 300 ? "c2" : "c5"}">${a.status_code}</span>`
        : "—";
      const wait = a.scheduled_in_ms ? `等待 ${a.scheduled_in_ms}ms 后重试 · ` : "";
      return `
        <div class="attempt">
          <div class="a-head">
            <span class="badge ${a.state}">第 ${a.attempt} 次 · ${wait}${esc(a.state)}</span>
            ${codeTxt}
            <span class="muted">${a.started_at ? new Date(a.started_at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : ""}</span>
          </div>
          ${a.error ? `<pre style="color:var(--red)">${esc(a.error)}</pre>` : ""}
          ${a.response_excerpt ? `<pre>↩ ${esc(a.response_excerpt)}</pre>` : ""}
        </div>`;
    })
    .join("");
  return `
    ${sigBlock}
    <div class="muted" style="font-size:11px;margin:6px 0 2px">请求体（${payload.length} 字节）：</div>
    <pre>${esc(payload)}</pre>
    ${d.error ? `<div style="color:var(--red);font-size:12px;margin:6px 0">✗ ${esc(d.error)}</div>` : ""}
    <div class="muted" style="font-size:11px;margin:8px 0 2px">投递尝试：</div>
    ${attemptsHtml || '<span class="muted">尚无尝试</span>'}`;
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  e.stopPropagation();
  try {
    await navigator.clipboard.writeText(btn.dataset.copy);
    toast("已复制签名");
  } catch (_) {
    toast("复制失败", true);
  }
});

/* ---------------- 本地接收端 ---------------- */

$("btnCreateBox").addEventListener("click", async () => {
  try {
    await api("/api/catchboxes", {
      method: "POST",
      body: JSON.stringify({
        name: $("bName").value || "receiver",
        fail_status: parseInt($("bFailStatus").value, 10) || 0,
        fail_every: parseInt($("bFailEvery").value, 10) || 0,
      }),
    });
    toast("接收端已创建");
    refreshBoxes();
  } catch (e) {
    toast(e.message, true);
  }
});

async function refreshBoxes() {
  state.boxes = await api("/api/catchboxes");
  // feed the new-session target datalist
  $("targetList").innerHTML = state.boxes
    .map((b) => `<option value="${location.origin}/catch/${b.token}">${esc(b.name)}</option>`)
    .join("");

  if (!state.boxes.length) {
    $("boxList").innerHTML = '<p class="muted" style="margin-top:14px">还没有接收端，创建一个即可获得投递 URL。</p>';
    return;
  }
  $("boxList").innerHTML = state.boxes
    .map(
      (b) => `
      <div class="item" style="cursor:default">
        <div class="line1">
          <span class="name">📥 ${esc(b.name)}</span>
          <span class="muted">${b.request_count} 次请求 · ${b.fail_count} 次失败</span>
          <span class="actions">
            <button class="btn small act-send" data-token="${b.token}">用作目标</button>
            <button class="btn small act-reqs" data-token="${b.token}">查看请求</button>
            <button class="btn small ghost act-del" data-token="${b.token}">删除</button>
          </span>
        </div>
        <div class="url-box">${location.origin}/catch/${b.token}
          <button class="copy-btn" data-copy="${location.origin}/catch/${b.token}">复制 URL</button></div>
        <div class="sub">
          失败模拟：${b.fail_status ? `每 ${b.fail_every || 1} 次返回 ${b.fail_status}` : "关闭"}
          <button class="btn small ghost act-edit" data-token="${b.token}" style="margin-left:8px">编辑</button>
        </div>
      </div>`
    )
    .join("");
}

$("boxList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const token = btn.dataset.token;
  if (btn.classList.contains("act-send")) {
    $("fTarget").value = `${location.origin}/catch/${token}`;
    show("new");
    toast("已填入目标 URL");
  } else if (btn.classList.contains("act-reqs")) {
    openRequests(token);
  } else if (btn.classList.contains("act-del")) {
    if (!confirm("删除该接收端及其捕获记录？")) return;
    await api(`/api/catchboxes/${token}`, { method: "DELETE" });
    refreshBoxes();
  } else if (btn.classList.contains("act-edit")) {
    const box = state.boxes.find((b) => b.token === token);
    const failStatus = prompt("失败状态码（0 = 关闭失败模拟）", box.fail_status);
    if (failStatus === null) return;
    const failEvery = prompt("每 N 次请求失败一次（1=每次，0=关闭）", box.fail_every);
    if (failEvery === null) return;
    await api(`/api/catchboxes/${token}`, {
      method: "PATCH",
      body: JSON.stringify({
        fail_status: parseInt(failStatus, 10) || 0,
        fail_every: parseInt(failEvery, 10) || 0,
      }),
    });
    refreshBoxes();
  }
});

/* ---------------- 捕获请求弹窗 ---------------- */

async function openRequests(token) {
  const reqs = await api(`/api/catchboxes/${token}/requests?limit=100`);
  $("modal").classList.remove("hidden");
  if (!reqs.length) {
    $("modalContent").innerHTML = '<p class="muted">还没有捕获到请求。启动一个指向该 URL 的回放试试。</p>';
    return;
  }
  $("modalContent").innerHTML =
    reqs
      .map((r) => {
        const headers = typeof r.headers === "string" ? JSON.parse(r.headers) : r.headers;
        const sigKeys = Object.keys(headers).filter((k) => k.toLowerCase().includes("signature"));
        const sig = sigKeys.map((k) => `${k}: ${headers[k]}`).join("\n");
        return `
        <div class="req-item" data-id="${r.id}">
          <div class="line1">
            <strong>${r.method}</strong>
            <span class="badge ${r.status_code < 300 ? "success" : "failed"}">${r.status_code}</span>
            <span class="muted">${new Date(r.received_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</span>
          </div>
          ${sig ? `<pre class="mono" style="margin:6px 0 0;color:var(--cyan)">${esc(sig)}</pre>` : ""}
          <pre class="mono" style="margin:6px 0 0;max-height:120px">${esc(r.body || "")}</pre>
        </div>`;
      })
      .join("");
}

$("modalClose").addEventListener("click", () => $("modal").classList.add("hidden"));
$("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") $("modal").classList.add("hidden");
});

/* ---------------- 启动 ---------------- */
show("new");
refreshBoxes();
