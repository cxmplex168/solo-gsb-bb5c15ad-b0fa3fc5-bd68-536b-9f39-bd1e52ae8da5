/* Webhook 事件回放沙箱 — 前端逻辑（原生 JS，轮询刷新） */
"use strict";

const $ = (id) => document.getElementById(id);
const RECEIVER_NAME = "default";
let currentSessionId = localStorage.getItem("replay.sid") || null;
let expanded = new Set();       // 展开尝试详情的事件 id
let pollTimer = null;

/* ---------- 工具 ---------- */

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
  return resp.json();
}

function fmtTime(epoch) {
  if (!epoch) return "-";
  return new Date(epoch * 1000).toLocaleString("zh-CN", { hour12: false });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function flash(el, text, ok = true) {
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
  setTimeout(() => { el.textContent = ""; el.className = "msg"; }, 4000);
}

/* ---------- 示例事件 ---------- */

function sampleEvents() {
  const types = ["user.created", "order.paid", "order.shipped", "invoice.issued",
                 "user.updated", "payment.refunded", "order.cancelled", "user.deleted"];
  const base = Date.now() / 1000 - 3600;
  let t = base;
  return types.map((type, i) => {
    t += 60 + Math.floor(Math.random() * 600); // 间隔 1~11 分钟（虚拟时间）
    return {
      timestamp: new Date(t * 1000).toISOString(),
      payload: {
        type,
        id: "evt_" + (1000 + i),
        data: { object: { id: "obj_" + (2000 + i), amount: (Math.random() * 500).toFixed(2) } },
      },
    };
  });
}

$("btn-sample").onclick = () => {
  $("f-events").value = JSON.stringify(sampleEvents(), null, 2);
};

/* ---------- 创建会话 ---------- */

$("btn-create").onclick = async () => {
  let events;
  try {
    events = JSON.parse($("f-events").value);
    if (!Array.isArray(events)) throw new Error("必须是 JSON 数组");
  } catch (e) {
    flash($("create-msg"), "事件 JSON 解析失败：" + e.message, false);
    return;
  }
  try {
    const data = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        name: $("f-name").value.trim() || "replay-session",
        secret: $("f-secret").value,
        target_url: $("f-target").value.trim(),
        speed: parseFloat($("f-speed").value),
        max_retries: parseInt($("f-max-retries").value, 10),
        retry_base_delay: parseFloat($("f-base-delay").value),
        events,
      }),
    });
    currentSessionId = data.session.id;
    localStorage.setItem("replay.sid", currentSessionId);
    expanded.clear();
    flash($("create-msg"), "会话已创建：" + currentSessionId);
    await refreshSessions();
    await refreshSession();
  } catch (e) {
    flash($("create-msg"), "创建失败：" + e.message, false);
  }
};

/* ---------- 会话列表 ---------- */

async function refreshSessions() {
  const { sessions } = await api("/api/sessions");
  const sel = $("session-select");
  sel.innerHTML = "";
  for (const s of sessions) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = `${s.name} (${s.id}) — ${s.status} ${s.delivered || 0}/${s.total || 0}`;
    sel.appendChild(opt);
  }
  if (currentSessionId && sessions.some((s) => s.id === currentSessionId)) {
    sel.value = currentSessionId;
  } else if (sessions.length) {
    currentSessionId = sessions[0].id;
    localStorage.setItem("replay.sid", currentSessionId);
  } else {
    currentSessionId = null;
  }
  $("control-card").hidden = !currentSessionId;
  $("events-card").hidden = !currentSessionId;
}

$("session-select").onchange = () => {
  currentSessionId = $("session-select").value;
  localStorage.setItem("replay.sid", currentSessionId);
  expanded.clear();
  refreshSession();
};

/* ---------- 会话状态渲染 ---------- */

