/* MoonLan web UI, v0.4 */

const els = {
  network: document.getElementById("network"),
  switchList: document.getElementById("switch-list"),
  hostList: document.getElementById("host-list"),
  unlocatedSection: document.getElementById("unlocated-section"),
  unlocatedList: document.getElementById("unlocated-list"),
  unlocatedCount: document.getElementById("unlocated-count"),
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
  stp: document.getElementById("stp"),
  stpBtn: document.getElementById("stp-btn"),
  stpBody: document.getElementById("stp-body"),
  stpClose: document.getElementById("stp-close"),
  emptyState: document.getElementById("empty-state"),
  freezeBtn: document.getElementById("freeze-btn"),
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
let lastStp = null; // cached STP report for re-render on language switch
let flapWindowHours = 2; // from the API, for the FLAP tooltip
let portsIp = null; // switch whose ports panel is open
let portsTimer = null; // its 30 s auto-refresh
let lastPorts = null; // cached ports payload for re-render
let portsSort = null; // {key, dir} chosen by clicking a column header
let portsHighlight = null; // port name to mark, e.g. the one an alarm is on
let selectedNodeId = null; // its caption gets a backdrop and brighter text
let hoveredNodeId = null; // the same, weaker, while the mouse is over it

const REFRESH_MS = 30000;

const colors = {
  moon: "#e8e4d5",
  warn: "#d9a86b",
  link: "#7fb4d9",
  text: "#d7dee9",
  dim: "#8593a8",
  ok: "#7fc98f",
  panel: "#141c2c",
  alarm: "#d96b6b",
  line: "#26324a",
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
  // what the two counters actually mean — the whole point of v0.5.4
  const errHead = document.querySelector('#ports th[data-sort="err"]');
  const discHead = document.querySelector('#ports th[data-sort="disc"]');
  if (errHead) errHead.title = t("errTooltip");
  if (discHead) discHead.title = t("discTooltip");
  els.langRu.classList.toggle("active", lang === "ru");
  els.langEn.classList.toggle("active", lang === "en");
  applyFreeze();
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
  if (!els.stp.classList.contains("hidden") && lastStp) {
    renderStp();
  }
}

/* ---------- layout freeze ---------- */

/* The force layout keeps running and slowly tidies the map up; anyone
   who prefers it still can stop it by hand. Dragging works either way. */
const FREEZE_KEY = "moonlan-freeze-layout";
let layoutFrozen = localStorage.getItem(FREEZE_KEY) === "1";

function applyFreeze() {
  if (network) network.setOptions({ physics: !layoutFrozen });
  els.freezeBtn.textContent = layoutFrozen ? t("unfreezeBtn") : t("freezeBtn");
  els.freezeBtn.classList.toggle("active", layoutFrozen);
}

function toggleFreeze() {
  layoutFrozen = !layoutFrozen;
  localStorage.setItem(FREEZE_KEY, layoutFrozen ? "1" : "0");
  applyFreeze();
}

/* ---------- helpers ---------- */

/* Host caption: DNS name, else IP, else what LLDP called the device,
   else the MAC. The main router has no IP of ours (routers are kept out
   of the ARP inventory) and no reverse DNS, so before v0.6.2 it was a
   pale dot labelled 00:0e:04:b7:79:ab — while LLDP had been calling it
   "MikroTik" the whole time. */
function hostLabel(h) {
  return h.name || h.ip || lldpLabel(h) || h.mac;
}

/* The name LLDP gave a device, or its first management address */
function lldpLabel(h) {
  const lldp = h.lldp;
  if (!lldp) return "";
  return lldp.sys_name || (lldp.mgmt_ips || [])[0] || "";
}

/* True when the caption on screen came from LLDP rather than from DNS
   or ARP — the card says so, so nobody hunts for a DHCP lease */
function labelFromLldp(h) {
  return !h.name && !h.ip && !!lldpLabel(h);
}

/* "router" / "phone" / … as the device announced itself */
function deviceKind(h) {
  return (h.lldp && h.lldp.kind) || "";
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
    // speed_mbps already counts only the active members
    const total = link.lag.count;
    const active = link.lag.active == null ? total : link.lag.active;
    label = active > 0
      ? t("lacp") + " " + active + "×" + fmtSpeed(link.speed_mbps / active)
      : t("lacp");
    if (active < total) {
      label += " " + fmt("lagMembersShort", { active: active, total: total });
    }
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
  els.scanStatus.classList.remove("failed");
  els.scanStatus.title = "";
  if (isScanning) {
    els.scanStatus.textContent = t("scanning");
    return;
  }
  // A failed scan used to read exactly like a service that had just
  // started. It now says so, and keeps the map from the last good one.
  if (topology.last_error) {
    els.scanStatus.classList.add("failed");
    els.scanStatus.textContent =
      t("scanFailed") +
      new Date(topology.last_error_ts * 1000).toLocaleString(locale());
    els.scanStatus.title = topology.last_error;
    return;
  }
  els.scanStatus.textContent = topology.last_scan
    ? t("scanPrefix") + new Date(topology.last_scan * 1000).toLocaleString(locale())
    : t("noData");
}

/* ---------- data loading and rendering ---------- */

async function loadTopology() {
  const [topo, alarms] = await Promise.all([
    fetch("/api/topology").then((r) => r.json()),
    fetchAlarms("active=1"),
  ]);
  topology = topo;
  activeAlarms = alarms;
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

function li(main, sub, dotClass, onClick, searchText, cssClass) {
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
  if (cssClass) item.classList.add(cssClass);
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
        [h.ip, h.mac, h.random_mac ? t("randomMac") : "",
         h.vlan ? "VLAN " + h.vlan : ""]
          .filter(Boolean)
          .join(" · "),
        statusClass(h),
        () => focusNode(h.merged_into || "host:" + h.mac),
        // VLAN is intentionally excluded from search
        [hostLabel(h), h.ip, h.mac].filter(Boolean).join(" "),
        h.stale ? "stale" : ""
      )
    )
  );
  // devices nothing draws on the map: known from ARP or seen on a port
  // too long ago — searchable inventory, not graph nodes
  const unlocated = topology.unlocated || [];
  els.unlocatedList.replaceChildren(
    ...unlocated.map((h) =>
      li(
        hostLabel(h) + (h.monitored ? " ★" : ""),
        [h.ip, h.mac, h.random_mac ? t("randomMac") : ""]
          .filter(Boolean)
          .join(" · "),
        statusClass(h),
        () => showDetails("unloc:" + h.mac),
        [hostLabel(h), h.ip, h.mac].filter(Boolean).join(" "),
        "stale"
      )
    )
  );
  els.unlocatedSection.classList.toggle("hidden", unlocated.length === 0);
  els.unlocatedCount.textContent = unlocated.length;
  els.switchCount.textContent = topology.switches.length;
  els.hostCount.textContent = topology.hosts.length;
  applySearchFilter();
}

