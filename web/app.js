/* MoonLan web UI, v0.4 */

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
  ports: document.getElementById("ports"),
  portsTitle: document.getElementById("ports-title"),
  portsBody: document.getElementById("ports-body"),
  portsClose: document.getElementById("ports-close"),
  alarms: document.getElementById("alarms"),
  alarmsBtn: document.getElementById("alarms-btn"),
  alarmsBadge: document.getElementById("alarms-badge"),
  alarmsBody: document.getElementById("alarms-body"),
  alarmsClose: document.getElementById("alarms-close"),
  emptyState: document.getElementById("empty-state"),
  langRu: document.getElementById("lang-ru"),
  langEn: document.getElementById("lang-en"),
};

let network = null;
let nodesDs = null;
let edgesDs = null;
let topology = { switches: [], links: [], hosts: [], pseudo_switches: [] };
let isScanning = false;
let shownDetails = null; // {type: "node"|"link", id} — to re-render on language switch
let lastEvents = null; // cached journal events for re-render
let activeAlarms = []; // refreshed with the topology — badge and red borders
let lastAlarms = null; // {active, cleared} cached for the alarms panel
let portsIp = null; // switch whose ports panel is open
let portsTimer = null; // its 30 s auto-refresh
let lastPorts = null; // cached ports payload for re-render

const REFRESH_MS = 30000;

const colors = {
  moon: "#e8e4d5",
  link: "#7fb4d9",
  text: "#d7dee9",
  dim: "#8593a8",
  ok: "#7fc98f",
  panel: "#141c2c",
  alarm: "#d96b6b",
};

/* ---------- localization ---------- */

const LANG_KEY = "moonlan-lang";
let lang = localStorage.getItem(LANG_KEY);
if (lang !== "ru" && lang !== "en") {
  lang = (navigator.language || "").toLowerCase().startsWith("ru") ? "ru" : "en";
}

function t(key) {
  return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
}

/* t() with {placeholder} substitution */
function fmt(key, params) {
  let s = t(key);
  for (const [name, value] of Object.entries(params)) {
    s = s.replace("{" + name + "}", value);
  }
  return s;
}

function locale() {
  return lang === "ru" ? "ru-RU" : "en-US";
}

/* Fill in all static texts for the current language */
function applyStatic() {
  document.documentElement.lang = lang;
  document.title = t("title");
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-html]")) {
    el.innerHTML = t(el.dataset.i18nHtml);
  }
  els.search.placeholder = t("searchPlaceholder");
  els.detailsClose.title = t("close");
  els.journalClose.title = t("close");
  els.langRu.classList.toggle("active", lang === "ru");
  els.langEn.classList.toggle("active", lang === "en");
}

function setLang(newLang) {
  if (newLang === lang) return;
  lang = newLang;
  localStorage.setItem(LANG_KEY, lang);
  applyStatic();
  updateScanStatus();
  renderSidebar();
  renderGraph();
  // re-render whatever panel is open
  if (!els.details.classList.contains("hidden") && shownDetails) {
    if (shownDetails.type === "link") showLinkDetails(shownDetails.id);
    else showDetails(shownDetails.id);
  }
  if (!els.journal.classList.contains("hidden") && lastEvents) {
    renderJournal(lastEvents);
  }
  if (!els.ports.classList.contains("hidden") && lastPorts) {
    renderPorts(lastPorts);
  }
  if (!els.alarms.classList.contains("hidden") && lastAlarms) {
    renderAlarms();
  }
}

/* ---------- helpers ---------- */

/* Host caption: name, else IP, else MAC */
function hostLabel(h) {
  return h.name || h.ip || h.mac;
}

/* "11 (ipmi)" — VLAN ID plus name when known */
function vlanLabel(v) {
  if (!v) return "—";
  const name = (topology.vlan_names || {})[v];
  return name ? v + " (" + name + ")" : String(v);
}

function fmtSpeed(mbps) {
  if (!mbps) return "";
  return mbps >= 1000 ? mbps / 1000 + " " + t("gbps") : mbps + " " + t("mbps");
}

function linkId(link) {
  return "link:" + link.a + "|" + link.b;
}

/* "34" for big rates, "3.4" for small ones, "—" when unknown */
function fmtRate(v) {
  if (v == null) return "—";
  return v >= 10 ? String(Math.round(v)) : (+v).toFixed(1);
}

