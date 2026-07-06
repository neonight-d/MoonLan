/* MoonLan web UI, v0.3 */

const els = {
  network: document.getElementById("network"),
  switchList: document.getElementById("switch-list"),
  hostList: document.getElementById("host-list"),
  switchCount: document.getElementById("switch-count"),
  hostCount: document.getElementById("host-count"),
  search: document.getElementById("search"),
  rescan: document.getElementById("rescan"),
  scanStatus: document.getElementById("scan-status"),
  details: document.getElementById("details"),
  detailsBody: document.getElementById("details-body"),
  detailsClose: document.getElementById("details-close"),
  journal: document.getElementById("journal"),
  journalBtn: document.getElementById("journal-btn"),
  journalList: document.getElementById("journal-list"),
  journalClose: document.getElementById("journal-close"),
  emptyState: document.getElementById("empty-state"),
};

let network = null;
let nodesDs = null;
let edgesDs = null;
let topology = { switches: [], links: [], hosts: [] };

const REFRESH_MS = 30000;

const colors = {
  moon: "#e8e4d5",
  link: "#7fb4d9",
  text: "#d7dee9",
  dim: "#8593a8",
  ok: "#7fc98f",
  panel: "#141c2c",
};

const EVENT_NAMES = {
  new_mac: "Новое устройство",
  host_down: "Хост недоступен",
  host_up: "Хост снова в сети",
};
const EVENT_CLASSES = { new_mac: "ev-new", host_down: "ev-down", host_up: "ev-up" };

/* Подпись хоста: имя, иначе IP, иначе MAC */
function hostLabel(h) {
  return h.name || h.ip || h.mac;
}

function fmtSpeed(mbps) {
  if (!mbps) return "";
  return mbps >= 1000 ? mbps / 1000 + " Гбит/с" : mbps + " Мбит/с";
}

function linkId(link) {
  return "link:" + link.a + "|" + link.b;
}

/* Подпись связи: «LACP 2×1 Гбит/с» для агрегата, иначе скорость */
function linkLabel(link) {
  if (link.lag && link.lag.count > 1) {
    return "LACP " + link.lag.count + "×" + fmtSpeed(link.speed_mbps / link.lag.count);
  }
  return fmtSpeed(link.speed_mbps);
}

/* Класс индикатора: живой / не отвечает / нет IP (ping невозможен) */
function statusClass(h) {
  if (!h.ip) return "noip";
  return h.ping_up ? "up" : "down";
}

function statusColor(h) {
  if (!h.ip) return colors.link;
  return h.ping_up ? colors.ok : colors.dim;
}

async function loadTopology() {
  const res = await fetch("/api/topology");
  topology = await res.json();
  renderSidebar();
  renderGraph();
  els.scanStatus.textContent = topology.last_scan
    ? "Опрос: " + new Date(topology.last_scan * 1000).toLocaleString()
    : "Данных пока нет";
  els.emptyState.classList.toggle(
    "hidden",
    topology.switches.length > 0
  );
}

function li(main, sub, dotClass, onClick) {
  const item = document.createElement("li");
  if (dotClass) {
    const dot = document.createElement("span");
    dot.className = "dot " + dotClass;
    item.append(dot);
  }
  const text = document.createElement("div");
  text.className = "li-text";
  const name = document.createElement("span");
  name.textContent = main;
  text.append(name);
  if (sub) {
    const extra = document.createElement("span");
    extra.className = "sub";
    extra.textContent = sub;
    text.append(extra);
  }
  item.append(text);
  item.dataset.search = (main + " " + sub).toLowerCase();
  item.addEventListener("click", onClick);
  return item;
}

function renderSidebar() {
  els.switchList.replaceChildren(
    ...topology.switches.map((sw) =>
      li(sw.name, sw.ip, sw.ping_up ? "up" : "down", () =>
        focusNode("sw:" + sw.ip)
      )
    )
  );
  els.hostList.replaceChildren(
    ...topology.hosts.map((h) =>
      li(
        hostLabel(h),
        [h.ip, h.mac].filter(Boolean).join(" · "),
        statusClass(h),
        () => focusNode("host:" + h.mac)
      )
    )
  );
  els.switchCount.textContent = topology.switches.length;
  els.hostCount.textContent = topology.hosts.length;
  applySearchFilter();
}

