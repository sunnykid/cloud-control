"use strict";

// API 응답 등 외부 문자열은 모두 textContent로만 화면에 넣는다.
// 앱 토큰은 이 변수(JS 메모리)에만 보관한다. URL·localStorage·콘솔에 넣지 않는다.
let appToken = null;
// 로그아웃·재연결 후 늦게 도착한 이전 응답이 화면에 그려지지 않도록 세대를 구분한다.
let generation = 0;
let loading = false;

// 조회 결과(메모리)
let lastContext = null;
let ec2Items = [];
let ec2Loaded = false;
let actionItems = null; // null이면 이력을 모르는 상태(서버 검사에 맡긴다)

// 제어 상태
let actionInFlight = false;
let pendingConfirm = null;
// 응답을 받지 못한 요청: instance_id → {instanceId, name, action, requestId}. 새 키로 자동 재요청하지 않는다.
const lostRequests = new Map();

const ACTION_LABEL = { start: "시작", stop: "중지" };
const REQUIRED_STATE = { start: "stopped", stop: "running" };
const OPEN_STATUSES = new Set(["REQUESTED", "UNKNOWN"]);

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(code, message, { lost = false, detail = null } = {}) {
    super(message);
    this.code = code;
    this.lost = lost; // 서버 응답을 받지 못해 결과를 알 수 없음
    this.detail = detail;
  }
}

class StaleResponse extends Error {}