/* Caption style of a node; the selected one is brighter than the rest */
function nodeFont(id) {
  const selected = id === selectedNodeId;
  if (id.startsWith("sw:")) {
    return { color: colors.text, face: "system-ui" };
  }
  if (id.startsWith("host:")) {
    return {
      color: selected ? colors.text : colors.dim,
      size: 11,
      face: "ui-monospace",
      strokeWidth: 0,
    };
  }
  return { color: selected ? colors.text : colors.dim, size: 11 };
}

function buildGraphData() {
  const nodes = [];
  const edges = [];

  for (const sw of topology.switches) {
    // a switch_down alarm paints the node border red; the root bridge
    // of a working spanning tree gets a thicker outline
    const border = switchHasAlarm(sw.ip)
      ? colors.alarm
      : sw.stp_root
      ? colors.ok
      : colors.moon;
    nodes.push({
      id: "sw:" + sw.ip,
      label: sw.name + "\n" + sw.ip + (sw.stp_root ? "\n" + t("stpRootMark") : ""),
      shape: "box",
      color: {
        background: colors.panel,
        border: border,
        highlight: { background: "#1c2739", border: border },
      },
      font: nodeFont("sw:" + sw.ip),
      borderWidth: switchHasAlarm(sw.ip) || sw.stp_root ? 3 : 2,
      margin: 10,
    });
  }

  for (const link of topology.links) {
    const isLacp = link.lag && link.lag.count > 1;
    const isTrunk = link.lag && link.lag.trunk;
    // LLDP-confirmed links are drawn solid and at full strength: both
    // devices named each other, there is nothing being guessed at
    const confirmed = link.source === "lldp" || link.source === "both";
    const edge = {
      id: linkId(link),
      from: "sw:" + link.a,
      to: "sw:" + link.b,
      label: linkLabel(link),
      color: { color: colors.moon, opacity: confirmed ? 1 : 0.8 },
      width: isLacp ? 5 : isTrunk ? 4 : 3,
      font: { color: colors.dim, size: 11, strokeWidth: 0 },
      dashes: false,
    };
    // a port STP is holding in discarding carries no traffic at all
    if (link.stp_blocking) {
      edge.color = { color: colors.alarm, opacity: 1 };
      edge.dashes = [6, 4];
      edge.label = (edge.label ? edge.label + " · " : "") + t("stpBlocking");
    }
    edges.push(edge);
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
      font: nodeFont(ps.id),
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

  // switches LLDP found behind our ports that nobody polls: a named
  // node replaces the anonymous "switch without SNMP" on that port
  for (const bridge of topology.bridges || []) {
    // an unreachable bridge is worth seeing at a glance, the same way
    // an unreachable host is
    const border = bridge.ip
      ? bridge.ping_up
        ? colors.ok
        : colors.dim
      : colors.link;
    // the address under the name is the one the operator uses, not
    // whichever of the announced ones came back first
    const bridgeAddress = bridge.router_ip || bridge.ip || bridge.mgmt_ip;
    nodes.push({
      id: bridge.id,
      label: bridge.name + (bridgeAddress ? "\n" + bridgeAddress : ""),
      shape: "box",
      color: {
        background: "#1b2436",
        border: border,
        highlight: { background: "#26314a", border: colors.moon },
      },
      shapeProperties: { borderDashes: [5, 3] },
      borderWidth: 2,
      margin: 8,
      font: { color: colors.dim, size: 12 },
    });
    edges.push({
      id: "bredge:" + bridge.id,
      from: "sw:" + bridge.switch,
      to: bridge.id,
      dashes: [5, 3],
      color: { color: colors.link, opacity: 0.6 },
      width: 2,
    });
  }

  // one node per port that leaves the network: what is behind the
  // provider's handover is not ours to draw device by device
  for (const external of topology.external_networks || []) {
    nodes.push({
      id: external.id,
      label: t("externalNetwork") + " · " + external.count,
      shape: "hexagon",
      size: 16,
      color: {
        background: "#2b2438",
        border: colors.warn || "#d9a86b",
        highlight: { background: "#3a3049", border: colors.moon },
      },
      borderWidth: 2,
      font: nodeFont(external.id),
    });
    edges.push({
      id: "extedge:" + external.id,
      // the provider's switch is on the cable and everything else is
      // behind IT: mb0 -> CE6851 -> external network
      from: external.via || "sw:" + external.switch,
      to: external.id,
      color: { color: colors.warn || "#d9a86b", opacity: 0.7 },
      width: 3,
    });
  }

  // one node per port whose devices are all offline, so the switches
  // are not surrounded by a cloud of grey dots
  for (const group of topology.offline_groups || []) {
    nodes.push({
      id: group.id,
      label: t("offlineGroup") + " · " + group.count,
      shape: "square",
      size: 13,
      color: {
        background: "#2a2f3d",
        border: colors.dim,
        highlight: { background: "#3a4152", border: colors.moon },
      },
      shapeProperties: { borderDashes: [2, 3] },
      borderWidth: 2,
      font: nodeFont(group.id),
    });
    edges.push({
      id: "offedge:" + group.id,
      from: "sw:" + group.switch,
      to: group.id,
      dashes: [3, 3],
      color: { color: colors.dim, opacity: 0.4 },
      width: 1,
    });
  }

  for (const host of topology.hosts) {
    // this device IS one of the bridge nodes above — drawing it again
    // as a bare MAC beside itself is what v0.6.1 did
    if (host.merged_into) continue;
    const c = statusColor(host);
    // stale = drawn from the grace window, not from a fresh MAC table
    // a router is infrastructure, not a workstation: same status
    // colour, but a shape that is picked out at a glance
    const isRouter = deviceKind(host) === "router";
    // a router named by LLDP has no address of ours; the one in
    // `routers:` is how anybody reaches it, so it goes under the name
    const caption = hostLabel(host);
    const second =
      host.router_ip && host.router_ip !== caption ? "\n" + host.router_ip : "";
    nodes.push({
      id: "host:" + host.mac,
      label: caption + second,
      shape: isRouter ? "diamond" : "dot",
      size: isRouter ? 14 : 9,
      opacity: host.stale ? 0.4 : 1,
      color: {
        background: c,
        border: isRouter ? colors.moon : c,
        highlight: { background: c, border: colors.moon },
      },
      borderWidth: isRouter ? 2 : 1,
      font: nodeFont("host:" + host.mac),
    });
    edges.push({
      id: "hostedge:" + host.mac,
      from: host.via || "sw:" + host.switch,
      to: "host:" + host.mac,
      color: { color: colors.link, opacity: host.stale ? 0.15 : 0.35 },
      width: 1,
      // approximate = seen only through a trunk: the device is real,
      // the port it hangs on is a guess
      dashes: host.stale ? [3, 3] : host.approximate ? [2, 4] : false,
      // remembered ≠ approximate: the port is known, the sighting is
      // second-hand, so the edge stays solid
    });
  }

  return { nodes, edges };
}

/* ---------- label backdrop for the selected node ---------- */

/* Where the node's caption is drawn, in canvas coordinates. vis knows
   it exactly (the label was measured on the previous frame); the
   fallback measures the text under the node's bounding box. */
function labelBox(ctx, nodeId) {
  const node = network.body && network.body.nodes[nodeId];
  const measured = node && node.labelModule && node.labelModule.size;
  if (measured && measured.width) {
    return {
      x: measured.left,
      y: measured.top,
      w: measured.width,
      h: measured.height,
    };
  }
  const item = nodesDs.get(nodeId);
  const pos = network.getPositions([nodeId])[nodeId];
  if (!item || !pos) return null;
  const box = network.getBoundingBox(nodeId);
  const font = (item.font || {}).size || 14;
  ctx.font = font + "px " + ((item.font || {}).face || "system-ui");
  const lines = String(item.label || "").split("\n");
  const w = Math.max(...lines.map((line) => ctx.measureText(line).width));
  const h = lines.length * font * 1.25;
  return { x: pos.x - w / 2, y: box.bottom - h, w, h };
}

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) {
    ctx.roundRect(x, y, w, h, r);
    return;
  }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* Drawn from beforeDrawing, i.e. under the nodes and their captions */