async function refreshSession() {
  if (!currentSessionId) return;
  let data;
  try {
    data = await api(`/api/sessions/${currentSessionId}`);
    $("conn-status").textContent = "已连接";
    $("conn-status").className = "badge muted";
  } catch (e) {
    $("conn-status").textContent = "连接失败：" + e.message;
    $("conn-status").className = "badge failed";
    return;
  }
  const { session, events, progress } = data;

  const st = $("s-status");
  st.textContent = { idle: "空闲", running: "运行中", paused: "已暂停", completed: "已完成" }[session.status] || session.status;
  st.className = "badge " + session.status;

  $("s-progress").textContent =
    `成功 ${progress.delivered} / 失败 ${progress.failed} / 待投递 ${progress.pending}（共 ${progress.total}）`;
  const done = progress.delivered + progress.failed;
  $("s-progress-bar").style.width = progress.total ? (done / progress.total * 100) + "%" : "0";

  // 按钮可用性
  $("btn-start").disabled = !(session.status === "idle" || session.status === "paused");
  $("btn-pause").disabled = session.status !== "running";
  $("btn-resume").disabled = session.status !== "paused";
  $("btn-step").disabled = !(session.status === "idle" || session.status === "paused");

  renderEvents(events, session);
}

function renderEvents(events, session) {
  const tbody = $("events-tbody");
  tbody.innerHTML = "";
  for (const e of events) {
    const tr = document.createElement("tr");
    tr.className = "ev-row";
    const lastAttempt = e.attempt_list[e.attempt_list.length - 1];
    const sig = lastAttempt && lastAttempt.signature ? lastAttempt.signature : "";
    const respCls = e.last_response_code == null ? ""
      : (e.last_response_code < 300 ? "resp-ok" : "resp-bad");
    tr.innerHTML = `
      <td>${e.seq}</td>
      <td class="mono">${fmtTime(e.ts)}</td>
      <td><span class="badge ${e.status}">${{ pending: "待投递", delivering: "投递中", retrying: "等待重试", delivered: "已送达", failed: "失败" }[e.status]}</span></td>
      <td>${e.attempts}</td>
      <td class="${respCls}">${e.last_response_code ?? "-"}</td>
      <td class="sig" title="${esc(sig)}">${sig ? esc(sig.slice(0, 16)) + "…" : "-"}</td>
      <td class="err-text">${e.last_error ? esc(e.last_error.slice(0, 60)) : ""}</td>
      <td></td>`;
    const actionTd = tr.lastElementChild;
    if (e.status === "failed" && (session.status === "idle" || session.status === "paused" || session.status === "completed")) {
      const btn = document.createElement("button");
      btn.className = "mini-btn";
      btn.textContent = "重试";
      btn.onclick = (ev) => { ev.stopPropagation(); manualRetry(e.id); };
      actionTd.appendChild(btn);
    }
    tr.onclick = () => {
      expanded.has(e.id) ? expanded.delete(e.id) : expanded.add(e.id);
      refreshSession();
    };
    tbody.appendChild(tr);

    if (expanded.has(e.id)) {
      const detail = document.createElement("tr");
      detail.className = "attempts-row";
      const td = document.createElement("td");
      td.colSpan = 8;
      td.innerHTML = `<div class="mono" style="color:var(--muted);margin-bottom:6px">报文体：${esc(e.body)}</div>`;
      if (!e.attempt_list.length) {
        td.innerHTML += `<div class="msg">尚无投递尝试</div>`;
      }
      for (const a of e.attempt_list) {
        const div = document.createElement("div");
        div.className = "attempt";
        const codeCls = a.response_code == null ? "" : (a.response_code < 300 ? "resp-ok" : "resp-bad");
        div.innerHTML = `
          <div class="head">
            <span><b>#${a.attempt_no}</b> ${a.kind === "manual" ? "手动重试" : "自动投递"}</span>
            <span>${fmtTime(a.started_at)}</span>
            ${a.delay_before > 0 ? `<span style="color:var(--yellow)">重试前等待 ${a.delay_before.toFixed(1)}s</span>` : ""}
            <span>耗时 ${a.duration_ms == null ? "-" : a.duration_ms.toFixed(0) + "ms"}</span>
            <span class="${codeCls}">响应 ${a.response_code ?? "-"}</span>
          </div>
          <div class="sig">签名 sha256=${esc(a.signature || "-")}（时间戳 ${esc(a.sig_ts || "-")}）</div>
          ${a.error ? `<div class="err-text">${esc(a.error)}</div>` : ""}
          ${a.response_body ? `<pre>${esc(a.response_body)}</pre>` : ""}`;
        td.appendChild(div);
      }
      detail.appendChild(td);
      tbody.appendChild(detail);
    }
  }
}