async function apiRequest(path, { method = "GET", body } = {}) {
  const gen = generation;
  if (!appToken) throw new ApiError("APP_AUTH_REQUIRED", "앱 토큰을 입력하세요.");

  const headers = { Accept: "application/json", Authorization: `Bearer ${appToken}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError("NETWORK_ERROR", "서버 응답을 받지 못했습니다.", { lost: true });
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // 본문이 JSON이 아니면 아래에서 처리한다.
  }
  if (gen !== generation) throw new StaleResponse();

  if (!response.ok) {
    const detail = data && data.detail;
    if (response.status === 401) {
      // 앱 토큰 거부: 입력 화면으로 돌아간다. AWS 로그인 만료(503 AWS_LOGIN_REQUIRED)와는 다르다.
      logout("[APP_AUTH_REQUIRED] 앱 토큰 인증에 실패했습니다(401). 토큰을 다시 입력하세요.");
      throw new StaleResponse();
    }
    if (detail && typeof detail === "object" && detail.code) {
      if (detail.code === "AWS_LOGIN_REQUIRED") showAwsLoginBanner(true);
      throw new ApiError(detail.code, detail.message || "요청이 실패했습니다.", { detail });
    }
    // 앱의 오류 형식이 아니면(프록시 등) 서버 처리 여부를 알 수 없다.
    throw new ApiError(`HTTP_${response.status}`, "요청 결과를 확인할 수 없습니다.", { lost: true });
  }
  if (data === null) {
    throw new ApiError("INVALID_RESPONSE", "응답 형식이 올바르지 않습니다.", { lost: true });
  }
  return { status: response.status, data };
}

async function getJson(path) {
  return (await apiRequest(path)).data;
}

function setStatus(el, kind, text) {
  el.className = `status ${kind}`;
  el.textContent = text;
}

function clearStatus(el) {
  el.className = "status";
  el.textContent = "";
}

function errorText(err) {
  const hint = err.code === "AWS_LOGIN_REQUIRED" ? " (앱 토큰 문제 아님 — aws login --profile lab 후 다시 조회)" : "";
  return `[${err.code || "ERROR"}] ${err.message}${hint}`;
}

function showAwsLoginBanner(show) {
  $("aws-login-banner").hidden = !show;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text === null || text === undefined || text === "" ? "-" : String(text);
  if (className) td.className = className;
  return td;
}

function row(...cells) {
  const tr = document.createElement("tr");
  tr.append(...cells);
  return tr;
}

function setCount(id, kind, value) {
  if (!id) return;
  const el = $(id);
  el.className = `card-value ${kind}`;
  el.textContent = value;
}

const countBy = (items, pred) => String(items.filter(pred).length);

// cards: 대시보드 숫자. 응답의 items로 직접 계산한다.
const EC2_LIST = {
  path: "/api/ec2", statusId: "ec2-status", tableId: "ec2-table",
  cards: {
    "count-ec2": (items) => String(items.length),
    "count-ec2-running": (items) => countBy(items, (it) => it.state === "running"),
    "count-ec2-stopped": (items) => countBy(items, (it) => it.state === "stopped"),
  },
  render: (items) => { ec2Items = items; ec2Loaded = true; renderEc2Table(); },
  onError: () => { ec2Items = []; ec2Loaded = false; $("ec2-filter-info").textContent = ""; },
};
const ACTIONS_LIST = {
  path: "/api/actions", statusId: "actions-status", tableId: "actions-table", emptyText: "제어 이력이 없습니다.",
  cards: { "count-actions-open": (items) => countBy(items, (a) => OPEN_STATUSES.has(a.status)) },
  renderRows: actionRows,
  onError: () => { actionItems = null; if (ec2Loaded) renderEc2Table(); },
};
const LISTS = [
  EC2_LIST,
  { path: "/api/s3", statusId: "s3-status", tableId: "s3-table", emptyText: "S3 버킷이 없습니다.", renderRows: s3Rows,
    cards: { "count-s3": (items) => String(items.length) } },
  { path: "/api/security-groups", statusId: "sg-status", tableId: "sg-table", emptyText: "보안 그룹이 없습니다.", renderRows: sgRows,
    cards: { "count-sg": (items) => String(items.length) } },
  ACTIONS_LIST,
];

function setCards(list, kind, valueOf) {
  for (const [id, compute] of Object.entries(list.cards || {})) setCount(id, kind, valueOf(compute));
}

// 목록 공통 처리: 로딩 → 오류 / 빈 목록 / 행 표시
async function loadList(list) {
  const { path, statusId, tableId, emptyText, renderRows, render, onError } = list;
  const status = $(statusId);
  const table = $(tableId);
  const tbody = table.querySelector("tbody");

  setStatus(status, "loading", "불러오는 중…");
  setCards(list, "loading", () => "불러오는 중…");
  table.hidden = true;

  try {
    const data = await getJson(path);
    if (!Array.isArray(data.items)) throw new ApiError("INVALID_RESPONSE", "응답 형식이 올바르지 않습니다.");
    const items = data.items;
    setCards(list, "", (compute) => compute(items));
    if (render) {
      render(items); // 목록이 직접 표·빈 목록 표시를 처리한다(EC2 필터).
      return;
    }
    tbody.replaceChildren(...renderRows(items));
    if (items.length === 0) {
      setStatus(status, "empty", emptyText);
    } else {
      clearStatus(status);
      table.hidden = false;
    }
  } catch (err) {
    if (err instanceof StaleResponse) return;
    tbody.replaceChildren();
    setStatus(status, "error", errorText(err));
    setCards(list, "error", () => "확인 불가");
    if (onError) onError();
  }
}

// ---- EC2 목록과 제어 버튼 ----

function hasOpenAction(instanceId) {
  return Array.isArray(actionItems) && actionItems.some((a) => a.instance_id === instanceId && OPEN_STATUSES.has(a.status));
}

// 허용 대상이면서 stopped면 시작, running이면 중지만 활성화. 전환 상태·진행 중·응답 유실 키 보관 중이면 비활성화.
function canAct(item, action) {
  return (
    Boolean(appToken) &&
    !actionInFlight &&
    item.controllable === true &&
    item.state === REQUIRED_STATE[action] &&
    !lostRequests.has(item.instance_id) &&
    !hasOpenAction(item.instance_id)
  );
}

function actionButton(item, action) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary";
  btn.textContent = ACTION_LABEL[action];
  btn.dataset.action = action;
  btn.dataset.instanceId = item.instance_id;
  btn.disabled = !canAct(item, action);
  btn.addEventListener("click", () => openConfirm(item.instance_id, action));
  return btn;
}

function updateActionButtons() {
  document.querySelectorAll("#ec2-table button[data-action]").forEach((btn) => {
    const item = ec2Items.find((it) => it.instance_id === btn.dataset.instanceId);
    btn.disabled = !item || !canAct(item, btn.dataset.action);
  });
}

function latestAction(instanceId) {
  if (!Array.isArray(actionItems)) return null;
  return actionItems.find((a) => a.instance_id === instanceId) || null; // 최신순 정렬
}

// 이미 조회한 목록에 상태 필터만 적용한다(서버 재조회 없음).
function renderEc2Table() {
  const table = $("ec2-table");
  const status = $("ec2-status");
  const filter = $("ec2-filter").value;
  const shown = filter === "all" ? ec2Items : ec2Items.filter((it) => it.state === filter);
  table.querySelector("tbody").replaceChildren(...ec2Rows(shown));
  $("ec2-filter-info").textContent = `${shown.length} / ${ec2Items.length}대 표시`;
  if (ec2Items.length === 0) {
    setStatus(status, "empty", "EC2 인스턴스가 없습니다.");
    table.hidden = true;
  } else if (shown.length === 0) {
    setStatus(status, "empty", `상태가 ${filter}인 EC2가 없습니다.`);
    table.hidden = true;
  } else {
    clearStatus(status);
    table.hidden = false;
  }
}

function ec2Rows(items) {
  return items.map((it) => {
    const last = latestAction(it.instance_id);
    const actionsTd = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "act-buttons";
    wrap.append(actionButton(it, "start"), actionButton(it, "stop"));
    actionsTd.append(wrap);
    return row(
      cell(it.name),
      cell(it.instance_id, "mono"),
      cell(it.state, `state-${it.state}`),
      cell(it.instance_type),
      cell(it.private_ip, "mono"),
      cell(it.public_ip, "mono"),
      cell(it.controllable === true ? "허용" : "-", it.controllable === true ? "allowed" : ""),
      last ? cell(`${ACTION_LABEL[last.action] || last.action} ${last.status}`, `status-${last.status}`) : cell(actionItems === null ? "확인 불가" : "-"),
      actionsTd,
    );
  });
}

function actionRows(items) {
  actionItems = items;
  if (ec2Loaded) renderEc2Table();
  return items.map((a) =>
    row(
      cell(a.created_at ? new Date(a.created_at).toLocaleString("ko-KR") : "-"),
      cell(a.instance_id, "mono"),
      cell(ACTION_LABEL[a.action] || a.action),
      cell(a.status, `status-${a.status}`),
      cell(a.request_id, "mono"),
      cell(a.aws_request_id, "mono"),
      cell(a.message),
    ),
  );
}

function s3Rows(items) {
  return items.map((it) => row(cell(it.name)));
}

function portText(perm) {
  if (perm.IpProtocol === "-1") return "전체";
  if (perm.FromPort === undefined || perm.FromPort === null) return "전체";
  if (perm.FromPort === perm.ToPort) return String(perm.FromPort);
  return `${perm.FromPort}-${perm.ToPort}`;
}

function protocolText(proto) {
  return proto === "-1" ? "전체" : proto;
}

// 보안 그룹의 인바운드 규칙을 소스 하나당 한 행으로 펼친다.
function sgRows(groups) {
  const rows = [];
  for (const g of groups) {
    const perms = Array.isArray(g.IpPermissions) ? g.IpPermissions : [];
    const sources = [];
    for (const p of perms) {
      const base = { proto: protocolText(p.IpProtocol), port: portText(p) };
      for (const r of p.IpRanges || []) sources.push({ ...base, src: r.CidrIp, desc: r.Description });
      for (const r of p.Ipv6Ranges || []) sources.push({ ...base, src: r.CidrIpv6, desc: r.Description });
      for (const r of p.UserIdGroupPairs || []) sources.push({ ...base, src: r.GroupId, desc: r.Description });
      for (const r of p.PrefixListIds || []) sources.push({ ...base, src: r.PrefixListId, desc: r.Description });
    }
    if (sources.length === 0) {
      rows.push(row(cell(g.GroupId, "mono"), cell(g.GroupName), cell("인바운드 규칙 없음"), cell(""), cell(""), cell("")));
      continue;
    }
    for (const s of sources) {
      rows.push(row(cell(g.GroupId, "mono"), cell(g.GroupName), cell(s.proto), cell(s.port), cell(s.src, "mono"), cell(s.desc)));
    }
  }
  return rows;
}

// ---- 확인창과 제어 요청 ----

function openConfirm(instanceId, action) {
  const item = ec2Items.find((it) => it.instance_id === instanceId);
  if (!item || !canAct(item, action)) return;
  pendingConfirm = { instanceId, name: item.name, action };
  $("confirm-account").textContent = lastContext ? lastContext.account_id : "-";
  $("confirm-region").textContent = lastContext ? lastContext.region : "-";
  $("confirm-name").textContent = item.name;
  $("confirm-id").textContent = item.instance_id;
  $("confirm-action").textContent = `${ACTION_LABEL[action]} (${action})`;
  $("confirm-approve").disabled = false;
  $("confirm-dialog").showModal();
}

// 취소: POST를 보내지 않는다.
function cancelConfirm() {
  pendingConfirm = null;
  if ($("confirm-dialog").open) $("confirm-dialog").close();
}

// 승인: 이 시점에 요청 키를 한 번 만든다.
function approveConfirm() {
  const req = pendingConfirm;
  pendingConfirm = null;
  $("confirm-approve").disabled = true;
  $("confirm-dialog").close();
  if (!req || actionInFlight) return;
  sendAction({ ...req, requestId: crypto.randomUUID() });
}

async function sendAction(req) {
  if (actionInFlight) return;
  actionInFlight = true;
  updateActionButtons();
  renderLostRequests();
  const status = $("action-status");
  const label = `${req.name} (${req.instanceId}) ${ACTION_LABEL[req.action]}`;
  setStatus(status, "loading", `${label} 요청 중…`);

  try {
    const { status: http, data } = await apiRequest(`/api/ec2/${encodeURIComponent(req.instanceId)}/actions`, {
      method: "POST",
      body: { action: req.action, request_id: req.requestId },
    });
    lostRequests.delete(req.instanceId);
    if (http === 202) {
      setStatus(status, "ok", `${label}: AWS 요청 접수 — 현재 상태는 새로고침으로 확인`);
    } else {
      setStatus(status, data.status === "ACCEPTED" ? "ok" : "loading", `${label}: 이미 처리된 요청(기존 결과 ${data.status}) — 현재 상태는 새로고침으로 확인`);
    }
  } catch (err) {
    if (err instanceof StaleResponse) return;
    if (err.lost) {
      // 응답 유실: 같은 키를 보관하고 자동 재요청하지 않는다.
      lostRequests.set(req.instanceId, req);
      setStatus(status, "error", `${label}: ${errorText(err)} 요청 키를 보관했습니다. 아래에서 같은 키로 다시 보내 결과를 확인하세요.`);
    } else {
      lostRequests.delete(req.instanceId);
      const recorded = err.detail && err.detail.status ? ` (이력 상태: ${err.detail.status})` : "";
      setStatus(status, "error", `${label}: ${errorText(err)}${recorded}`);
    }
  } finally {
    actionInFlight = false;
    renderLostRequests();
    updateActionButtons();
  }
  // 이력만 한 번 다시 읽는다(DB 조회). EC2 상태는 사용자가 새로고침한다.
  if (appToken) loadList(ACTIONS_LIST);
}

function renderLostRequests() {
  const box = $("lost-requests");
  const entries = [...lostRequests.values()].map((req) => {
    const div = document.createElement("div");
    div.className = "lost-item";
    const text = document.createElement("span");
    text.textContent = `응답 미확인: ${req.name} (${req.instanceId}) ${ACTION_LABEL[req.action]} · 요청 키 ${req.requestId}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary";
    btn.textContent = "같은 요청 키로 다시 보내기";
    btn.disabled = actionInFlight;
    btn.addEventListener("click", () => sendAction(req));
    div.append(text, btn);
    return div;
  });
  box.replaceChildren(...entries);
  box.hidden = entries.length === 0;
}