function drawLabelBackdrop(ctx, nodeId, strong) {
  const box = labelBox(ctx, nodeId);
  if (!box || !box.w) return;
  const padX = 6;
  const padY = 4;
  ctx.save();
  ctx.globalAlpha = strong ? 0.85 : 0.5;
  ctx.fillStyle = colors.panel;
  roundedRect(ctx, box.x - padX, box.y - padY, box.w + padX * 2, box.h + padY * 2, 4);
  ctx.fill();
  if (strong) {
    ctx.globalAlpha = 1;
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  ctx.restore();
}

/* The selected caption is also brighter than the others */
function setSelectedNode(id) {
  const previous = selectedNodeId;
  selectedNodeId = id;
  const updates = [];
  for (const nodeId of [previous, id]) {
    if (nodeId && nodesDs && nodesDs.get(nodeId)) {
      updates.push({ id: nodeId, font: nodeFont(nodeId) });
    }
  }
  if (updates.length) nodesDs.update(updates);
  if (network) network.redraw();
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
      if (params.nodes.length) {
        setSelectedNode(params.nodes[0]);
        showDetails(params.nodes[0]);
      } else if (
        params.edges.length &&
        String(params.edges[0]).startsWith("link:")
      ) {
        setSelectedNode(null);
        showLinkDetails(params.edges[0]);
      } else {
        setSelectedNode(null);
        hideDetails();
      }
    });
    network.on("beforeDrawing", (ctx) => {
      if (hoveredNodeId && hoveredNodeId !== selectedNodeId) {
        drawLabelBackdrop(ctx, hoveredNodeId, false);
      }
      if (selectedNodeId) drawLabelBackdrop(ctx, selectedNodeId, true);
    });
    network.on("hoverNode", (params) => {
      hoveredNodeId = params.node;
      network.redraw();
    });
    network.on("blurNode", () => {
      hoveredNodeId = null;
      network.redraw();
    });
    applyFreeze();  // a freeze chosen earlier survives a reload
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
  setSelectedNode(id);
  showDetails(id);
}

/* ---------- detail cards ---------- */