/* Link caption: "LACP 2×1 Gbit/s" for an aggregate, "LAG trunk" for a
   trunk detected via synthetic bridge-ports, otherwise the speed;
   plus the current load ("↓34 ↑12 Mbit/s") when counters know it */
function linkLabel(link) {
  let label;
  if (link.lag && link.lag.count > 1) {
    label = t("lacp") + " " + link.lag.count + "×" + fmtSpeed(link.speed_mbps / link.lag.count);
  } else if (link.lag && link.lag.trunk) {
    const speed = fmtSpeed(link.speed_mbps);
    label = speed ? t("lagTrunk") + " " + speed : t("lagTrunk");
  } else {
    label = fmtSpeed(link.speed_mbps);
  }
  if (link.load) {
    // out of the parent side = downstream (↓), into it = upstream (↑)
    const load =
      "↓" + fmtRate(link.load.out_mbps) + " ↑" + fmtRate(link.load.in_mbps) +
      " " + t("mbps");
    label = label ? label + " · " + load : load;
  }
  return label;
}

/* Port name in the link card; synthetic trunk ports get a "(LAG)" mark */
function linkPortLabel(link, port) {
  if (link.lag && link.lag.trunk && port.startsWith("bridge-port")) {
    return port + " (LAG)";
  }
  return port;
}

/* Status dot class: alive / silent / no IP (cannot ping) */
function statusClass(h) {
  if (!h.ip) return "noip";
  return h.ping_up ? "up" : "down";
}

function statusColor(h) {
  if (!h.ip) return colors.link;
  return h.ping_up ? colors.ok : colors.dim;
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(locale()) : "—";
}

function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(locale()) : "—";
}

function updateScanStatus() {
  if (isScanning) {
    els.scanStatus.textContent = t("scanning");
  } else {
    els.scanStatus.textContent = topology.last_scan
      ? t("scanPrefix") + new Date(topology.last_scan * 1000).toLocaleString(locale())
      : t("noData");
  }
}

/* ---------- data loading and rendering ---------- */

async function loadTopology() {
  const [topoRes, alarmsRes] = await Promise.all([
    fetch("/api/topology"),
    fetch("/api/alarms?active=1"),
  ]);
  topology = await topoRes.json();
  activeAlarms = (await alarmsRes.json()).alarms || [];
  renderBadge();
  renderSidebar();
  renderGraph();
  updateScanStatus();
  els.emptyState.classList.toggle("hidden", topology.switches.length > 0);
}

function renderBadge() {
  els.alarmsBadge.textContent = activeAlarms.length;
  els.alarmsBadge.classList.toggle("hidden", activeAlarms.length === 0);
  els.alarmsBadge.classList.toggle(
    "critical",
    activeAlarms.some((a) => a.severity === "critical")
  );
}

function switchHasAlarm(ip) {
  return activeAlarms.some(
    (a) => a.type === "switch_down" && a.subject === ip
  );
}