// ---- 보안 점검 ----

let checkInFlight = false;

function rangeText(f) {
  if (f.protocol === "-1") return "전체";
  if (f.from_port === null || f.from_port === undefined) return "전체";
  return f.from_port === f.to_port ? String(f.from_port) : `${f.from_port}-${f.to_port}`;
}

function checkProtocolText(protocol) {
  return protocol === "-1" ? "전체 프로토콜" : protocol;
}

function clearCheckResult() {
  $("check-summary").hidden = true;
  for (const id of ["findings-table", "checked-groups-table"]) {
    const table = $(id);
    table.querySelector("tbody").replaceChildren();
    table.hidden = true;
  }
  ["check-at", "check-instances", "check-groups", "check-findings"].forEach((id) => ($(id).textContent = "-"));
  clearStatus($("check-result"));
}

// 연결 대상(인스턴스·ENI)마다 한 행으로 펼친다. 건수(finding_count)는 펼친 행 수가 아니다.
function findingRows(findings) {
  const rows = [];
  for (const f of findings) {
    const conns = Array.isArray(f.connections) && f.connections.length ? f.connections : [{}];
    for (const c of conns) {
      rows.push(
        row(
          cell(c.instance_name),
          cell(c.instance_id, "mono"),
          cell(c.state, `state-${c.state}`),
          cell(c.network_interface_id, "mono"),
          cell(f.group_id, "mono"),
          cell(f.group_name),
          cell(f.ip_family),
          cell(f.cidr, "mono"),
          cell(checkProtocolText(f.protocol)),
          cell(rangeText(f)),
          cell(Array.isArray(f.management_ports) ? f.management_ports.join(", ") : "-"),
          cell(f.result, "finding"),
        ),
      );
    }
  }
  return rows;
}