/* Host behind a details id: "host:<mac>" on the map, "unloc:<mac>" off it */
function findHost(nodeId) {
  const mac = nodeId.slice(nodeId.indexOf(":") + 1);
  const list = nodeId.startsWith("unloc:")
    ? topology.unlocated || []
    : topology.hosts;
  return list.find((h) => h.mac === mac);
}

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
  } else if (nodeId.startsWith("offline:")) {
    const group = (topology.offline_groups || []).find((g) => g.id === nodeId);
    if (!group) return;
    const members = topology.hosts.filter((h) => h.via === nodeId);
    const rows = members
      .map(
        (h) => `<li data-mac="${h.mac}"><span>${hostLabel(h)}</span>
          <span class="sub">${fmtTime(h.last_seen)}</span></li>`
      )
      .join("");
    html = `<h3>${fmt("offlineGroupTitle", { n: group.count })}</h3>
      <p class="hint">${
        group.approximate ? t("offlineGroupGuessHint") : t("offlineGroupHint")
      }</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${group.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${group.port}</dd>
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(group.last_seen_max)}</dd>
      </dl><ul class="offline-list">${rows}</ul>`;
  } else if (nodeId.startsWith("bridge:")) {
    const bridge = (topology.bridges || []).find((b) => b.id === nodeId);
    if (!bridge) return;
    const caps = (bridge.capabilities || []).join(", ");
    // every address the device announced, the first one clickable
    const addresses = bridge.mgmt_ips && bridge.mgmt_ips.length
      ? bridge.mgmt_ips
      : bridge.mgmt_ip
      ? [bridge.mgmt_ip]
      : [];
    const link = addressList(addresses);
    // one hint, not several: "the devices behind this port hang off
    // this node", "they stay on the unmanaged switch" and "everything
    // behind here is somebody else's" cannot all be true at once
    const bridgeHint = bridge.external
      ? t("externalHint")
      : bridge.cap_assumed
      ? t("bridgeAssumedHint")
      : bridge.shares_port
      ? t("bridgeSharesPortHint")
      : t("bridgeHint");
    html = `<h3>${bridge.name}</h3>
      <p class="hint">${bridgeHint}</p>
      ${bridge.lldp_crowded && !bridge.shares_port
        ? `<p class="hint">${t("lldpCrowdedHint")}</p>` : ""}
      ${bridge.lldp_forwarded ? `<p class="hint">${t("lldpForwardedHint")}</p>` : ""}
      <dl>
      <dt>${t("descr")}</dt><dd>${bridge.sys_desc || "—"}</dd>
      <dt>${t("chassisId")}</dt><dd>${bridge.chassis_id}</dd>
      ${bridge.dns_name ? `<dt>${t("name")}</dt><dd>${bridge.dns_name}</dd>` : ""}
      <dt>${t("mgmtIp")}</dt><dd>${link}</dd>
      ${bridge.ip && !(bridge.mgmt_ips || []).includes(bridge.ip)
        ? `<dt>${t("ipAddr")}</dt><dd>${bridge.ip}</dd>` : ""}
      ${bridge.ip
        ? `<dt>${t("lastReply")}</dt><dd>${fmtTime(bridge.last_ping_ok)}</dd>`
        : ""}
      ${(bridge.also_ips || []).length
        ? `<dt>${t("sameDevice")}</dt><dd>${bridge.also_ips.join(", ")}</dd>`
        : ""}
      <dt>${t("switchLabel")}</dt><dd>${bridge.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${bridge.port}</dd>
      <dt>${t("remotePort")}</dt><dd>${bridge.remote_port || "—"}</dd>
      <dt>${t("capabilities")}</dt><dd>${
        bridge.cap_known ? `${t("kind_" + (bridge.kind || "other"))} (${caps})`
        : t("capsUnknown")
      }</dd>
      ${bridge.shares_port || bridge.external
        ? ""
        : `<dt>${t("devicesBehindPort")}</dt><dd>${bridge.host_count ?? 0}</dd>`}
      <dt>${t("firstSeen")}</dt><dd>${fmtTime(bridge.first_seen)}</dd>
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(bridge.last_seen)}</dd>
      </dl>`;
  } else if (nodeId.startsWith("external:")) {
    const external = (topology.external_networks || []).find(
      (e) => e.id === nodeId
    );
    if (!external) return;
    // the provider's own switch is drawn as its own node, not counted
    // among the addresses living behind it
    const members = topology.hosts.filter(
      (h) => h.via === nodeId && !h.merged_into
    );
    // addresses first: past the handover the address is all there is,
    // and the name column would just repeat it
    const rows = members
      .map(
        (h) => `<li data-mac="${h.mac}"><span>${h.ip || hostLabel(h)}</span>
          <span class="sub">${h.ip ? h.mac : ""}</span></li>`
      )
      .join("");
    html = `<h3>${t("externalNetwork")}</h3>
      <p class="hint">${t("externalHint")}</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${external.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${external.port}</dd>
      ${external.via_name
        ? `<dt>${t("behindBridge")}</dt><dd>${external.via_name}</dd>`
        : ""}
      <dt>${t("devicesBehindPort")}</dt><dd>${external.count}</dd>
      </dl><ul class="offline-list">${rows}</ul>`;
  } else if (nodeId.startsWith("pseudo:")) {
    const ps = (topology.pseudo_switches || []).find((p) => p.id === nodeId);
    if (!ps) return;
    html = `<h3>${t("pseudoTitle")}</h3>
      <p class="hint">${fmt("pseudoHint", { n: ps.host_count })}</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${ps.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${ps.port}</dd>
      <dt>${t("devicesBehindPort")}</dt><dd>${fmt("devicesCount", {
        n: ps.host_count,
        live: ps.host_count_live == null ? ps.host_count : ps.host_count_live,
      })}</dd></dl>`;
  } else {
    const host = findHost(nodeId);
    if (!host) return;
    const offMap = host.unlocated;
    const hint = offMap
      ? host.ip
        ? t("unlocatedHint")
        : t("offMapHint")
      : host.stale
      ? t("staleHint")
      : "";
    // a stale record whose address ARP still confirms is not a dead
    // host: the device most likely changed its MAC
    const aliveByIp = host.stale && !offMap && host.ping_up && host.ip_confirmed;
    html = `<h3>${hostLabel(host)}</h3>
      ${labelFromLldp(host) ? `<p class="hint">${t("nameFromLldpHint")}</p>` : ""}
      ${host.approximate ? `<p class="hint">${t("approximateHint")}</p>` : ""}
      ${host.remembered ? `<p class="hint">${t("rememberedHint")}</p>` : ""}
      ${aliveByIp ? `<p class="hint">${t("staleButAliveHint")}</p>` : ""}
      ${hint ? `<p class="hint">${hint}</p>` : ""}<dl>
      <dt>${t("name")}</dt><dd>${host.name || "—"}</dd>
      <dt>${t("ipAddr")}</dt><dd>${host.ip || "—"}</dd>
      <dt>${t("ipConfirmedLabel")}</dt><dd>${
        host.ip_confirmed ? fmtTime(host.ip_confirmed) : t("ipNotConfirmed")
      }</dd>
      <dt>${t("macAddr")}</dt><dd>${host.mac}${
        host.random_mac ? ` <span class="chip" title="${t("randomMacHint")}">${t("randomMac")}</span>` : ""
      }</dd>
      <dt>${t("switchLabel")}</dt><dd>${host.switch || "—"}</dd>
      <dt>${t("portLabel")}</dt><dd>${host.port || "—"}${
        host.approximate ? ` <span class="chip">${t("approximate")}</span>` : ""
      }${
        host.remembered ? ` <span class="chip">${t("remembered")}</span>` : ""
      }</dd>
      <dt>${t("vlan")}</dt><dd>${vlanLabel(host.vlan)}</dd>
      ${host.router_ip
        ? `<dt>${t("routerAddr")}</dt><dd>${addressLink(host.router_ip)}</dd>`
        : ""}
      ${host.lldp ? `<dt>${t("lldpLabel")}</dt><dd>${lldpHostLine(host.lldp)}</dd>` : ""}
      ${(host.lldp && (host.lldp.mgmt_ips || []).length > 1)
        ? `<dt>${t("mgmtIp")}</dt><dd>${addressList(host.lldp.mgmt_ips)}</dd>`
        : ""}
      <dt>${t("lastReply")}</dt><dd>${fmtTime(host.last_ping_ok)}</dd>
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(host.last_seen)}</dd>
      ${offMap ? `<dt>${t("lastArpLabel")}</dt><dd>${fmtTime(host.last_arp)}</dd>` : ""}
      <dt>${t("firstSeen")}</dt><dd>${fmtDate(host.first_seen)}</dd></dl>
      <button id="monitor-btn" class="panel-btn${host.monitored ? " active" : ""}">
        ${host.monitored ? "★" : "☆"} ${t("monitorBtn")}</button>`;
  }
  shownDetails = { type: "node", id: nodeId };
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
  closePorts();
  const portsBtn = document.getElementById("ports-btn");
  if (portsBtn) {
    portsBtn.addEventListener("click", () =>
      openPorts(nodeId.slice("sw:".length))
    );
  }
  for (const more of els.detailsBody.querySelectorAll(".addr-more")) {
    more.addEventListener("click", () => {
      const rest = more.nextElementSibling;
      if (rest) rest.classList.remove("hidden");
      more.remove();
    });
  }
  const monitorBtn = document.getElementById("monitor-btn");
  if (monitorBtn) {
    monitorBtn.addEventListener("click", () => toggleMonitor(nodeId));
  }
  // a device of the group: its node is always on the map
  for (const row of els.detailsBody.querySelectorAll(".offline-list li")) {
    row.addEventListener("click", () => focusNode("host:" + row.dataset.mac));
  }
}