function buildGraphData() {
  const nodes = [];
  const edges = [];

  for (const sw of topology.switches) {
    nodes.push({
      id: "sw:" + sw.ip,
      label: sw.name + "\n" + sw.ip,
      shape: "box",
      color: {
        background: colors.panel,
        border: colors.moon,
        highlight: { background: "#1c2739", border: colors.moon },
      },
      font: { color: colors.text, face: "system-ui" },
      borderWidth: 2,
      margin: 10,
    });
  }

  for (const link of topology.links) {
    const isLag = link.lag && link.lag.count > 1;
    edges.push({
      id: linkId(link),
      from: "sw:" + link.a,
      to: "sw:" + link.b,
      label: linkLabel(link),
      color: { color: colors.moon, opacity: 0.8 },
      width: isLag ? 5 : 3,
      font: { color: colors.dim, size: 11, strokeWidth: 0 },
    });
  }

  for (const host of topology.hosts) {
    const c = statusColor(host);
    nodes.push({
      id: "host:" + host.mac,
      label: hostLabel(host),
      shape: "dot",
      size: 9,
      color: { background: c, border: c },
      font: { color: colors.dim, size: 11, face: "ui-monospace" },
    });
    edges.push({
      id: "hostedge:" + host.mac,
      from: "sw:" + host.switch,
      to: "host:" + host.mac,
      color: { color: colors.link, opacity: 0.35 },
      width: 1,
    });
  }

  return { nodes, edges };
}

function renderGraph() {
  const { nodes, edges } = buildGraphData();

  if (!network) {
    nodesDs = new vis.DataSet(nodes);
    edgesDs = new vis.DataSet(edges);
    const options = {
      physics: {
        solver: "forceAtlas2Based",
        forceAtlas2Based: { gravitationalConstant: -60, springLength: 90 },
        stabilization: { iterations: 200 },
      },
      interaction: { hover: true },
    };
    network = new vis.Network(
      els.network,
      { nodes: nodesDs, edges: edgesDs },
      options
    );
    network.on("click", (params) => {
      if (params.nodes.length) showDetails(params.nodes[0]);
      else if (
        params.edges.length &&
        String(params.edges[0]).startsWith("link:")
      )
        showLinkDetails(params.edges[0]);
      else hideDetails();
    });
    return;
  }

  // Тихое обновление: меняем данные в DataSet, не пересоздавая Network,
  // чтобы не сбрасывать позиции узлов и камеру
  const nodeIds = new Set(nodes.map((n) => n.id));
  const edgeIds = new Set(edges.map((e) => e.id));
  nodesDs.remove(nodesDs.getIds().filter((id) => !nodeIds.has(id)));
  edgesDs.remove(edgesDs.getIds().filter((id) => !edgeIds.has(id)));
  nodesDs.update(nodes);
  edgesDs.update(edges);
}

function focusNode(id) {
  if (!network) return;
  network.focus(id, { scale: 1.2, animation: true });
  network.selectNodes([id]);
  showDetails(id);
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString() : "—";
}

function showDetails(nodeId) {
  let html = "";
  if (nodeId.startsWith("sw:")) {
    const sw = topology.switches.find((s) => "sw:" + s.ip === nodeId);
    if (!sw) return;
    html = `<h3>${sw.name}</h3><dl>
      <dt>IP-адрес</dt><dd>${sw.ip}</dd>
      <dt>MAC моста</dt><dd>${sw.mac || "—"}</dd>
      <dt>Порты (активно/всего)</dt><dd>${sw.ports_up} / ${sw.ports_total}</dd>
      <dt>Отвечал</dt><dd>${fmtTime(sw.last_ping_ok)}</dd>
      <dt>Описание</dt><dd>${sw.descr || "—"}</dd></dl>`;
  } else {
    const host = topology.hosts.find((h) => "host:" + h.mac === nodeId);
    if (!host) return;
    html = `<h3>${hostLabel(host)}</h3><dl>
      <dt>Имя</dt><dd>${host.name || "—"}</dd>
      <dt>IP-адрес</dt><dd>${host.ip || "—"}</dd>
      <dt>MAC-адрес</dt><dd>${host.mac}</dd>
      <dt>Коммутатор</dt><dd>${host.switch}</dd>
      <dt>Порт</dt><dd>${host.port}</dd>
      <dt>Отвечал</dt><dd>${fmtTime(host.last_ping_ok)}</dd>
      <dt>Впервые замечен</dt><dd>${fmtDate(host.first_seen)}</dd></dl>`;
  }
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
}