function checkedGroupRows(groups, findings) {
  const countByGroup = new Map();
  for (const f of findings) countByGroup.set(f.group_id, (countByGroup.get(f.group_id) || 0) + 1);
  const rows = [];
  for (const g of groups) {
    const n = countByGroup.get(g.group_id) || 0;
    const result = n ? `공개 관리 포트 규칙: 검토 필요 (${n}건)` : "이번 점검 조건에 해당하는 규칙 없음";
    const conns = Array.isArray(g.connections) && g.connections.length ? g.connections : [{}];
    for (const c of conns) {
      rows.push(
        row(
          cell(g.group_id, "mono"),
          cell(g.group_name),
          cell(c.instance_name),
          cell(c.instance_id, "mono"),
          cell(c.state, `state-${c.state}`),
          cell(c.network_interface_id, "mono"),
          cell(result, n ? "finding" : ""),
        ),
      );
    }
  }
  return rows;
}

// 클릭할 때마다 서버에서 새로 조회한다. 이전 결과는 먼저 지워 새 결과처럼 보이지 않게 한다.
async function runSecurityCheck() {
  if (checkInFlight) return;
  checkInFlight = true;
  const button = $("run-check");
  const status = $("check-status");
  button.disabled = true;
  clearCheckResult();
  setStatus(status, "loading", "점검 중… AWS에서 EC2와 보안 그룹을 새로 조회합니다.");
  try {
    const data = await getJson("/api/security-checks");
    const findings = Array.isArray(data.findings) ? data.findings : [];
    const groups = Array.isArray(data.checked_groups) ? data.checked_groups : [];
    clearStatus(status);

    $("check-at").textContent = data.checked_at ? new Date(data.checked_at).toLocaleString("ko-KR") + ` (${data.checked_at})` : "-";
    $("check-instances").textContent = `${data.checked_instance_count}대`;
    $("check-groups").textContent = `${data.checked_group_count}개`;
    $("check-findings").textContent = `${data.finding_count}건`;

    const result = $("check-result");
    if (data.checked_group_count === 0) {
      setStatus(result, "empty", "점검 대상 EC2 연결 보안 그룹 없음");
    } else if (data.finding_count === 0) {
      setStatus(result, "ok", "이번 점검 조건에 해당하는 규칙 없음");
    } else {
      setStatus(result, "error", `공개 관리 포트 규칙: 검토 필요 (${data.finding_count}건)`);
    }

    const fTable = $("findings-table");
    fTable.querySelector("tbody").replaceChildren(...findingRows(findings));
    fTable.hidden = findings.length === 0;
    const gTable = $("checked-groups-table");
    gTable.querySelector("tbody").replaceChildren(...checkedGroupRows(groups, findings));
    gTable.hidden = groups.length === 0;
    $("check-summary").hidden = false;
  } catch (err) {
    if (err instanceof StaleResponse) return;
    // 권한·로그인·통신 실패를 '발견 없음'으로 보이지 않게 결과 영역은 숨긴 채 오류만 표시한다.
    clearCheckResult();
    setStatus(status, "error", `점검 실패: ${errorText(err)}`);
  } finally {
    checkInFlight = false;
    button.disabled = false;
  }
}