/* "10.0.0.1 and 37 more" — a router announces one management address
   per VLAN interface, and all of them belong to the same device */
function fmtAddresses(addresses, limit) {
  const list = addresses || [];
  const keep = limit || 3;
  if (list.length <= keep) return list.join(", ");
  return (
    list.slice(0, keep).join(", ") +
    " " + fmt("andMore", { n: list.length - keep })
  );
}

/* A management address as a link to the device's web interface.
   IPv6 needs brackets in a URL, and a hostname that is not an address
   is left as plain text rather than turned into a broken link. */
function addressLink(address) {
  const text = String(address);
  const host = text.includes(":") ? "[" + text + "]" : text;
  const linkable = /^[0-9.]+$/.test(text) || text.includes(":");
  if (!linkable) return `<span>${text}</span>`;
  return `<a href="http://${host}" target="_blank" rel="noopener">${text}</a>`;
}

/* Management addresses as a column: the first few as links, the rest
   behind "and N more". A router announces one per VLAN interface, and
   38 of them run together into an unreadable wall. */
function addressList(addresses, limit) {
  const list = addresses || [];
  if (!list.length) return "—";
  const keep = limit || 3;
  const shown = list
    .slice(0, keep)
    .map((a) => `<li>${addressLink(a)}</li>`)
    .join("");
  if (list.length <= keep) return `<ul class="addr-list">${shown}</ul>`;
  const rest = list
    .slice(keep)
    .map((a) => `<li>${addressLink(a)}</li>`)
    .join("");
  return (
    `<ul class="addr-list">${shown}</ul>` +
    `<button type="button" class="addr-more">` +
    fmt("andMore", { n: list.length - keep }) +
    `</button><ul class="addr-list addr-rest hidden">${rest}</ul>`
  );
}

/* What LLDP says about a device that is already a host on the map:
   the switch heard it on this very port, so the data belongs here
   rather than on a node of its own */