/* ---------- 控制按钮 ---------- */

async function ctl(path, el) {
  try {
    await api(`/api/sessions/${currentSessionId}${path}`, { method: "POST" });
    await refreshSession();
  } catch (e) {
    flash(el, e.message, false);
  }
}

$("btn-start").onclick = () => ctl("/start", $("ctl-msg"));
$("btn-pause").onclick = () => ctl("/pause", $("ctl-msg"));
$("btn-resume").onclick = () => ctl("/resume", $("ctl-msg"));
$("btn-step").onclick = () => ctl("/step", $("ctl-msg"));
$("btn-reset").onclick = () => ctl("/reset", $("ctl-msg"));

$("btn-delete").onclick = async () => {
  if (!confirm("确定删除该会话及其全部投递记录？")) return;
  await api(`/api/sessions/${currentSessionId}`, { method: "DELETE" });
  currentSessionId = null;
  localStorage.removeItem("replay.sid");
  expanded.clear();
  await refreshSessions();
  await refreshSession();
};

$("btn-export-json").onclick = () => { location.href = `/api/sessions/${currentSessionId}/export?format=json`; };
$("btn-export-csv").onclick = () => { location.href = `/api/sessions/${currentSessionId}/export?format=csv`; };

async function manualRetry(eventId) {
  try {
    await api(`/api/sessions/${currentSessionId}/events/${eventId}/retry`, { method: "POST" });
  } catch (e) {
    flash($("ctl-msg"), "重试失败：" + e.message, false);
  }
  await refreshSession();
}

/* ---------- 接收端 ---------- */

async function refreshReceiver() {
  const data = await api(`/api/receivers/${RECEIVER_NAME}`);
  const c = data.config;
  $("r-mode").value = c.mode;
  $("r-fail-code").value = c.fail_code;
  $("r-fail-rate").value = c.fail_rate;
  $("r-latency").value = c.latency_ms;
  if (document.activeElement !== $("r-secret")) $("r-secret").value = c.secret;

  const tbody = $("receiver-tbody");
  tbody.innerHTML = "";
  for (const item of data.log) {
    const tr = document.createElement("tr");
    const valid = item.signature_valid == null ? "—"
      : (item.signature_valid ? '<span class="resp-ok">✓ 有效</span>' : '<span class="resp-bad">✗ 无效</span>');
    tr.innerHTML = `
      <td class="mono">${fmtTime(item.received_at)}</td>
      <td class="mono">${esc(item.event_id || "-")}</td>
      <td>${esc(item.attempt || "-")}</td>
      <td>${valid}</td>
      <td class="mono" style="max-width:420px;word-break:break-all">${esc(item.body.slice(0, 120))}</td>`;
    tbody.appendChild(tr);
  }
}

$("btn-save-receiver").onclick = async () => {
  try {
    await api(`/api/receivers/${RECEIVER_NAME}/config`, {
      method: "PUT",
      body: JSON.stringify({
        mode: $("r-mode").value,
        fail_code: parseInt($("r-fail-code").value, 10),
        fail_rate: parseFloat($("r-fail-rate").value),
        latency_ms: parseInt($("r-latency").value, 10),
        secret: $("r-secret").value,
      }),
    });
    flash($("receiver-msg"), "接收端配置已保存");
  } catch (e) {
    flash($("receiver-msg"), e.message, false);
  }
};

$("btn-clear-log").onclick = async () => {
  await api(`/api/receivers/${RECEIVER_NAME}/log`, { method: "DELETE" });
  await refreshReceiver();
};

/* ---------- 启动 ---------- */

function init() {
  $("f-target").value = `${location.origin}/receiver/${RECEIVER_NAME}`;
  $("receiver-url").textContent = `POST ${location.origin}/receiver/${RECEIVER_NAME}`;
  $("f-events").value = JSON.stringify(sampleEvents(), null, 2);
  refreshSessions().then(refreshSession);
  refreshReceiver();
  pollTimer = setInterval(() => {
    refreshSession();
    refreshReceiver();
  }, 1000);
}

init();