// ---- 계정 정보·전체 조회 ----

function renderContext(data) {
  lastContext = { account_id: data.account_id, region: data.region };
  $("ctx-account").textContent = data.account_id;
  $("ctx-region").textContent = data.region;
  $("ctx-arn").textContent = data.caller_arn;
}

async function loadContext() {
  const status = $("context-status");
  setStatus(status, "loading", "계정 정보를 불러오는 중…");
  try {
    renderContext(await getJson("/api/context"));
    showAwsLoginBanner(false);
    clearStatus(status);
  } catch (err) {
    if (err instanceof StaleResponse) return;
    lastContext = null;
    ["ctx-account", "ctx-region", "ctx-arn"].forEach((id) => ($(id).textContent = "-"));
    setStatus(status, "error", errorText(err));
  }
}

function markUpdated() {
  const now = new Date().toLocaleString("ko-KR");
  $("ctx-updated").textContent = now;
  $("last-updated").textContent = `마지막 조회: ${now}`;
}

async function withLoading(button, task) {
  if (loading) return;
  loading = true;
  button.disabled = true;
  try {
    await task();
  } finally {
    button.disabled = false;
    loading = false;
  }
}

async function refreshAll() {
  const gen = generation;
  await Promise.all([loadContext(), ...LISTS.map(loadList)]);
  if (gen === generation) markUpdated();
}