function hideDetails() {
  els.details.classList.add("hidden");
}

/* Карточка связи: порты обеих сторон, скорость, состав агрегата */
function showLinkDetails(edgeId) {
  const link = topology.links.find((l) => linkId(l) === edgeId);
  if (!link) return;
  const swName = (ip) => {
    const sw = topology.switches.find((s) => s.ip === ip);
    return sw ? sw.name : ip;
  };
  let html = `<h3>${swName(link.a)} — ${swName(link.b)}</h3><dl>
    <dt>Порт со стороны ${swName(link.a)}</dt><dd>${link.a_port}</dd>
    <dt>Порт со стороны ${swName(link.b)}</dt><dd>${link.b_port}</dd>
    <dt>Скорость</dt><dd>${fmtSpeed(link.speed_mbps) || "—"}</dd>`;
  if (link.lag) {
    html += `<dt>Агрегат (LACP)</dt><dd>${linkLabel(link)}</dd>`;
    if (link.lag.a_members.length)
      html += `<dt>Порты ${swName(link.a)}</dt><dd>${link.lag.a_members.join(", ")}</dd>`;
    if (link.lag.b_members.length)
      html += `<dt>Порты ${swName(link.b)}</dt><dd>${link.lag.b_members.join(", ")}</dd>`;
  }
  html += "</dl>";
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
}

/* ---------- журнал ---------- */

async function toggleJournal() {
  if (!els.journal.classList.contains("hidden")) {
    els.journal.classList.add("hidden");
    return;
  }
  const res = await fetch("/api/journal?limit=100");
  const data = await res.json();
  els.journalList.replaceChildren(
    ...data.events.map((ev) => {
      const item = document.createElement("li");
      const time = document.createElement("span");
      time.className = "ev-time";
      time.textContent = fmtTime(ev.ts);
      const type = document.createElement("span");
      type.className = EVENT_CLASSES[ev.event] || "";
      type.textContent = EVENT_NAMES[ev.event] || ev.event;
      const host = document.createElement("span");
      host.className = "ev-host";
      host.textContent = ev.name || ev.ip || ev.mac;
      item.append(time, type, host);
      return item;
    })
  );
  if (!data.events.length) {
    const empty = document.createElement("li");
    empty.textContent = "Событий пока нет";
    els.journalList.append(empty);
  }
  hideDetails();
  els.journal.classList.remove("hidden");
}

/* ---------- поиск и опрос ---------- */

function applySearchFilter() {
  const q = els.search.value.trim().toLowerCase();
  for (const item of document.querySelectorAll(".device-list li")) {
    item.classList.toggle(
      "hidden-by-search",
      q !== "" && !item.dataset.search.includes(q)
    );
  }
}

async function rescan() {
  els.rescan.disabled = true;
  els.scanStatus.textContent = "Идёт опрос сети…";
  await fetch("/api/scan", { method: "POST" });
  // опрашиваем статус, пока сканирование не завершится
  const timer = setInterval(async () => {
    const status = await (await fetch("/api/status")).json();
    if (!status.scanning) {
      clearInterval(timer);
      els.rescan.disabled = false;
      await loadTopology();
    }
  }, 1500);
}

els.search.addEventListener("input", applySearchFilter);
els.rescan.addEventListener("click", rescan);
els.detailsClose.addEventListener("click", hideDetails);
els.journalBtn.addEventListener("click", toggleJournal);
els.journalClose.addEventListener("click", () =>
  els.journal.classList.add("hidden")
);

loadTopology();
setInterval(loadTopology, REFRESH_MS);