function lldpHostLine(lldp) {
  const kind = lldp.cap_known ? t("kind_" + (lldp.kind || "other")) : "";
  const parts = [
    lldp.sys_name,
    kind,
    lldp.port_id ? t("portLabel") + " " + lldp.port_id : "",
    (lldp.mgmt_ips || []).length === 1 ? lldp.mgmt_ips[0] : "",
    lldp.sys_desc,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : t("lldpUnidentified");
}

/* Flip the host_down alarm flag of a host and re-render */
async function toggleMonitor(nodeId) {
  const host = findHost(nodeId);
  if (!host) return;
  const res = await fetch("/api/host/" + encodeURIComponent(host.mac), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ monitored: !host.monitored }),
  });
  if (!res.ok) return;
  host.monitored = (await res.json()).monitored;
  renderSidebar();
  showDetails(nodeId);
}

function hideDetails() {
  els.details.classList.add("hidden");
  shownDetails = null;
}

/* ---------- ports panel ---------- */

async function openPorts(ip, highlightPort) {
  portsIp = ip;
  portsHighlight = highlightPort || null;
  hideDetails();
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
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

/* "Gi0/2" before "Gi0/10": compare digit runs as numbers */
function naturalCompare(a, b) {
  const chunks = (s) => String(s).match(/\d+|\D+/g) || [];
  const left = chunks(a);
  const right = chunks(b);
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const x = left[i];
    const y = right[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    const nx = parseInt(x, 10);
    const ny = parseInt(y, 10);
    if (!isNaN(nx) && !isNaN(ny)) {
      if (nx !== ny) return nx - ny;
    } else if (x !== y) {
      return x < y ? -1 : 1;
    }
  }
  return 0;
}

/* Column the user picked; without one the server order stands
   (active ports first, then by port number) */
function sortPorts(ports) {
  if (!portsSort) return ports;
  const value = {
    speed: (p) => p.speed_mbps,
    in: (p) => p.in_mbps,
    out: (p) => p.out_mbps,
    err: (p) => p.errors_per_min,
    disc: (p) => p.discards_per_min,
  }[portsSort.key];
  const compare = value
    ? (a, b) => (value(a) ?? -1) - (value(b) ?? -1)
    : (a, b) => naturalCompare(a.name, b.name);
  // the direction goes into the comparator, not a reverse() afterwards,
  // so ports with equal values keep their natural order
  return [...ports].sort((a, b) => portsSort.dir * compare(a, b));
}

function setPortsSort(key) {
  // a second click on the same column flips the direction; numbers
  // start with the largest, names with the first port
  const dir = portsSort && portsSort.key === key ? -portsSort.dir
    : key === "name" ? 1 : -1;
  portsSort = { key, dir };
  for (const th of document.querySelectorAll("#ports th[data-sort]")) {
    th.classList.toggle("sorted-asc", th.dataset.sort === key && dir > 0);
    th.classList.toggle("sorted-desc", th.dataset.sort === key && dir < 0);
  }
  if (lastPorts) renderPorts(lastPorts);
}

/* A column the agent never answered for is a column of dashes that
   means "unknown", not "zero". The header says which, and why. */
function markSilentColumns(columns) {
  for (const th of document.querySelectorAll("#ports th[data-sort]")) {
    const info = (columns || {})[th.dataset.sort];
    const silent = info && !info.answered;
    // answered, but not for every port — the ports at the end of the
    // table are the ones that go missing
    const partial = info && info.answered && info.partial;
    th.classList.toggle("no-answer", !!silent);
    th.classList.toggle("partial", !!partial);
    const base =
      th.dataset.sort === "err"
        ? t("errTooltip")
        : th.dataset.sort === "disc"
        ? t("discTooltip")
        : "";
    if (!silent && !partial) {
      th.title = base;
      continue;
    }
    const head = silent
      ? fmt("colNoAnswer", { oids: (info.oids || []).join(", ") })
      : fmt("colPartial", {
          oids: (info.oids || []).join(", "),
          filled: info.filled || 0,
        });
    th.title =
      head +
      (info.error ? "\n" + info.error : "") +
      (base ? "\n\n" + base : "");
  }
}

function renderPorts(data) {
  els.portsTitle.textContent = fmt("portsTitle", {
    name: data.name || data.switch,
  });
  markSilentColumns(data.columns);
  const limits = data.thresholds || {};
  let highlighted = null;
  // physical ports only; the server puts active ones first
  const rows = sortPorts(data.ports.filter((p) => p.is_physical)).map((p) => {
      const tr = document.createElement("tr");
      if (!p.oper_up) tr.className = "port-down";
      if (portsHighlight && p.name === portsHighlight) {
        tr.classList.add("highlight");
        highlighted = tr;
      }
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
      const nameCell = document.createElement("td");
      nameCell.textContent = name;
      // lldpLocPortDesc: what the operator called this port on the
      // switch itself ("Library", "403 audit")
      if (p.uplink_hint) {
        const chip = document.createElement("span");
        chip.className = "chip warn";
        chip.textContent = t("uplinkHintChip");
        chip.title =
          fmt("uplinkHint", { switch: data.switch, port: p.name }) +
          "\n\n" +
          fmt("uplinkWhy_" + p.uplink_hint.kind, {
            ip: p.uplink_hint.ip,
            name: p.uplink_hint.name,
          });
        nameCell.append(" ", chip);
      }
      if (p.external) {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = t("externalPort");
        chip.title = t("externalHint");
        nameCell.append(" ", chip);
      }
      if (p.label) {
        const label = document.createElement("span");
        label.className = "port-label";
        label.textContent = p.label;
        label.title = t("portLabelName") + "\n" + p.label;
        nameCell.append(" ", label);
      }
      // addresses this port's damaged frames invented, with the real
      // one each of them is a distortion of
      if (p.flaps) {
        const chip = document.createElement("span");
        chip.className = "chip warn";
        chip.textContent = "⇅ " + p.flaps;
        chip.title = fmt("portFlappingHint", {
          n: p.flaps,
          when: p.flaps_last ? fmtTime(p.flaps_last) : "—",
        });
        nameCell.append(" ", chip);
      }
      const neighbours = p.lldp || [];
      if (neighbours.length) {
        const chip = document.createElement("span");
        chip.className = "chip" + (p.lldp_forwarded ? " warn" : "");
        const first = neighbours[0];
        chip.textContent =
          "⇄ " + (first.sys_name || first.chassis_id) +
          (neighbours.length > 1 ? " +" + (neighbours.length - 1) : "");
        chip.title =
          neighbours
            .map((n) =>
              [
                t("lldpNeighbour") + ": " + (n.sys_name || n.chassis_id),
                n.port_id ? t("portLabel") + " " + n.port_id : "",
                fmtAddresses(n.mgmt_ips),
                n.cap_known
                  ? (n.capabilities || []).join(", ")
                  : t("lldpUnidentified"),
                n.sys_desc,
              ]
                .filter(Boolean)
                .join(" · ")
            )
            .join("\n") +
          (p.lldp_forwarded
            ? "\n\n" + t("lldpForwardedHint")
            : p.lldp_crowded
            ? "\n\n" + t("lldpCrowdedHint")
            : "");
        nameCell.append(" ", chip);
      }
      const suspect = p.suspect_macs || [];
      if (suspect.length) {
        const chip = document.createElement("span");
        chip.className = "chip warn";
        chip.textContent = "⚠ " + suspect.length;
        chip.title =
          fmt("suspectMacs", { n: suspect.length }) + "\n" +
          t("suspectMacsHint") + "\n" +
          suspect
            .map((s) => s.mac + " ← " + s.sample + " (" + s.distance + ")")
            .join("\n");
        nameCell.append(" ", chip);
      }
      tr.append(nameCell);
      const dot = document.createElement("span");
      dot.className = "dot " + (p.oper_up ? "up" : "down");
      td(dot);
      td(p.oper_up && p.speed_mbps ? fmtSpeed(p.speed_mbps) : "—");
      td(fmtRate(p.in_mbps), "num");
      td(fmtRate(p.out_mbps), "num");
      // damaged frames are a fault, discards are usually filtering
      const overErr = p.errors_per_min > (limits.errors_per_minute ?? Infinity);
      const overDisc =
        p.discards_per_min > (limits.discards_per_minute ?? Infinity);
      td(fmtRate(p.errors_per_min), "num" + (overErr ? " over-error" : ""));
      td(fmtRate(p.discards_per_min), "num" + (overDisc ? " over-discard" : ""));
      return tr;
    });
  els.portsBody.replaceChildren(...rows);
  if (highlighted) highlighted.scrollIntoView({ block: "center" });
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
    // each member with its oper state: green = up, grey = down
    const memberList = (members, states) =>
      members
        .map((m, i) => {
          const up = !states || states[i] == null || states[i];
          return `<span class="dot ${up ? "up" : "down"} inline-dot"></span>${m}`;
        })
        .join(", ") + " (" + t("lacp") + ")";
    if ((link.lag.a_members || []).length)
      html += `<dt>${fmt("portsOf", { name: swName(link.a) })}</dt><dd>${memberList(link.lag.a_members, link.lag.a_states)}</dd>`;
    if ((link.lag.b_members || []).length)
      html += `<dt>${fmt("portsOf", { name: swName(link.b) })}</dt><dd>${memberList(link.lag.b_members, link.lag.b_states)}</dd>`;
  }
  const source = link.source || "fdb";
  const sourceLabel =
    source === "both" ? t("srcBoth") : source === "lldp" ? t("srcLldp") : t("srcFdb");
  const sourceHint = source === "fdb" ? t("srcHintFdb") : t("srcHintLldp");
  html += `<dt>${t("linkSource")}</dt><dd title="${sourceHint}">${sourceLabel}</dd>`;
  html += "</dl>";
  if (link.stp_blocking) {
    html += `<p class="hint">${t("stpBlockingHint")}</p>`;
  }
  shownDetails = { type: "link", id: edgeId };
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
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

/* One alarms request; also picks up the flap window for the tooltip */
async function fetchAlarms(query) {
  const data = await (await fetch("/api/alarms?" + query)).json();
  if (data.flap_window_hours) flapWindowHours = data.flap_window_hours;
  return data.alarms || [];
}

/* Reloads both lists and repaints the panel and the badge */
async function refreshAlarms() {
  const [active, cleared] = await Promise.all([
    fetchAlarms("active=1"),
    fetchAlarms("active=0&limit=50"),
  ]);
  lastAlarms = { active, cleared };
  activeAlarms = active;
  renderBadge();
  renderAlarms();
}

async function toggleAlarms() {
  if (!els.alarms.classList.contains("hidden")) {
    els.alarms.classList.add("hidden");
    return;
  }
  await refreshAlarms();
  hideDetails();
  els.journal.classList.add("hidden");
  els.stp.classList.add("hidden");
  closePorts();
  els.alarms.classList.remove("hidden");
}

/* Manually clear one active alarm (with confirmation), then refresh */
async function clearAlarm(id) {
  if (!confirm(t("clearConfirm"))) return;
  const res = await fetch("/api/alarms/" + id + "/clear", { method: "POST" });
  if (!res.ok) return;
  await refreshAlarms();
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
      // head: kind of alarm, the FLAP mark and the clear button
      const head = document.createElement("div");
      head.className = "alarm-head";
      const sev = document.createElement("span");
      sev.className = "sev sev-" + a.severity;
      sev.textContent = t("al_" + a.type);
      head.append(sev);
      if (a.flapping) {
        const flap = document.createElement("span");
        flap.className = "flap-chip";
        flap.textContent = t("flapChip");
        flap.title = fmt("flapTooltip", {
          n: a.raise_count,
          h: flapWindowHours,
        });
        const since = document.createElement("span");
        since.className = "flap-since";
        since.textContent = t("lastRaiseLabel") + " " + fmtTime(a.last_raise);
        head.append(flap, since);
      }
      if (isActive) {
        const clear = document.createElement("button");
        clear.className = "alarm-clear";
        clear.textContent = "✕ " + t("clearBtn");
        clear.addEventListener("click", () => clearAlarm(a.id));
        head.append(clear);
      }
      // device on its own line, wrapped in full — never truncated
      const subject = document.createElement("div");
      subject.className = "alarm-subject";
      subject.textContent = a.display || a.subject;
      li.append(head, subject);
      if (a.port) {
        const port = document.createElement("div");
        port.className = "alarm-port";
        port.textContent = t("portLabel") + ": " + a.port;
        li.append(port);
      }
      // counter alarms: jump straight to the port they are about
      if (
        a.switch_ip &&
        (a.type === "port_errors" ||
          a.type === "port_discards" ||
          a.type === "port_frame_corruption")
      ) {
        const link = document.createElement("button");
        link.className = "alarm-link";
        link.textContent = t("openPortsLink");
        link.addEventListener("click", () => openPorts(a.switch_ip, a.port));
        li.append(link);
      }
      const sub = document.createElement("div");
      sub.className = "alarm-sub";
      sub.textContent = isActive
        ? fmtTime(a.ts_raised) + " · " + fmtDuration(now - a.ts_raised)
        : fmtTime(a.ts_raised) + " · " + t("clearedAt") + fmtTime(a.ts_cleared);
      li.append(sub);
      ul.append(li);
    }
    body.append(ul);
  };
  section("alarmsActive", lastAlarms.active, true);
  section("alarmsCleared", lastAlarms.cleared, false);
}