async function refreshEc2AndActions() {
  const gen = generation;
  await Promise.all([loadContext(), loadList(EC2_LIST), loadList(ACTIONS_LIST)]);
  if (gen === generation) markUpdated();
}

// 연결: 입력값을 메모리에 보관하고 /api/context로 검증한 뒤 나머지 목록을 조회한다.
async function connect(event) {
  event.preventDefault();
  const input = $("token-input");
  const status = $("login-status");
  const value = input.value;
  input.value = "";
  if (!value.trim()) {
    setStatus(status, "error", "앱 토큰을 입력하세요.");
    return;
  }

  await withLoading($("connect"), async () => {
    generation += 1;
    appToken = value;
    setStatus(status, "loading", "확인 중…");
    let context = null;
    let contextError = null;
    try {
      context = await getJson("/api/context");
    } catch (err) {
      if (err instanceof StaleResponse) return; // 401이면 logout()이 이미 오류를 표시했다.
      if (!err.detail) {
        // 서버 응답 자체를 받지 못해 토큰을 확인하지 못했다.
        appToken = null;
        setStatus(status, "error", errorText(err));
        return;
      }
      // 서버가 앱 오류 형식으로 답했다면 토큰 인증은 통과했다(인증이 AWS 호출보다 먼저 실행됨).
      // AWS_LOGIN_REQUIRED 등 AWS 쪽 문제는 앱 안에서 안내한다.
      contextError = err;
    }
    clearStatus(status);
    showApp(true);
    if (context) {
      renderContext(context);
      showAwsLoginBanner(false);
      clearStatus($("context-status"));
    } else {
      setStatus($("context-status"), "error", errorText(contextError));
    }
    const gen = generation;
    await Promise.all(LISTS.map(loadList));
    if (gen === generation) markUpdated();
  });
}

function resetData() {
  clearCheckResult();
  clearStatus($("check-status"));
  lastContext = null;
  ec2Items = [];
  ec2Loaded = false;
  actionItems = null;
  $("ec2-filter").value = "all";
  $("ec2-filter-info").textContent = "";
  showAwsLoginBanner(false);
  lostRequests.clear();
  pendingConfirm = null;
  actionInFlight = false;
  if ($("confirm-dialog").open) $("confirm-dialog").close();
  renderLostRequests();
  clearStatus($("action-status"));
  ["ctx-account", "ctx-region", "ctx-arn", "ctx-updated"].forEach((id) => ($(id).textContent = "-"));
  clearStatus($("context-status"));
  for (const list of LISTS) {
    const table = $(list.tableId);
    table.querySelector("tbody").replaceChildren();
    table.hidden = true;
    clearStatus($(list.statusId));
    setCards(list, "", () => "-");
  }
  $("last-updated").textContent = "조회 전";
}

function showApp(connected) {
  $("app-view").hidden = !connected;
  $("session-controls").hidden = !connected;
  $("login-view").hidden = connected;
}

// 로그아웃: 토큰과 조회 데이터(보관한 요청 키 포함)를 메모리·화면에서 지운다.
function logout(message) {
  generation += 1;
  appToken = null;
  resetData();
  selectTab("dashboard");
  showApp(false);
  $("token-input").value = "";
  if (message) setStatus($("login-status"), "error", message);
  else clearStatus($("login-status"));
  $("token-input").focus();
}

function selectTab(name) {
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === name));
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${name}`;
  });
}

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => selectTab(btn.dataset.tab));
});
$("login-form").addEventListener("submit", connect);
$("refresh").addEventListener("click", () => withLoading($("refresh"), refreshAll));
$("refresh-ec2").addEventListener("click", () => withLoading($("refresh-ec2"), refreshEc2AndActions));
$("logout").addEventListener("click", () => logout());
$("run-check").addEventListener("click", runSecurityCheck);
$("ec2-filter").addEventListener("change", () => { if (ec2Loaded) renderEc2Table(); });
$("refresh-actions").addEventListener("click", () => withLoading($("refresh-actions"), () => loadList(ACTIONS_LIST)));
$("confirm-cancel").addEventListener("click", cancelConfirm);
$("confirm-approve").addEventListener("click", approveConfirm);
// Esc로 닫는 경우도 취소로 처리한다.
$("confirm-dialog").addEventListener("cancel", () => (pendingConfirm = null));