function li(main, sub, dotClass, onClick, searchText) {
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
  item.dataset.search = (searchText || main + " " + sub).toLowerCase();
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
        hostLabel(h) + (h.monitored ? " ★" : ""),
        [h.ip, h.mac, h.vlan ? "VLAN " + h.vlan : ""]
          .filter(Boolean)
          .join(" · "),
        statusClass(h),
        () => focusNode("host:" + h.mac),
        // VLAN is intentionally excluded from search
        [hostLabel(h), h.ip, h.mac].filter(Boolean).join(" ")
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
    // a switch_down alarm paints the node border red
    const border = switchHasAlarm(sw.ip) ? colors.alarm : colors.moon;
    nodes.push({
      id: "sw:" + sw.ip,
      label: sw.name + "\n" + sw.ip,
      shape: "box",
      color: {
        background: colors.panel,
        border: border,
        highlight: { background: "#1c2739", border: border },
      },
      font: { color: colors.text, face: "system-ui" },
      borderWidth: switchHasAlarm(sw.ip) ? 3 : 2,
      margin: 10,
    });
  }

  for (const link of topology.links) {
    const isLacp = link.lag && link.lag.count > 1;
    const isTrunk = link.lag && link.lag.trunk;
    edges.push({
      id: linkId(link),
      from: "sw:" + link.a,
      to: "sw:" + link.b,
      label: linkLabel(link),
      color: { color: colors.moon, opacity: 0.8 },
      width: isLacp ? 5 : isTrunk ? 4 : 3,
      font: { color: colors.dim, size: 11, strokeWidth: 0 },
    });
  }

  for (const ps of topology.pseudo_switches || []) {
    nodes.push({
      id: ps.id,
      label: t("pseudoTitle"),
      shape: "square",
      size: 14,
      color: {
        background: "#3a4356",
        border: colors.dim,
        highlight: { background: "#4a5468", border: colors.moon },
      },
      shapeProperties: { borderDashes: [4, 4] },
      borderWidth: 2,
      font: { color: colors.dim, size: 11 },
    });
    edges.push({
      id: "psedge:" + ps.id,
      from: "sw:" + ps.switch,
      to: ps.id,
      dashes: [4, 4],
      color: { color: colors.dim, opacity: 0.7 },
      width: 2,
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
      from: host.via || "sw:" + host.switch,
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

  // Silent update: change data inside the DataSet without recreating
  // the Network, so node positions and the camera are preserved
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

/* ---------- detail cards ---------- */

function showDetails(nodeId) {
  let html = "";
  if (nodeId.startsWith("sw:")) {
    const sw = topology.switches.find((s) => "sw:" + s.ip === nodeId);
    if (!sw) return;
    html = `<h3>${sw.name}</h3><dl>
      <dt>${t("ipAddr")}</dt><dd>${sw.ip}</dd>
      <dt>${t("bridgeMac")}</dt><dd>${sw.mac || "—"}</dd>
      <dt>${t("portsUpTotal")}</dt><dd>${sw.ports_up} / ${sw.ports_total}</dd>
      <dt>${t("lastReply")}</dt><dd>${fmtTime(sw.last_ping_ok)}</dd>
      <dt>${t("descr")}</dt><dd>${sw.descr || "—"}</dd></dl>
      <button id="ports-btn" class="panel-btn">${t("portsBtn")}</button>`;
  } else if (nodeId.startsWith("pseudo:")) {
    const ps = (topology.pseudo_switches || []).find((p) => p.id === nodeId);
    if (!ps) return;
    html = `<h3>${t("pseudoTitle")}</h3>
      <p class="hint">${fmt("pseudoHint", { n: ps.host_count })}</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${ps.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${ps.port}</dd>
      <dt>${t("devicesBehindPort")}</dt><dd>${ps.host_count}</dd></dl>`;
  } else {
    const host = topology.hosts.find((h) => "host:" + h.mac === nodeId);
    if (!host) return;
    html = `<h3>${hostLabel(host)}</h3><dl>
      <dt>${t("name")}</dt><dd>${host.name || "—"}</dd>
      <dt>${t("ipAddr")}</dt><dd>${host.ip || "—"}</dd>
      <dt>${t("macAddr")}</dt><dd>${host.mac}</dd>
      <dt>${t("switchLabel")}</dt><dd>${host.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${host.port}</dd>
      <dt>${t("vlan")}</dt><dd>${vlanLabel(host.vlan)}</dd>
      <dt>${t("lastReply")}</dt><dd>${fmtTime(host.last_ping_ok)}</dd>
      <dt>${t("firstSeen")}</dt><dd>${fmtDate(host.first_seen)}</dd></dl>
      <button id="monitor-btn" class="panel-btn${host.monitored ? " active" : ""}">
        ${host.monitored ? "★" : "☆"} ${t("monitorBtn")}</button>`;
  }
  shownDetails = { type: "node", id: nodeId };
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  closePorts();
  const portsBtn = document.getElementById("ports-btn");
  if (portsBtn) {
    portsBtn.addEventListener("click", () =>
      openPorts(nodeId.slice("sw:".length))
    );
  }
  const monitorBtn = document.getElementById("monitor-btn");
  if (monitorBtn) {
    monitorBtn.addEventListener("click", () =>
      toggleMonitor(nodeId.slice("host:".length))
    );
  }
}

/* Flip the host_down alarm flag of a host and re-render */
async function toggleMonitor(mac) {
  const host = topology.hosts.find((h) => h.mac === mac);
  if (!host) return;
  const res = await fetch("/api/host/" + encodeURIComponent(mac), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ monitored: !host.monitored }),
  });
  if (!res.ok) return;
  host.monitored = (await res.json()).monitored;
  renderSidebar();
  showDetails("host:" + mac);
}

