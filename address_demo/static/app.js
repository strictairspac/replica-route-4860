// 收货地址拆分演练 —— 前端编排（原生 JS，零依赖）
const $ = (id) => document.getElementById(id);
let state = null;
let selected = null;
let lastWriteRid = null;
let lastWritePayload = null;

const api = async (path, body) => {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.message || r.statusText);
    err.payload = data; err.status = r.status; throw err;
  }
  return data;
};

const toast = (msg, kind = "ok") => {
  const box = $("toast");
  const t = document.createElement("div");
  t.className = `t ${kind}`;
  t.innerHTML = msg.replace(/\n/g, "<br>");
  box.appendChild(t);
  setTimeout(() => t.remove(), 5200);
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function refresh() {
  state = await api("/api/state");
  render();
}

function render() {
  $("modeBadge").textContent = state.mode;
  $("modeBadge").className = `badge ${state.mode}`;
  $("primarySeq").textContent = state.seq;
  $("btnV1").disabled = state.mode === "v1";
  $("btnV2").disabled = state.mode === "v2";

  // 副本
  const rep = state.replica;
  $("repSeq").textContent = rep.applied_seq;
  $("repMax").textContent = state.seq;
  $("repFmt").textContent = rep.format;
  $("repOnline").textContent = rep.online ? "在线" : "离线";

  // 订单表
  const rows = state.orders.map((o) => {
    const cls = o.id === selected ? "sel" : "";
    const confirm = o.house_confirmed ? '<span class="badge confirmed">门牌已确认</span>' : "";
    return `<tr class="${cls}" onclick="selectOrder('${o.id}')">
      <td class="mono">${esc(o.id)}</td>
      <td><span class="badge ${o.state}">${o.state}</span> ${confirm}</td>
      <td>${esc(o.legacy_addr)}</td>
      <td>${esc(o.street) || '<span class="muted">—</span>'}</td>
      <td>${esc(o.house) || '<span class="muted">—</span>'}</td>
      <td class="mono">v${o.version}</td></tr>`;
  }).join("");
  $("orderRows").innerHTML = rows ||
    '<tr><td colspan="6" class="muted">暂无订单，先接单</td></tr>';

  // 选中提示
  const sel = state.orders.find((o) => o.id === selected);
  $("selHint").textContent = sel
    ? `选中 ${sel.id}（${sel.state}, v${sel.version}）—— 写入前请核对 expected_version`
    : "未选中订单";
  if (sel) $("wVersion").value = sel.version;

  // 任务
  $("taskRows").innerHTML = state.tasks.map((t) =>
    `<tr><td class="mono">${esc(t.order_id)}</td><td>${esc(t.reason)}</td>
     <td><span class="badge ${t.status === "open" ? "stashed" : "split"}">${t.status}</span></td></tr>`
  ).join("") || '<tr><td colspan="3" class="muted">无暂存任务</td></tr>';

  // 事件日志
  $("eventLog").innerHTML = state.events.map((e) => {
    const d = e.data;
    const reject = e.kind === "audit_reject";
    const note = d.message || d.note || d.code || "";
    return `<div class="e">
      <span class="muted">#${e.seq}</span>
      <span class="kind ${reject ? "reject" : ""}">${esc(e.kind)}</span>
      ${d.order_id ? `<span class="pill">${esc(d.order_id)}</span>` : ""}
      ${e.request_id ? `<span class="pill mono">${esc(e.request_id)}</span>` : ""}
      ${note ? `<div class="muted">${esc(note)}</div>` : ""}
    </div>`;
  }).join("");
}

function selectOrder(id) {
  selected = id;
  const o = state.orders.find((x) => x.id === id);
  if (o) {
    $("wVersion").value = o.version;
    $("wClient").value = o.state === "legacy" ? "old" : "new";
    toggleClientFields();
  }
  render();
}

function toggleClientFields() {
  const isNew = $("wClient").value === "new";
  $("oldFields").style.display = isNew ? "none" : "";
  $("newFields").style.display = isNew ? "" : "none";
}
$("wClient").addEventListener("change", toggleClientFields);

function genRid() {
  $("wRid").value = "req-" + Math.random().toString(16).slice(2, 10);
}

const requireSelection = () => {
  if (!selected) { toast("请先在订单表点击选中一笔订单", "warn"); return false; }
  return true;
};

async function createOrder(addr) {
  const full = addr ?? $("newAddr").value;
  try {
    const r = await api("/api/orders", { full_address: full });
    selected = r.body.order_id;
    toast(`接单成功 ${r.body.order_id}：整段原文已入库，状态 ${r.body.state}`);
  } catch (e) { toast(e.message, "err"); }
  await refresh();
}
const createBadOrder = () => createOrder("港区三号仓库门卫代收");

async function doMigrate() {
  if (!requireSelection()) return;
  try {
    const r = await api("/api/migrate", { order_id: selected });
    toast(`转换完成：${r.body.state === "stashed"
      ? "无法拆分，已暂存并生成人工任务（订单保留）"
      : `街道=${r.body.street} / 门牌=${r.body.house}（待确认）`}`,
      r.body.state === "stashed" ? "warn" : "ok");
  } catch (e) { toast(`${e.payload.error}：${e.message}`, "err"); }
  await refresh();
}

function buildWritePayload(rid) {
  const isNew = $("wClient").value === "new";
  return {
    client: $("wClient").value,
    order_id: selected,
    value: isNew ? $("wValue").value : $("wFull").value,
    field: isNew ? $("wField").value : null,
    expected_version: Number($("wVersion").value),
    request_id: rid,
  };
}

async function doWrite() {
  if (!requireSelection()) return;
  const rid = $("wRid").value.trim();
  if (!rid) { genRid(); }
  const useRid = $("wRid").value.trim();
  lastWriteRid = useRid;
  lastWritePayload = buildWritePayload(useRid);
  try {
    const r = await api("/api/write", lastWritePayload);
    toast(r.body.idempotent_replay
      ? `幂等重放：事件未重复生效，版本仍为 v${r.body.version}`
      : `写入成功：${r.body.state} v${r.body.version}${r.body.house_confirmed ? "（门牌已确认）" : ""}`);
  } catch (e) {
    toast(`<b>${e.payload.error}</b>：${e.message}`, "err");
  }
  await refresh();
}

async function doRetry() {
  if (!lastWritePayload) { toast("还没有可重试的写入", "warn"); return; }
  try {
    const r = await api("/api/write", lastWritePayload);
    toast(r.body.idempotent_replay
      ? `request_id=${lastWriteRid} 重试：命中幂等，未产生第二个事件，版本 v${r.body.version}`
      : "重试被当作新请求（request_id 未登记过）",
      r.body.idempotent_replay ? "ok" : "warn");
  } catch (e) {
    toast(`重试返回原始拒绝（幂等）：<b>${e.payload.error}</b>`, "warn");
  }
  await refresh();
}

async function doLostWrite() {
  if (!requireSelection()) return;
  genRid();
  const rid = $("wRid").value.trim();
  lastWriteRid = rid;
  lastWritePayload = buildWritePayload(rid);
  try {
    await api("/api/write", { ...lastWritePayload, simulate_lost: true });
  } catch (e) {
    toast(`首次请求：${e.payload.error} —— 事件其实已落库，现在自动用同一 request_id 重试…`, "warn");
  }
  await refresh();
  setTimeout(doRetry, 700);
}

async function doManual() {
  if (!requireSelection()) return;
  const street = prompt("人工拆分：街道", "港区仓库路");
  if (street === null) return;
  const house = prompt("人工拆分：门牌", "3号");
  if (house === null) return;
  try {
    const r = await api("/api/manual-resolve",
      { order_id: selected, street, house });
    toast(`人工处置完成：${r.body.street} / ${r.body.house}，门牌已确认`);
  } catch (e) { toast(`${e.payload.error}：${e.message}`, "err"); }
  await refresh();
}

async function doRead() {
  if (!requireSelection()) return;
  const body = { order_id: selected, client: $("rClient").value,
    source: $("rSource").value };
  try {
    const r = await api("/api/read", body);
    $("readOut").textContent = JSON.stringify(r, null, 2);
    toast(`读取成功：由 ${r.read_route.served_by} 服务（${r.read_route.reason}）`);
  } catch (e) {
    $("readOut").textContent = JSON.stringify(e.payload, null, 2);
    const fb = e.payload.extra?.fallback;
    toast(`<b>${e.payload.error}</b>：${e.message}${fb ? ` → 明确去向：${fb}` : ""}`, "warn");
  }
}

async function setMode(mode) {
  try {
    const r = await api("/api/mode", { mode });
    toast(mode === "v2"
      ? "已升级 v2：事件重放，dirty 订单重新转换，已确认门牌保留"
      : "已回退 v1：旧端照常整段读写，新端写被闸门拒绝");
  } catch (e) { toast(e.message, "err"); }
  await refresh();
}

async function repCmd(cmd) {
  try {
    if (cmd === "catchup") await api("/api/replica/catchup", {});
    if (cmd === "split") await api("/api/replica/format", { format: "split" });
    if (cmd === "legacy") await api("/api/replica/format", { format: "legacy" });
    if (cmd === "toggleOnline")
      await api("/api/replica/online", { online: !state.replica.online });
    toast("副本状态已更新");
  } catch (e) {
    toast(`<b>${e.payload.error}</b>：${e.message}`, "err");
  }
  await refresh();
}

async function resetAll() {
  if (!confirm("确认清空全部事件与订单？")) return;
  await api("/api/reset", {});
  selected = null; lastWritePayload = null; lastWriteRid = null;
  $("readOut").textContent = "（尚未读取）";
  toast("已重置");
  await refresh();
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- 剧本
async function runScript(name) {
  await api("/api/reset", {});
  selected = null;
  await refresh();
  const steps = SCRIPTS[name]();
  for (const s of steps) {
    try {
      if (s.call) {
        const r = await s.call();
        if (s.pick) selected = r.body.order_id;
        toast(`<b>${s.title}</b> ✓`, "ok");
      } else {
        await s.call2();
        toast(`<b>${s.title}</b> ✓`, "ok");
      }
    } catch (e) {
      toast(`<b>${s.title}</b> → ${e.payload?.error || e.message}（预期内的拒绝）`, "warn");
    }
    await refresh();
    await wait(650);
  }
  toast("剧本结束：可在事件日志逐条复核 seq 与 request_id", "ok");
}

const post = (path, body) => api(path, body);

const SCRIPTS = {
  // 完整剧本：接单→升级→拆分→并发/锁/幂等→暂存→副本→回退→再升级
  full: () => {
    const rid = (s) => "req-" + s;
    return [
      { title: "v1 接单（两单，含一单无法拆分）", pick: true,
        call: () => post("/api/orders", { full_address: "栖原市港区临港大道188号", request_id: rid("c1") }) },
      { title: "再接一单无法拆分地址（接单不受影响）",
        call: () => post("/api/orders", { full_address: "港区三号仓库门卫代收", request_id: rid("c2") }) },
      { title: "升级 v2", call: () => post("/api/mode", { mode: "v2" }) },
      { title: "转换订单 1：自动拆分", call: () => post("/api/migrate", { order_id: "ORD-001", request_id: rid("m1") }) },
      { title: "转换订单 2：拆不动 → 暂存+人工任务", call: () => post("/api/migrate", { order_id: "ORD-002", request_id: rid("m2") }) },
      { title: "旧端整段写（v1 版本号陈旧的并发）",
        call: () => post("/api/write", { client: "old", order_id: "ORD-001", value: "栖原市港区临港大道999号", expected_version: 1, request_id: rid("w1") }) },
      { title: "新端同版本写门牌 → CAS 冲突",
        call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "188号", field: "house", expected_version: 1, request_id: rid("w2") }) },
      { title: "新端按新版本确认门牌",
        call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "188号", field: "house", expected_version: 2, request_id: rid("w3") }) },
      { title: "迟到旧端整段写 → house_locked 拒绝",
        call: () => post("/api/write", { client: "old", order_id: "ORD-001", value: "栖原市港区临港大道1号", expected_version: 3, request_id: rid("w4") }) },
      { title: "同一 request_id 重试 → 幂等不重复",
        call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "188号", field: "house", expected_version: 2, request_id: rid("w3") }) },
      { title: "副本追平写入（格式仍 legacy）", call: () => post("/api/replica/catchup", {}) },
      { title: "新端读副本 → format_not_migrated，回主库",
        call2: () => post("/api/read", { order_id: "ORD-001", client: "new", source: "replica" }) },
      { title: "副本切换 split 格式", call: () => post("/api/replica/format", { format: "split" }) },
      { title: "新端读副本 → 成功", call2: () => post("/api/read", { order_id: "ORD-001", client: "new", source: "replica" }) },
      { title: "回退 v1", call: () => post("/api/mode", { mode: "v1" }) },
      { title: "v1 下新端写 → 模式闸门拒绝",
        call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "1号", field: "house", expected_version: 3, request_id: rid("v1w") }) },
      { title: "再升级 v2：记录与确认门牌都在", call: () => post("/api/mode", { mode: "v2" }) },
    ];
  },
  concurrent: () => {
    const rid = (s) => "req-c-" + s;
    return [
      { title: "接单", pick: true, call: () => post("/api/orders", { full_address: "枫林街20号", request_id: rid(1) }) },
      { title: "升级 v2", call: () => post("/api/mode", { mode: "v2" }) },
      { title: "拆分", call: () => post("/api/migrate", { order_id: "ORD-001", request_id: rid(2) }) },
      { title: "旧端基于 v1 整段写成功", call: () => post("/api/write", { client: "old", order_id: "ORD-001", value: "枫林街22号", expected_version: 1, request_id: rid(3) }) },
      { title: "新端也基于 v1 → 版本冲突", call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "20号", field: "house", expected_version: 1, request_id: rid(4) }) },
      { title: "新端重读 v2 后确认门牌", call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "22号", field: "house", expected_version: 2, request_id: rid(5) }) },
      { title: "迟到旧写 → house_locked", call: () => post("/api/write", { client: "old", order_id: "ORD-001", value: "枫林街8号", expected_version: 3, request_id: rid(6) }) },
    ];
  },
  stash: () => {
    const rid = (s) => "req-s-" + s;
    return [
      { title: "接一单无法拆分地址", pick: true, call: () => post("/api/orders", { full_address: "港区三号仓库门卫代收", request_id: rid(1) }) },
      { title: "升级 v2", call: () => post("/api/mode", { mode: "v2" }) },
      { title: "转换 → 暂存（不拒收，接单保留）", call: () => post("/api/migrate", { order_id: "ORD-001", request_id: rid(2) }) },
      { title: "新端主库读：可见 stashed 与原文", call2: () => post("/api/read", { order_id: "ORD-001", client: "new", source: "primary" }) },
      { title: "人工拆分处置 → 恢复可读", call: () => post("/api/manual-resolve", { order_id: "ORD-001", street: "港区仓库路", house: "3号", request_id: rid(3) }) },
    ];
  },
  replica: () => {
    const rid = (s) => "req-r-" + s;
    return [
      { title: "接单", pick: true, call: () => post("/api/orders", { full_address: "栖原市港区临港大道188号", request_id: rid(1) }) },
      { title: "升级 v2 并拆分", call: () => post("/api/mode", { mode: "v2" }) },
      { title: "转换", call: () => post("/api/migrate", { order_id: "ORD-001", request_id: rid(2) }) },
      { title: "新端读落后副本 → replica_lagging 回主库", call2: () => post("/api/read", { order_id: "ORD-001", client: "new", source: "replica" }) },
      { title: "副本追平写入", call: () => post("/api/replica/catchup", {}) },
      { title: "新端读 legacy 格式副本 → format_not_migrated 回主库", call2: () => post("/api/read", { order_id: "ORD-001", client: "new", source: "replica" }) },
      { title: "旧端读同一副本 → 整段正常返回", call2: () => post("/api/read", { order_id: "ORD-001", client: "old", source: "replica" }) },
      { title: "副本换 split 格式", call: () => post("/api/replica/format", { format: "split" }) },
      { title: "新端再读副本 → 成功", call2: () => post("/api/read", { order_id: "ORD-001", client: "new", source: "replica" }) },
    ];
  },
  rollback: () => {
    const rid = (s) => "req-b-" + s;
    return [
      { title: "接单 A/B 两单", pick: true, call: () => post("/api/orders", { full_address: "枫林街20号", request_id: rid(1) }) },
      { title: "接第二单", call: () => post("/api/orders", { full_address: "海棠路5号", request_id: rid(2) }) },
      { title: "升级 v2", call: () => post("/api/mode", { mode: "v2" }) },
      { title: "转换两单", call: () => post("/api/migrate", { order_id: "ORD-001", request_id: rid(3) }) },
      { title: "转换第二单", call: () => post("/api/migrate", { order_id: "ORD-002", request_id: rid(4) }) },
      { title: "确认 A 单门牌（已确认）", call: () => post("/api/write", { client: "new", order_id: "ORD-001", value: "20号", field: "house", expected_version: 1, request_id: rid(5) }) },
      { title: "回退 v1", call: () => post("/api/mode", { mode: "v1" }) },
      { title: "旧端改 B 单整段（未确认，允许）", call: () => post("/api/write", { client: "old", order_id: "ORD-002", value: "海棠路9号", expected_version: 1, request_id: rid(6) }) },
      { title: "旧端改 A 单 → house_locked", call: () => post("/api/write", { client: "old", order_id: "ORD-001", value: "枫林街30号", expected_version: 2, request_id: rid(7) }) },
      { title: "再升级 v2：A 门牌保留，B 重新拆分", call: () => post("/api/mode", { mode: "v2" }) },
    ];
  },
};

refresh();
