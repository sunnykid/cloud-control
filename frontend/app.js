"use strict";

// API 응답 등 외부 문자열은 모두 textContent로만 화면에 넣는다.

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

async function getJson(path) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("NETWORK_ERROR", "서버에 연결할 수 없습니다.");
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    // 본문이 JSON이 아니면 아래에서 상태 코드로 처리한다.
  }
  if (!response.ok) {
    const detail = body && body.detail;
    if (detail && typeof detail === "object" && detail.code) {
      throw new ApiError(detail.code, detail.message || "요청이 실패했습니다.");
    }
    throw new ApiError(`HTTP_${response.status}`, "요청이 실패했습니다.");
  }
  if (body === null) {
    throw new ApiError("INVALID_RESPONSE", "응답 형식이 올바르지 않습니다.");
  }
  return body;
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
  return `[${err.code || "ERROR"}] ${err.message}`;
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
  const el = $(id);
  el.className = `card-value ${kind}`;
  el.textContent = value;
}

// 목록 공통 처리: 로딩 → 오류 / 빈 목록 / 행 표시
async function loadList({ path, statusId, tableId, countId, emptyText, renderRows }) {
  const status = $(statusId);
  const table = $(tableId);
  const tbody = table.querySelector("tbody");

  setStatus(status, "loading", "불러오는 중…");
  setCount(countId, "loading", "불러오는 중…");
  table.hidden = true;

  try {
    const data = await getJson(path);
    const items = Array.isArray(data.items) ? data.items : [];
    tbody.replaceChildren(...renderRows(items));
    setCount(countId, "", String(data.count ?? items.length));
    if (items.length === 0) {
      setStatus(status, "empty", emptyText);
    } else {
      clearStatus(status);
      table.hidden = false;
    }
  } catch (err) {
    tbody.replaceChildren();
    setStatus(status, "error", errorText(err));
    setCount(countId, "error", "조회 실패");
  }
}

function ec2Rows(items) {
  return items.map((it) =>
    row(
      cell(it.name),
      cell(it.instance_id, "mono"),
      cell(it.state, `state-${it.state}`),
      cell(it.instance_type),
      cell(it.private_ip, "mono"),
      cell(it.public_ip, "mono"),
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

async function loadContext() {
  const status = $("context-status");
  const fields = ["ctx-account", "ctx-region", "ctx-arn"];
  setStatus(status, "loading", "계정 정보를 불러오는 중…");
  fields.forEach((id) => ($(id).textContent = "-"));
  try {
    const data = await getJson("/api/context");
    $("ctx-account").textContent = data.account_id;
    $("ctx-region").textContent = data.region;
    $("ctx-arn").textContent = data.caller_arn;
    clearStatus(status);
  } catch (err) {
    setStatus(status, "error", errorText(err));
  }
}

let loading = false;

async function refreshAll() {
  if (loading) return;
  loading = true;
  const button = $("refresh");
  button.disabled = true;
  try {
    await Promise.all([
      loadContext(),
      loadList({ path: "/api/ec2", statusId: "ec2-status", tableId: "ec2-table", countId: "count-ec2", emptyText: "EC2 인스턴스가 없습니다.", renderRows: ec2Rows }),
      loadList({ path: "/api/s3", statusId: "s3-status", tableId: "s3-table", countId: "count-s3", emptyText: "S3 버킷이 없습니다.", renderRows: s3Rows }),
      loadList({ path: "/api/security-groups", statusId: "sg-status", tableId: "sg-table", countId: "count-sg", emptyText: "보안 그룹이 없습니다.", renderRows: sgRows }),
    ]);
    const now = new Date().toLocaleString("ko-KR");
    $("ctx-updated").textContent = now;
    $("last-updated").textContent = `마지막 조회: ${now}`;
  } finally {
    button.disabled = false;
    loading = false;
  }
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
$("refresh").addEventListener("click", refreshAll);

refreshAll();