function hideDetails() {
  els.details.classList.add("hidden");
  shownDetails = null;
}

/* ---------- ports panel ---------- */

async function openPorts(ip) {
  portsIp = ip;
  hideDetails();
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  await refreshPorts();
  els.ports.classList.remove("hidden");
  clearInterval(portsTimer);
  portsTimer = setInterval(refreshPorts, REFRESH_MS);
}

function closePorts() {
  els.ports.classList.add("hidden");
  clearInterval(portsTimer);
  portsTimer = null;
  portsIp = null;
}

async function refreshPorts() {
  if (!portsIp) return;
  const res = await fetch("/api/switch/" + encodeURIComponent(portsIp) + "/ports");
  lastPorts = await res.json();
  renderPorts(lastPorts);
}

function renderPorts(data) {
  els.portsTitle.textContent = fmt("portsTitle", {
    name: data.name || data.switch,
  });
  // physical ports only; the server puts active ones first
  const rows = data.ports
    .filter((p) => p.is_physical)
    .map((p) => {
      const tr = document.createElement("tr");
      if (!p.oper_up) tr.className = "port-down";
      const td = (content, cls) => {
        const cell = document.createElement("td");
        if (cls) cell.className = cls;
        cell.append(content);
        tr.append(cell);
      };
      let name = p.name;
      if (p.lag) name += " (" + p.lag + ")";
      if (p.monitored_hosts) {
        // monitored devices behind this port
        name += " ★" + (p.monitored_hosts > 1 ? p.monitored_hosts : "");
      }
      td(name);
      const dot = document.createElement("span");
      dot.className = "dot " + (p.oper_up ? "up" : "down");
      td(dot);
      td(p.oper_up && p.speed_mbps ? fmtSpeed(p.speed_mbps) : "—");
      td(fmtRate(p.in_mbps), "num");
      td(fmtRate(p.out_mbps), "num");
      td(fmtRate(p.errors_per_min), "num");
      td(fmtRate(p.discards_per_min), "num");
      return tr;
    });
  els.portsBody.replaceChildren(...rows);
}

/* Link card: ports of both ends, speed, aggregate members */
function showLinkDetails(edgeId) {
  const link = topology.links.find((l) => linkId(l) === edgeId);
  if (!link) return;
  const swName = (ip) => {
    const sw = topology.switches.find((s) => s.ip === ip);
    return sw ? sw.name : ip;
  };
  let html = `<h3>${swName(link.a)} — ${swName(link.b)}</h3><dl>
    <dt>${fmt("portOnSide", { name: swName(link.a) })}</dt><dd>${linkPortLabel(link, link.a_port)}</dd>
    <dt>${fmt("portOnSide", { name: swName(link.b) })}</dt><dd>${linkPortLabel(link, link.b_port)}</dd>`;
  // trunk speed is unknown — do not show a meaningless dash row
  if (!(link.lag && link.lag.trunk) || link.speed_mbps) {
    html += `<dt>${t("speed")}</dt><dd>${fmtSpeed(link.speed_mbps) || "—"}</dd>`;
  }
  if (link.lag && link.lag.count > 1) {
    html += `<dt>${t("lagAggregate")}</dt><dd>${linkLabel(link)}</dd>`;
    const memberList = (members) => members.join(", ") + " (" + t("lacp") + ")";
    if ((link.lag.a_members || []).length)
      html += `<dt>${fmt("portsOf", { name: swName(link.a) })}</dt><dd>${memberList(link.lag.a_members)}</dd>`;
    if ((link.lag.b_members || []).length)
      html += `<dt>${fmt("portsOf", { name: swName(link.b) })}</dt><dd>${memberList(link.lag.b_members)}</dd>`;
  }
  html += "</dl>";
  shownDetails = { type: "link", id: edgeId };
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  closePorts();
}

/* ---------- alarms ---------- */

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return seconds + t("durS");
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + t("durM");
  const hours = Math.floor(minutes / 60);
  return hours + t("durH") + " " + (minutes % 60) + t("durM");
}