/* ---------- STP panel ---------- */

/* "1 h 12 m", or "—" when the switch never reported a change */
function fmtSince(seconds) {
  if (seconds == null) return "—";
  return fmtDuration(seconds);
}

function stpVerdictText(v) {
  if (!v || v.verdict === "not_operating") return t("stpVerdictNone");
  if (v.verdict === "single") {
    const root = Object.keys(v.roots)[0] || "";
    return fmt("stpVerdictSingle", { root: root });
  }
  return fmt("stpVerdictFragmented", { n: Object.keys(v.roots).length });
}

function renderStp() {
  const body = els.stpBody;
  body.replaceChildren();
  const data = lastStp || { verdict: null, switches: [] };
  const verdict = document.createElement("p");
  verdict.className =
    "stp-verdict" +
    (!data.verdict || data.verdict.verdict === "not_operating"
      ? " none"
      : data.verdict.verdict === "fragmented"
      ? " bad"
      : " ok");
  verdict.textContent = stpVerdictText(data.verdict);
  body.append(verdict);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = t("stpHint");
  body.append(hint);

  if (!data.switches.length) {
    const empty = document.createElement("p");
    empty.className = "no-alarms";
    empty.textContent = t("noData");
    body.append(empty);
    return;
  }
  const table = document.createElement("table");
  table.className = "ports-table stp-table";
  const head = document.createElement("tr");
  for (const key of [
    "switchLabel", "stpState", "stpPriority", "stpRoot", "stpCost",
    "stpRootPort", "stpChanges", "stpLastChange",
  ]) {
    const th = document.createElement("th");
    th.textContent = t(key);
    head.append(th);
  }
  const thead = document.createElement("thead");
  thead.append(head);
  table.append(thead);
  const tbody = document.createElement("tbody");
  for (const sw of data.switches) {
    const tr = document.createElement("tr");
    const cells = sw.operating
      ? [
          sw.name,
          sw.is_root ? t("stpStateRoot") : t("stpStateMember"),
          String(sw.priority),
          sw.designated_root || "—",
          String(sw.root_cost),
          sw.root_port || "—",
          String(sw.top_changes),
          fmtSince(sw.time_since_change),
        ]
      : // root, cost and root port are meaningless here and are shown
        // as such rather than as the zeros the switch reports
        [sw.name, t("stpStateOff"), "—", "—", "—", "—", "—", "—"];
    cells.forEach((value, i) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (i === 1 && !sw.operating) {
        td.className = "stp-off";
        td.title = sw.reason || "";
      }
      if (i === 1 && sw.is_root) td.className = "stp-root";
      tr.append(td);
    });
    tbody.append(tr);
    if (sw.blocking_ports && sw.blocking_ports.length) {
      const note = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "stp-blocking";
      td.textContent =
        t("stpBlocking") + ": " + sw.blocking_ports.join(", ");
      note.append(td);
      tbody.append(note);
    }
  }
  table.append(tbody);
  body.append(table);
}

async function toggleStp() {
  if (!els.stp.classList.contains("hidden")) {
    els.stp.classList.add("hidden");
    return;
  }
  lastStp = await (await fetch("/api/stp")).json();
  renderStp();
  hideDetails();
  closePorts();
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  els.stp.classList.remove("hidden");
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
  els.stp.classList.add("hidden");
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
for (const th of document.querySelectorAll("#ports th[data-sort]")) {
  th.addEventListener("click", () => setPortsSort(th.dataset.sort));
}
els.freezeBtn.addEventListener("click", toggleFreeze);
els.alarmsBtn.addEventListener("click", toggleAlarms);
els.alarmsClose.addEventListener("click", () =>
  els.alarms.classList.add("hidden")
);
els.stpBtn.addEventListener("click", toggleStp);
els.stpClose.addEventListener("click", () => els.stp.classList.add("hidden"));
els.langRu.addEventListener("click", () => setLang("ru"));
els.langEn.addEventListener("click", () => setLang("en"));

applyStatic();
loadTopology();
setInterval(loadTopology, REFRESH_MS);