async function toggleAlarms() {
  if (!els.alarms.classList.contains("hidden")) {
    els.alarms.classList.add("hidden");
    return;
  }
  const [act, cleared] = await Promise.all([
    fetch("/api/alarms?active=1").then((r) => r.json()),
    fetch("/api/alarms?active=0&limit=50").then((r) => r.json()),
  ]);
  lastAlarms = { active: act.alarms || [], cleared: cleared.alarms || [] };
  activeAlarms = lastAlarms.active;
  renderBadge();
  renderAlarms();
  hideDetails();
  els.journal.classList.add("hidden");
  closePorts();
  els.alarms.classList.remove("hidden");
}

function renderAlarms() {
  const body = els.alarmsBody;
  body.replaceChildren();
  const now = Date.now() / 1000;
  const section = (titleKey, alarms, isActive) => {
    const h = document.createElement("h4");
    h.textContent = t(titleKey);
    body.append(h);
    if (!alarms.length) {
      const empty = document.createElement("p");
      empty.className = "no-alarms";
      empty.textContent = t(titleKey === "alarmsActive" ? "noAlarms" : "noEvents");
      body.append(empty);
      return;
    }
    const ul = document.createElement("ul");
    ul.className = "alarm-list";
    for (const a of alarms) {
      const li = document.createElement("li");
      const head = document.createElement("div");
      head.className = "alarm-head";
      const sev = document.createElement("span");
      sev.className = "sev sev-" + a.severity;
      sev.textContent = t("al_" + a.type);
      const subject = document.createElement("span");
      subject.className = "alarm-subject";
      subject.textContent = a.display || a.subject;
      head.append(sev, subject);
      const sub = document.createElement("div");
      sub.className = "alarm-sub";
      sub.textContent = isActive
        ? fmtTime(a.ts_raised) + " · " + fmtDuration(now - a.ts_raised)
        : fmtTime(a.ts_raised) + " · " + t("clearedAt") + fmtTime(a.ts_cleared);
      li.append(head, sub);
      ul.append(li);
    }
    body.append(ul);
  };
  section("alarmsActive", lastAlarms.active, true);
  section("alarmsCleared", lastAlarms.cleared, false);
}

/* ---------- journal ---------- */

function renderJournal(events) {
  els.journalList.replaceChildren(
    ...events.map((ev) => {
      const item = document.createElement("li");
      const time = document.createElement("span");
      time.className = "ev-time";
      time.textContent = fmtTime(ev.ts);
      const type = document.createElement("span");
      type.className =
        {
          new_mac: "ev-new",
          host_down: "ev-down",
          host_up: "ev-up",
          alarm_raised: "ev-down",
          alarm_cleared: "ev-up",
        }[ev.event] || "";
      // events arrive as codes; unknown codes are shown as-is
      const translated = t("ev_" + ev.event);
      type.textContent = translated === "ev_" + ev.event ? ev.event : translated;
      const host = document.createElement("span");
      host.className = "ev-host";
      host.textContent = ev.name || ev.ip || ev.mac;
      item.append(time, type, host);
      return item;
    })
  );
  if (!events.length) {
    const empty = document.createElement("li");
    empty.textContent = t("noEvents");
    els.journalList.append(empty);
  }
}

async function toggleJournal() {
  if (!els.journal.classList.contains("hidden")) {
    els.journal.classList.add("hidden");
    return;
  }
  const res = await fetch("/api/journal?limit=100");
  const data = await res.json();
  lastEvents = data.events;
  renderJournal(lastEvents);
  hideDetails();
  closePorts();
  els.alarms.classList.add("hidden");
  els.journal.classList.remove("hidden");
}

/* ---------- search and rescan ---------- */

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
  isScanning = true;
  updateScanStatus();
  await fetch("/api/scan", { method: "POST" });
  // poll the status until the scan finishes
  const timer = setInterval(async () => {
    const status = await (await fetch("/api/status")).json();
    if (!status.scanning) {
      clearInterval(timer);
      els.rescan.disabled = false;
      isScanning = false;
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
els.portsClose.addEventListener("click", closePorts);
els.alarmsBtn.addEventListener("click", toggleAlarms);
els.alarmsClose.addEventListener("click", () =>
  els.alarms.classList.add("hidden")
);
els.langRu.addEventListener("click", () => setLang("ru"));
els.langEn.addEventListener("click", () => setLang("en"));

applyStatic();
loadTopology();
setInterval(loadTopology, REFRESH_MS);
