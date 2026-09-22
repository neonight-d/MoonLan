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
  detailsTitle: document.getElementById("details-title"),
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
  arrangeBtn: document.getElementById("arrange-btn"),
  nodeMenu: document.getElementById("node-menu"),
  layoutStatus: document.getElementById("layout-status"),
  resetLayoutBtn: document.getElementById("reset-layout-btn"),
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
  applyArrangeMode();
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

/* ---------- saved layout ----------

   Where the nodes sit is kept on the server, not in this browser. A
   map of a network is a shared object: two people looking at one
   network have to see one picture, or "the switch at the bottom left"
   stops meaning anything. The language and the freeze toggle are
   personal settings and stay in localStorage; coordinates are not.

   `placed` remembers which nodes have already been given their saved
   position in this session. An unpinned node is started there and then
   left to the physics engine — re-applying the coordinates on every
   thirty-second refresh would drag it back and fight the layout. */
let savedLayout = {};
let layoutSavedAt = 0;
const placed = new Set();
// How long to let the layout settle before writing down where a new
// node ended up
const AUTOSAVE_SETTLE_MS = 4000;

async function loadLayout() {
  try {
    const data = await (await fetch("/api/layout")).json();
    savedLayout = data.nodes || {};
    layoutSavedAt = data.saved_at || 0;
  } catch (e) {
    // no saved layout is not an error: the map lays itself out
    savedLayout = {};
    layoutSavedAt = 0;
  }
}

/* The saved position of a node, if it has one and has not been given
   it already. A pinned node keeps being told where it is: it is out of
   the physics engine, so nothing else would hold it there. */
function layoutFor(id) {
  const pos = savedLayout[id];
  if (!pos) return null;
  const first = !placed.has(id);
  placed.add(id);
  if (pos.pinned) {
    return { x: pos.x, y: pos.y, fixed: { x: true, y: true } };
  }
  return first ? { x: pos.x, y: pos.y } : null;
}

/* Applied to every node as it is built: where it goes, whether the
   physics engine may move it, and whether to say a person put it
   there. */
function applyLayout(node) {
  const pinned = isPinned(node.id);
  const pos = layoutFor(node.id);
  if (pos) Object.assign(node, pos);
  // Set on EVERY render, including to its off state. vis merges an
  // update field by field, so a property left out keeps whatever it
  // had — and a node just released would go on being held.
  node.fixed = pinned ? { x: true, y: true } : { x: false, y: false };
  if (pinned) node.label = (node.label || "") + " " + t("pinnedMark");
  return node;
}

/* Records where the nodes nobody has placed yet have ended up.

   There is no "save layout" button any more, and there should not be
   one: a button that has to be pressed for the picture to survive is
   a button somebody will forget, and then the map they arranged is
   gone. A node that has no saved position gets one as soon as the
   layout settles; a node that already has one is never touched here,
   or every open browser would rewrite the shared picture on every
   refresh.

   `autoSaved` keeps one client from sending the same node twice while
   the answer is still in flight. */
const autoSaved = new Set();
let autoSaveTimer = null;

async function saveNewPositions() {
  if (!network) return;
  const positions = network.getPositions();
  const fresh = {};
  for (const id of Object.keys(positions)) {
    if (savedLayout[id] || autoSaved.has(id)) continue;
    fresh[id] = { x: positions[id].x, y: positions[id].y, pinned: false };
  }
  const ids = Object.keys(fresh);
  if (!ids.length) return;
  for (const id of ids) autoSaved.add(id);
  try {
    await fetch("/api/layout", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nodes: fresh }),
    });
  } catch (e) {
    for (const id of ids) autoSaved.delete(id);
    serviceLost("saving the layout");
    return;
  }
  serviceBack();
  for (const id of ids) savedLayout[id] = fresh[id];
  if (!layoutSavedAt) layoutSavedAt = Date.now() / 1000;
  updateScanStatus();
}

/* The physics engine emits `stabilized` when it settles, which is the
   right moment — but with the simulation running continuously it may
   not come for a while, and a node added by a scan should not have to
   wait for it. So a short timer as well, and one idempotent function
   behind both. */
function scheduleAutoSave() {
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(saveNewPositions, AUTOSAVE_SETTLE_MS);
}

/* Pins one or more nodes where they are, and remembers it.

   Pinning says "the physics engine does not get to move this", and
   nothing more. A pinned node is still draggable: asking somebody to
   release a switch before nudging it a centimetre would be a rule
   about our bookkeeping, not about their map. */
async function pinNodes(positions) {
  const ids = Object.keys(positions);
  if (!ids.length) return;
  for (const id of ids) {
    savedLayout[id] = {
      x: positions[id].x, y: positions[id].y, pinned: true,
    };
  }
  try {
    await Promise.all(
      ids.map((id) =>
        fetch("/api/layout/" + layoutPath(id), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            x: positions[id].x, y: positions[id].y, pinned: true,
          }),
        })
      )
    );
  } catch (e) {
    serviceLost("pinning a node");
    return;
  }
  serviceBack();
  if (!layoutSavedAt) layoutSavedAt = Date.now() / 1000;
  renderGraph();
  updateScanStatus();
}

function pinNode(id, x, y) {
  return pinNodes({ [id]: { x: x, y: y } });
}

/* Releases nodes back to the physics engine. No confirmation: this is
   cheap and reversible in one gesture, and a dialog in front of it
   only makes the cheap thing feel expensive. */
async function unpinNodes(ids) {
  if (!ids.length) return;
  for (const id of ids) {
    delete savedLayout[id];
    placed.delete(id);
  }
  try {
    await Promise.all(
      ids.map((id) =>
        fetch("/api/layout/" + layoutPath(id), { method: "DELETE" })
      )
    );
  } catch (e) {
    serviceLost("releasing a node");
    return;
  }
  serviceBack();
  await loadLayout();
  renderGraph();
  updateScanStatus();
}

function unpinNode(id) {
  return unpinNodes([id]);
}

function isPinned(id) {
  return !!(savedLayout[id] && savedLayout[id].pinned);
}

/* Node ids carry colons and, in four of the seven kinds, a port name
   with a slash in it ("pseudo:10.0.0.21:Gi0/3"). The slash is part of
   the id and the route reads it with a path converter, so it must
   survive encoding; everything else must not. */
function layoutPath(id) {
  return encodeURIComponent(id).replace(/%2F/g, "/");
}

async function resetLayout() {
  if (!window.confirm(t("resetLayoutConfirm"))) return;
  try {
    await fetch("/api/layout", { method: "DELETE" });
  } catch (e) {
    serviceLost("clearing the layout");
    return;
  }
  serviceBack();
  savedLayout = {};
  layoutSavedAt = 0;
  placed.clear();
  autoSaved.clear();
  // Forgetting the positions on the server is only half of it. vis
  // keeps x and y on the node it has already built, and nothing in
  // the update path can take them away again — `setOptions` assigns a
  // coordinate only when one is given, so leaving it out means "keep
  // what you have". The nodes went on standing exactly where they
  // were, `stabilize()` restarted the physics from those same points,
  // and the reset looked like it had done nothing at all until the
  // page was reloaded.
  //
  // So the node set is rebuilt rather than updated: that is what
  // makes vis drop the old bodies and lay the map out afresh.
  rebuildGraph();
  updateScanStatus();
  if (network) network.stabilize();
}

/* Who a node hangs off, taken from the edges that were just built.

   Every node but the root has exactly one edge coming into it from
   the thing it belongs to — a host from its switch or from the group
   node standing in for one, a group from its switch, a switch from
   its parent in the tree. Reading it off the edges rather than
   re-deriving it per node kind means the two can never disagree.  */
function anchorMap(edges) {
  const anchors = new Map();
  for (const edge of edges) {
    if (!anchors.has(edge.to)) anchors.set(edge.to, edge.from);
  }
  return anchors;
}

/* The same map, kept from the last render: the selection needs to
   know what hangs off a container, and re-deriving it from the
   topology would be a second set of rules that can disagree with the
   edges actually drawn. */
let lastAnchors = new Map();

/* Everything hanging off one container — a switch, a switch without
   SNMP, a "beyond the trunk" or "offline" group — following the
   chain down through further containers.

   Switches are left out on purpose: this is "take what is on this
   box", not "take this branch". Selecting a switch's whole subtree
   would move half the map on the first drag.  */
function devicesUnder(id) {
  const children = new Map();
  for (const [child, anchor] of lastAnchors) {
    if (!children.has(anchor)) children.set(anchor, []);
    children.get(anchor).push(child);
  }
  const found = new Set();
  const stack = [id];
  while (stack.length) {
    for (const child of children.get(stack.pop()) || []) {
      if (found.has(child) || child.startsWith("sw:")) continue;
      found.add(child);
      stack.push(child);
    }
  }
  return [...found];
}

/* A number from a string, so a node's offset is the same in every
   browser and does not jump between renders. Math.random() would put
   the same device in a different spot for each person looking. */
function idHash(id) {
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 31 + id.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

/* Gives a position to every node that has none.

   A node nobody has placed yet used to be dropped wherever vis felt
   like putting it, which on a fresh map or straight after a reset
   means a scattering of boxes with no relation to the network. A new
   device belongs next to the thing it is plugged into, and the
   physics engine can take it from there. */
function seedPositions(nodes, edges) {
  const anchors = anchorMap(edges);
  lastAnchors = anchors;
  const known = new Map();
  const existing = network ? network.getPositions() : {};
  for (const node of nodes) {
    if (node.x != null && node.y != null) {
      known.set(node.id, { x: node.x, y: node.y });
    } else if (existing[node.id]) {
      known.set(node.id, existing[node.id]);
    }
  }
  // A node that hangs off nothing — the root of the tree, or a
  // switch nobody could link — anchors the rest. Without it a first
  // load has no starting point at all and every chain below stays
  // unplaced.
  for (const node of nodes) {
    if (known.has(node.id) || anchors.has(node.id)) continue;
    const hash = idHash(node.id);
    node.x = (hash % 400) - 200;
    node.y = ((hash >> 9) % 400) - 200;
    known.set(node.id, { x: node.x, y: node.y });
  }
  // A chain — switch, then the group on it, then the hosts in the
  // group — needs a pass per level. Four covers every shape the map
  // draws, and the loop stops as soon as a pass places nothing.
  for (let pass = 0; pass < 6; pass++) {
    let placedAny = false;
    for (const node of nodes) {
      if (known.has(node.id)) continue;
      const at = known.get(anchors.get(node.id));
      if (!at) continue;
      const hash = idHash(node.id);
      const angle = ((hash % 360) * Math.PI) / 180;
      const radius = 70 + (hash % 50);
      node.x = at.x + Math.cos(angle) * radius;
      node.y = at.y + Math.sin(angle) * radius;
      known.set(node.id, { x: node.x, y: node.y });
      placedAny = true;
    }
    if (!placedAny) break;
  }
  return nodes;
}

/* Throws the drawn nodes away and builds them again from the data.

   `renderGraph` updates in place on purpose — it runs every thirty
   seconds and must not disturb the camera or the positions. This is
   the opposite operation, and it exists for the one case that needs
   it. */
function rebuildGraph() {
  if (!network) {
    renderGraph();
    return;
  }
  const { nodes, edges } = buildGraphData();
  seedPositions(nodes, edges);
  nodesDs.clear();
  edgesDs.clear();
  nodesDs.add(nodes);
  edgesDs.add(edges);
  scheduleAutoSave();
}

/* ---------- context menu ----------

   The items are data, not markup. This version has three of them; the
   next one adds diagnostic actions (ping, traceroute, a link to the
   device's own web interface) and whatever commands the operator puts
   in config.yaml, and none of that should mean rewriting the menu.

   Every item says what it is called, when it applies, and what it
   does — and it is handed the whole selection, because a menu that
   works on one node when three are chosen is a menu that surprises
   people. */
function menuItemsFor(ids) {
  const loose = ids.filter((id) => !isPinned(id));
  return [
    {
      key: "pin",
      label: loose.length ? t("menuPin") : t("menuUnpin"),
      applies: () => ids.length > 0,
      run: () => togglePinOnSelection(),
    },
    {
      key: "card",
      label: t("menuOpenCard"),
      // one node, one card: there is nothing to show for a crowd
      applies: () => ids.length === 1,
      run: () => {
        setSelectedNode(ids[0]);
        showDetails(ids[0]);
      },
    },
    {
      key: "ports",
      label: t("menuOpenPorts"),
      applies: () => ids.length === 1 && ids[0].startsWith("sw:"),
      run: () => openPorts(ids[0].slice("sw:".length)),
    },
  ];
}

function closeNodeMenu() {
  els.nodeMenu.classList.add("hidden");
  els.nodeMenu.replaceChildren();
}

function openNodeMenu(ids, at) {
  const items = menuItemsFor(ids).filter((item) => item.applies());
  if (!items.length) {
    closeNodeMenu();
    return;
  }
  const parts = [];
  const head = document.createElement("div");
  head.className = "context-menu-head";
  head.textContent =
    ids.length === 1 ? nodeTitle(ids[0]) : fmt("menuSelected", { n: ids.length });
  parts.push(head);
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.key = item.key;
    button.textContent = item.label;
    button.addEventListener("click", () => {
      closeNodeMenu();
      item.run();
    });
    parts.push(button);
  }
  els.nodeMenu.replaceChildren(...parts);
  els.nodeMenu.classList.remove("hidden");
  // Keep it on screen: near the bottom or the right edge it flips
  const box = els.nodeMenu.getBoundingClientRect();
  const x = Math.min(at.x, window.innerWidth - box.width - 8);
  const y = Math.min(at.y, window.innerHeight - box.height - 8);
  els.nodeMenu.style.left = Math.max(4, x) + "px";
  els.nodeMenu.style.top = Math.max(4, y) + "px";
}

/* ---------- arrange mode ----------

   Dragging a node is how people look at a map: pull the cloud of hosts
   aside, lift a switch out of the tangle, see what is behind it. v0.7
   read every one of those as a decision and pinned the node for good,
   so the map slowly set like concrete without anyone choosing it.

   Placing a node is a different act from looking at one, and it now
   has a mode of its own. Deliberately not remembered between
   sessions: a mode that comes back on its own after a reload is the
   same trap in another shape. */
let arrangeMode = false;

function applyArrangeMode() {
  els.arrangeBtn.classList.toggle("active", arrangeMode);
  els.arrangeBtn.textContent = arrangeMode
    ? t("arrangeOnBtn")
    : t("arrangeBtn");
  els.arrangeBtn.title = t("arrangeHint");
  // The map itself changes, not only the button: a mode you can
  // forget you are in is a mode that edits the map by accident.
  els.network.classList.toggle("arranging", arrangeMode);
}

function toggleArrangeMode() {
  arrangeMode = !arrangeMode;
  applyArrangeMode();
}

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

/* "4 min", "2 h 10 min" — how long ago something was measured. */
function fmtAge(seconds) {
  if (seconds == null) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return t("ageUnderMinute");
  if (minutes < 60) return fmt("ageMinutes", { n: minutes });
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest
    ? fmt("ageHoursMinutes", { h: hours, n: rest })
    : fmt("ageHours", { h: hours });
}

function updateScanStatus() {
  els.scanStatus.classList.remove("failed");
  els.scanStatus.classList.remove("partial");
  els.scanStatus.classList.remove("offline");
  els.scanStatus.title = "";
  // Nothing below this line is arriving any more, so it goes first:
  // everything else the header could say is about a picture that has
  // stopped being refreshed.
  if (!serviceReachable) {
    els.scanStatus.classList.add("offline");
    els.scanStatus.textContent = fmt("serviceOffline", {
      time: topology.last_scan ? fmtTime(topology.last_scan) : t("noData"),
    });
    els.scanStatus.title = t("serviceOfflineHint");
    return;
  }
  // A scan of eight switches took ten minutes and the interface said
  // nothing at all about it — the only way to find out whether
  // anything was happening was journalctl. One line settles it.
  if (isScanning || topology.scanning) {
    els.scanStatus.textContent =
      topology.scan_total > 0
        ? fmt("scanningProgress", {
            done: topology.scan_done || 0,
            total: topology.scan_total,
          })
        : t("scanning");
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
  // Switches the last scan gave up waiting for. Worth seeing — their
  // part of the map is older than the rest — but not an alarm: they
  // answer, and nothing about them is being claimed.
  const late = topology.scan_over_budget || [];
  if (late.length) {
    els.scanStatus.classList.add("partial");
    els.scanStatus.textContent +=
      "  " + fmt("scanOverBudgetMark", { n: late.length });
    els.scanStatus.title = fmt("scanOverBudgetHint", {
      switches: late.map(switchName).join(", "),
    });
  }
  updateLayoutStatus();
}

/* How much of this map was arranged by a person.

   Until v0.7.1 this counted nodes the saved picture did not contain,
   which made sense while saving was something you pressed a button
   for. Now a position is recorded as soon as the layout settles, so
   "not saved" is a state no node stays in — and a count of it would
   be a number that is always zero, or worse, briefly not. What is
   worth knowing is how much of the picture is somebody's decision
   rather than the engine's. */
function updateLayoutStatus() {
  const pinned = Object.keys(savedLayout).filter((id) =>
    savedLayout[id].pinned
  ).length;
  els.layoutStatus.classList.toggle("hidden", pinned === 0);
  if (!pinned) return;
  els.layoutStatus.textContent = fmt("layoutPinnedMark", { n: pinned });
  els.layoutStatus.title = t("layoutPinnedHint");
}

/* The service can go away — restarted, redeployed, or the machine this
   page is open from lost the route to it. Every periodic request has
   to survive that: an unhandled rejection every thirty seconds is not
   information, and a map that simply stops changing looks exactly like
   a quiet network where nothing is happening.

   So: the last good picture stays on screen, the header says the data
   is no longer arriving and how old it is, and the console gets one
   line when the connection is lost and one when it comes back. */
let serviceReachable = true;

function serviceLost(what) {
  if (serviceReachable) {
    serviceReachable = false;
    console.warn(
      "MoonLan: the service is not answering (" + what + "). The map " +
      "below is the last picture that arrived; polling continues."
    );
    updateScanStatus();
  }
}

function serviceBack() {
  if (!serviceReachable) {
    serviceReachable = true;
    console.info("MoonLan: the service is answering again.");
    updateScanStatus();
  }
}

/* While a scan runs the header counts switches off. The map itself is
   only refreshed every REFRESH_MS, which is no use to somebody
   watching a scan that may last minutes. */
let scanWatcher = null;

function watchScan() {
  if (scanWatcher) return;
  scanWatcher = setInterval(async () => {
    let status;
    try {
      status = await (await fetch("/api/status")).json();
      serviceBack();
    } catch (e) {
      serviceLost("scan progress");
      return; // keep the timer: the next tick may well succeed
    }
    topology.scanning = status.scanning;
    topology.scan_done = status.scan_done;
    topology.scan_total = status.scan_total;
    topology.scan_over_budget = status.scan_over_budget;
    updateScanStatus();
    if (!status.scanning) {
      clearInterval(scanWatcher);
      scanWatcher = null;
      els.rescan.disabled = false;
      isScanning = false;
      await loadTopology();
    }
  }, 2000);
}

/* ---------- data loading and rendering ---------- */

async function loadTopology() {
  let topo;
  let alarms;
  try {
    [topo, alarms] = await Promise.all([
      fetch("/api/topology").then((r) => r.json()),
      fetchAlarms("active=1"),
    ]);
  } catch (e) {
    // Whatever is on screen stays there. This is the moment somebody
    // is most likely to be looking at it.
    serviceLost("the map");
    return;
  }
  serviceBack();
  topology = topo;
  activeAlarms = alarms;
  renderBadge();
  renderSidebar();
  renderGraph();
  updateScanStatus();
  // A scan the operator did not start is worth counting off too: the
  // periodic one is when they are most likely to wonder why nothing
  // has moved for ten minutes.
  if (topology.scanning) watchScan();
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

function li(main, sub, dotClass, onClick, searchText, cssClass, title) {
  const item = document.createElement("li");
  if (title) item.title = title;
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
      li(
        sw.name,
        // same rule as the map caption: no address over the address
        sw.named === false ? sw.model || "" : sw.ip,
        sw.ping_up ? "up" : "down",
        () => focusNode("sw:" + sw.ip),
        undefined,
        // greyed the way an old host record is: the switch is there,
        // its numbers are from the last poll that finished
        sw.over_budget ? "stale" : "",
        sw.over_budget_scans >= 2
          ? fmt("staleSwitchScans", { n: sw.over_budget_scans }) +
            " · " + t("dataFrom") + " " + fmtTime(sw.polled_at)
          : ""
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

  // Ports the switches' own Loop Detection reports a loop on, as
  // "<switch ip>|<port name>". Polled on the counters cycle, so this
  // is fresher than the rest of the map.
  const loopedPorts = new Set();
  for (const sw of topology.switches) {
    for (const port of (sw.loop && sw.loop.looped_ports) || []) {
      loopedPorts.add(sw.ip + "|" + port);
    }
  }
  const isLooped = (ip, port) =>
    !!ip && !!port && loopedPorts.has(ip + "|" + port);

  /* Paints the edge that leaves a looping port. Only the segment that
     starts at the switch is marked: what hangs further down the chain
     is behind the loop, not in it. */
  const markLoop = (edge, ip, port) => {
    if (!isLooped(ip, port)) return edge;
    if (edge.from !== "sw:" + ip && edge.to !== "sw:" + ip) return edge;
    edge.color = { color: colors.alarm, opacity: 1 };
    edge.width = Math.max(edge.width || 1, 3);
    edge.dashes = false;
    edge.label = (edge.label ? edge.label + " · " : "") + t("loopMark");
    edge.font = { color: colors.alarm, size: 11, strokeWidth: 0 };
    return edge;
  };

  for (const sw of topology.switches) {
    // a switch_down alarm paints the node border red; so does a loop,
    // which is the one thing on this map that is taking a segment down
    // right now. The root bridge of a working tree gets a thicker
    // outline instead.
    const looping = !!(sw.loop && sw.loop.status === "loop");
    const border = switchHasAlarm(sw.ip) || looping
      ? colors.alarm
      : sw.stp_root
      ? colors.ok
      : colors.moon;
    // A switch with an empty sysName is captioned by its address, and
    // repeating the address underneath tells nobody anything. The
    // model out of sysDescr goes there instead, or nothing at all.
    const second = sw.named === false ? sw.model || "" : sw.ip;
    nodes.push(applyLayout({
      id: "sw:" + sw.ip,
      label:
        sw.name + (second ? "\n" + second : "") +
        (sw.stp_root ? "\n" + t("stpRootMark") : "") +
        (looping ? "\n" + t("loopMark") : ""),
      shape: "box",
      color: {
        background: colors.panel,
        border: border,
        // A selected node has to look selected. The highlight border
        // used to repeat the normal one, so with several nodes chosen
        // nothing on the map said which.
        highlight: { background: "#1c2739", border: colors.link },
      },
      font: nodeFont("sw:" + sw.ip),
      borderWidth: switchHasAlarm(sw.ip) || sw.stp_root || looping ? 3 : 2,
      margin: 10,
    }));
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
    // The MAC tables could not say which of several switches behind
    // one port is on the cable, so they are all hung off it. A guess
    // has no business looking like a measured cable: dashed, thinner,
    // and the tooltip says what is unknown about it.
    if (link.order_unknown) {
      edge.dashes = [4, 6];
      edge.color = { color: colors.moon, opacity: 0.55 };
      edge.width = 2;
      edge.title = t("linkOrderUnknown");
    }
    // …and a ring nothing could account for: not removed, not believed
    if (link.cycle_unresolved) {
      edge.dashes = [2, 4];
      edge.color = { color: colors.dim, opacity: 0.7 };
      edge.title = t("linkCycleUnresolved");
    }
    // a port STP is holding in discarding carries no traffic at all
    if (link.stp_blocking) {
      edge.color = { color: colors.alarm, opacity: 1 };
      edge.dashes = [6, 4];
      edge.label = (edge.label ? edge.label + " · " : "") + t("stpBlocking");
      edge.title = t("linkStpBlocking");
    }
    markLoop(edge, link.a, link.a_port);
    markLoop(edge, link.b, link.b_port);
    edges.push(edge);
  }

  for (const ps of topology.pseudo_switches || []) {
    nodes.push(applyLayout({
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
    }));
    edges.push(markLoop({
      id: "psedge:" + ps.id,
      from: "sw:" + ps.switch,
      to: ps.id,
      dashes: [4, 4],
      color: { color: colors.dim, opacity: 0.7 },
      width: 2,
    }, ps.switch, ps.port));
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
    nodes.push(applyLayout({
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
    }));
    edges.push(markLoop({
      id: "bredge:" + bridge.id,
      from: "sw:" + bridge.switch,
      to: bridge.id,
      dashes: [5, 3],
      color: { color: colors.link, opacity: 0.6 },
      width: 2,
    }, bridge.switch, bridge.port));
  }

  // one node per port that leaves the network: what is behind the
  // provider's handover is not ours to draw device by device
  for (const external of topology.external_networks || []) {
    nodes.push(applyLayout({
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
    }));
    edges.push(markLoop({
      id: "extedge:" + external.id,
      // the provider's switch is on the cable and everything else is
      // behind IT: mb0 -> CE6851 -> external network
      from: external.via || "sw:" + external.switch,
      to: external.id,
      color: { color: colors.warn || "#d9a86b", opacity: 0.7 },
      width: 3,
    }, external.switch, external.port));
  }

  // one node per port whose devices are all offline, so the switches
  // are not surrounded by a cloud of grey dots
  // one node per trunk carrying devices that have no place of their
  // own. Deliberately not shaped like a switch: nobody knows whether
  // there is one, only that these addresses come through this cable
  for (const group of topology.trunk_groups || []) {
    nodes.push(applyLayout({
      id: group.id,
      label: t("trunkGroup") + " · " + group.count,
      shape: "ellipse",
      color: {
        background: "#232c3d",
        border: colors.link,
        highlight: { background: "#2e3b52", border: colors.moon },
      },
      shapeProperties: { borderDashes: [4, 3] },
      borderWidth: 2,
      margin: 6,
      font: { color: colors.dim, size: 11 },
    }));
    edges.push(markLoop({
      id: "trunkedge:" + group.id,
      // off whatever stands on that cable, if anything does
      from: group.via || "sw:" + group.switch,
      to: group.id,
      dashes: [4, 3],
      color: { color: colors.link, opacity: 0.5 },
      width: 2,
    }, group.switch, group.port));
  }

  for (const group of topology.offline_groups || []) {
    nodes.push(applyLayout({
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
    }));
    edges.push(markLoop({
      id: "offedge:" + group.id,
      // the live devices of this port hang off the bridge or the
      // unmanaged switch on its cable; the quiet ones belong there too
      from: group.via || "sw:" + group.switch,
      to: group.id,
      dashes: [3, 3],
      color: { color: colors.dim, opacity: 0.4 },
      width: 1,
    }, group.switch, group.port));
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
    nodes.push(applyLayout({
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
    }));
    edges.push(markLoop({
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
    }, host.switch, host.port));
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
  // Anything without a saved position starts next to what it is
  // plugged into, not wherever the engine drops it
  seedPositions(nodes, edges);

  if (!network) {
    nodesDs = new vis.DataSet(nodes);
    edgesDs = new vis.DataSet(edges);
    const options = {
      physics: {
        solver: "forceAtlas2Based",
        forceAtlas2Based: { gravitationalConstant: -60, springLength: 90 },
        stabilization: { iterations: 200 },
      },
      // Ctrl+click adds and removes a node, and dragging one of
      // several selected nodes moves the whole selection — both of
      // them vis's own behaviour, which is better than a reimplementation
      interaction: { hover: true, multiselect: true },
      nodes: { borderWidthSelected: 4 },
    };
    network = new vis.Network(
      els.network,
      { nodes: nodesDs, edges: edgesDs },
      options
    );
    // A drag ends with a click event too. Without this, dropping a
    // node would also open its card — and the card covers the part of
    // the map somebody has just been arranging.
    let draggedNode = null;
    network.on("dragStart", (params) => {
      draggedNode = params.nodes.length ? params.nodes[0] : null;
    });
    // A new node's position is worth writing down once the layout has
    // settled around it — not while it is still being pushed about
    network.on("stabilized", saveNewPositions);
    // Double click on a container takes everything on it: the cloud of
    // devices behind one switch is the thing people want to move out
    // of the way in one go.
    network.on("doubleClick", (params) => {
      if (!params.nodes.length) return;
      const ids = devicesUnder(params.nodes[0]);
      if (!ids.length) return;
      network.selectNodes(ids, false);
      setSelectedNode(null);
      hideDetails();
    });
    network.on("dragEnd", (params) => {
      if (!params.nodes.length) return;
      // In arrange mode a drag places the node. Outside it, a drag is
      // somebody looking at the map — unless the node was already
      // pinned, in which case it keeps its pin and takes its new
      // coordinates with it.
      const at = network.getPositions(params.nodes);
      const moved = {};
      for (const id of params.nodes) {
        if (!at[id]) continue;
        if (arrangeMode || isPinned(id)) moved[id] = at[id];
      }
      if (Object.keys(moved).length) pinNodes(moved);
    });
    network.on("oncontext", (params) => {
      params.event.preventDefault();
      const id = network.getNodeAt(params.pointer.DOM);
      // In arrange mode the right button is the undo of the left one:
      // it releases the node, and asks nothing.
      if (arrangeMode && id && isPinned(id)) {
        unpinNode(id);
        return;
      }
      if (!id) {
        closeNodeMenu();
        return;
      }
      // Right-clicking inside the selection works on all of it;
      // right-clicking outside it means that node instead
      const selected = network.getSelectedNodes();
      const ids = selected.includes(id) ? selected : [id];
      if (!selected.includes(id)) network.selectNodes([id], false);
      openNodeMenu(ids, {
        x: params.event.clientX,
        y: params.event.clientY,
      });
    });
    network.on("click", (params) => {
      closeNodeMenu();
      if (draggedNode) {
        draggedNode = null;
        return;
      }
      // Ctrl+click is "add to the selection", not "look at this one"
      if (params.event && (params.event.srcEvent || {}).ctrlKey) return;
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
    // The very first map is exactly the one most likely to hold nodes
    // nobody has a position for
    scheduleAutoSave();
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
  scheduleAutoSave();
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

/* The "Loop Detection" row of a switch card: what the switch says,
   or that it says nothing. "No loops" is a claim; a model that keeps
   no loop state in any readable MIB does not get to make it. */
function loopCardLine(loop) {
  if (!loop || loop.polled === false) {
    // the counters cycle has not run since this switch was polled
    return t("loopCardNotPolled");
  }
  if (loop.supported === false) {
    return t("loopCardUnsupported");
  }
  if (loop.status === "loop") {
    return fmt("loopCardLoop", {
      ports: (loop.looped_ports || []).join(", "),
    });
  }
  // three-valued: on, off, and "the switch did not tell us". Never a
  // bare truthiness test — `!loop.enabled` reads the third as the
  // second, which is how three switches were reported as unprotected
  if (loop.enabled === false) return t("loopCardOff");
  if (loop.enabled !== true) return t("loopCardUnknown");
  const base = fmt("loopCardOn", {
    interval: loop.interval == null ? "—" : loop.interval,
    recover: loop.recover_time == null ? "—" : loop.recover_time,
  });
  const detail =
    loop.status === "partial" ? ", " + t("loopCardPartial") : "";
  return base + detail;
}

/* The sysObjectID and which profile it matched: the model identity
   behind the row, and the first thing needed to add a new profile. */
function loopCardTitle(loop) {
  if (!loop || !loop.sys_object_id) return "";
  const head = "sysObjectID " + loop.sys_object_id;
  if (!loop.supported) return head;
  return (
    head + "\n" +
    fmt("loopProfileHint", {
      profile: loop.profile,
      how: loop.matched_by,
    })
  );
}

/* A switch's name for a card, falling back to its address */
/* What to call a node in a confirmation dialog: its caption if the
   graph has one, its id otherwise. */
function nodeTitle(id) {
  const node = nodesDs && nodesDs.get(id);
  const label = node && node.label ? String(node.label).split("\n")[0] : "";
  return label.trim() || id;
}

function switchName(ip) {
  const sw = (topology.switches || []).find((s) => s.ip === ip);
  return sw ? sw.name : ip;
}

/* Fills the details card, hoisting its own <h3> into the panel head.
   Each branch below writes its title as part of the HTML — that is
   where it belongs, next to the thing it titles — and the head is
   where it has to end up, so it stays put while the card scrolls. */
function setDetails(html) {
  els.detailsBody.innerHTML = html;
  const heading = els.detailsBody.querySelector("h3");
  els.detailsTitle.replaceChildren(
    ...(heading ? [...heading.childNodes] : [])
  );
  if (heading) heading.remove();
}

/* Every line drawn from this switch: the neighbour, our port, whether
   it is up, and where the link came from. The tooltip on an edge
   answers that one edge at a time; "how many lines does this box have
   and why" is a question about the box, and belongs on its card. */
function switchLinksHtml(ip) {
  const rows = [];
  for (const link of topology.links) {
    const side = link.a === ip ? "a" : link.b === ip ? "b" : null;
    if (!side) continue;
    const otherIp = link[side === "a" ? "b" : "a"];
    const port = link[side + "_port"];
    const source = link.source || "fdb";
    const mark = link.cycle_unresolved
      ? " ⚠"
      : link.order_unknown
      ? " ?"
      : "";
    const title = link.cycle_unresolved
      ? t("linkCycleUnresolved")
      : link.order_unknown
      ? t("linkOrderUnknown")
      : link.stp_blocking
      ? t("linkStpBlocking")
      : "";
    rows.push(
      `<li${title ? ` title="${title}"` : ""}>` +
        `<span>${port}${mark} → ${switchName(otherIp)}</span>` +
        `<span class="sub">${
          source === "both"
            ? t("srcBoth")
            : source === "lldp"
            ? t("srcLldp")
            : t("srcFdb")
        }${link.speed_mbps ? " · " + fmtSpeed(link.speed_mbps) : ""}${
          link.stp_blocking ? " · " + t("stpBlocking") : ""
        }</span></li>`
    );
  }
  if (!rows.length) return "";
  return (
    `<h4 class="card-sub">${fmt("switchLinks", { n: rows.length })}</h4>` +
    `<ul class="offline-list">${rows.join("")}</ul>`
  );
}

function showDetails(nodeId) {
  let html = "";
  if (nodeId.startsWith("sw:")) {
    const sw = topology.switches.find((s) => "sw:" + s.ip === nodeId);
    if (!sw) return;
    const loop = sw.loop || {};
    html = `<h3>${sw.name}</h3>
      ${sw.over_budget
        ? `<p class="hint">${fmt(
            sw.over_budget_scans >= 2 ? "staleSwitchHint" : "overBudgetHint",
            { time: fmtTime(sw.polled_at), n: sw.over_budget_scans }
          )}</p>`
        : ""}${loop.supported === false
        ? `<p class="hint">${fmt("loopUnsupportedHint", {
            oid: loop.sys_object_id || "—",
          })}</p>`
        : ""}<dl>
      <dt>${t("ipAddr")}</dt><dd>${sw.ip}</dd>
      <dt>${t("bridgeMac")}</dt><dd>${sw.mac || "—"}</dd>
      <dt>${t("portsUpTotal")}</dt><dd>${sw.ports_up} / ${sw.ports_total}</dd>
      <dt>${t("lastReply")}</dt><dd>${fmtTime(sw.last_ping_ok)}</dd>
      ${sw.over_budget
        ? `<dt>${t("dataFrom")}</dt><dd>${fmtTime(sw.polled_at)}${
            sw.over_budget_scans >= 2
              ? " · " + fmt("staleSwitchScans", { n: sw.over_budget_scans })
              : ""
          }</dd>`
        : ""}
      <dt>${t("loopDetection")}</dt><dd${
        loop.status === "loop" ? ' class="loop-alarm"' : ""
      } title="${loopCardTitle(loop)}">${loopCardLine(loop)}</dd>
      <dt>${t("descr")}</dt><dd>${sw.descr || "—"}</dd></dl>
      ${switchLinksHtml(sw.ip)}
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
      ${group.via_name
        ? `<dt>${t("behindBridge")}</dt><dd>${group.via_name}</dd>`
        : ""}
      <dt>${t("lastSeenLabel")}</dt><dd>${fmtTime(group.last_seen_max)}</dd>
      </dl><ul class="offline-list">${rows}</ul>`;
  } else if (nodeId.startsWith("trunk:")) {
    const group = (topology.trunk_groups || []).find((g) => g.id === nodeId);
    if (!group) return;
    const members = topology.hosts.filter((h) => h.via === nodeId);
    const rows = members
      .map(
        (h) => `<li data-mac="${h.mac}"><span>${hostLabel(h)}</span>
          <span class="sub">${h.ip ? h.mac : ""}</span></li>`
      )
      .join("");
    html = `<h3>${fmt("trunkGroupTitle", { n: group.count })}</h3>
      <p class="hint">${fmt("trunkGroupHint", {
        switch: switchName(group.switch),
        port: group.port,
      })}</p><dl>
      <dt>${t("switchLabel")}</dt><dd>${group.switch}</dd>
      <dt>${t("portLabel")}</dt><dd>${group.port}</dd>
      ${group.via_name
        ? `<dt>${t("behindBridge")}</dt><dd>${group.via_name}</dd>`
        : ""}
      <dt>${t("devicesBehindPort")}</dt><dd>${group.count}</dd>
      ${group.silent
        ? `<dt>${t("trunkGroupSilent")}</dt><dd>${group.silent}</dd>`
        : ""}
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
    // A device seen only on uplinks is a third case: it was seen this
    // very scan, and every sighting was of the cable it is NOT behind.
    const hint = host.uplink_only
      ? t("uplinkOnlyHint")
      : offMap
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
      ${host.approximate
        ? `<p class="hint">${t("approximateHint")}<br>${fmt("approximateWhy", {
            switch: switchName(host.switch),
            port: host.port,
          })}</p>`
        : ""}
      ${host.remembered ? `<p class="hint">${t("rememberedHint")}</p>` : ""}
      ${host.from_saved
        ? `<p class="hint">${fmt("hostFromSavedHint", {
            switch: switchName(host.switch),
            time: fmtTime(host.reading_at),
          })}</p>`
        : ""}
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
      ${(host.seen_on || []).length
        ? `<dt>${t("uplinkOnlySeenOn")}</dt><dd>${host.seen_on
            .map((s) => `${switchName(s.switch)} ${s.port}`)
            .join(", ")}</dd>`
        : ""}
      <dt>${t("switchLabel")}</dt><dd>${host.switch || "—"}</dd>
      <dt>${t("portLabel")}</dt><dd>${host.port || "—"}${
        host.approximate ? ` <span class="chip">${t("approximate")}</span>` : ""
      }${
        host.remembered ? ` <span class="chip">${t("remembered")}</span>` : ""
      }${
        host.from_saved
          ? ` <span class="chip" title="${fmt("hostFromSavedHint", {
              switch: switchName(host.switch),
              time: fmtTime(host.reading_at),
            })}">${t("fromSaved")}</span>`
          : ""
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
      ${host.from_saved
        ? `<dt>${t("readingFrom")}</dt><dd>${fmtTime(host.reading_at)}</dd>`
        : ""}
      ${offMap ? `<dt>${t("lastArpLabel")}</dt><dd>${fmtTime(host.last_arp)}</dd>` : ""}
      <dt>${t("firstSeen")}</dt><dd>${fmtDate(host.first_seen)}</dd></dl>
      <button id="monitor-btn" class="panel-btn${host.monitored ? " active" : ""}">
        ${host.monitored ? "★" : "☆"} ${t("monitorBtn")}</button>`;
  }
  // Placed by hand: say so, and offer to hand it back to the physics
  // engine. The map must answer "did MoonLan arrange this or did I"
  // without anyone having to guess.
  if (savedLayout[nodeId] && savedLayout[nodeId].pinned) {
    html +=
      `<p class="hint">${t("pinnedHint")}</p>` +
      `<button id="unpin-btn" class="panel-btn">${t("unpinBtn")}</button>`;
  }
  shownDetails = { type: "node", id: nodeId };
  setDetails(html);
  els.details.classList.remove("hidden");
  els.journal.classList.add("hidden");
  els.alarms.classList.add("hidden");
  els.stp.classList.add("hidden");
  closePorts();
  const unpinBtn = document.getElementById("unpin-btn");
  if (unpinBtn) {
    unpinBtn.addEventListener("click", () => unpinNode(nodeId));
  }
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
  let data;
  try {
    const res = await fetch(
      "/api/switch/" + encodeURIComponent(portsIp) + "/ports"
    );
    data = await res.json();
  } catch (e) {
    // the panel keeps the last table, the header says why
    serviceLost("the ports panel");
    return;
  }
  serviceBack();
  lastPorts = data;
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
    // a loop first, then the ports nothing is known about, then the
    // quiet ones — the order someone sorting by this column wants
    loop: (p) => LOOP_RANK[(p.loop || {}).state] ?? -1,
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

/* Sort order of the loop column: a loop first, then unknown, then
   the ports that are fine, then the ones nobody is watching */
const LOOP_RANK = { loop: 3, unknown: 2, ok: 1, off: 0 };

/* One "Loop" cell. Four states, and the difference between the last
   two is the whole point: "no loop" is an answer, "no data" is not. */
function loopCell(tr, loop, report) {
  const cell = document.createElement("td");
  const state = loop ? loop.state : "";
  if (!loop && report && report.polled === false) {
    // nobody has asked yet: unknown, not "the model says nothing"
    cell.textContent = t("loopNoData");
    cell.className = "loop-unknown";
    cell.title = t("loopCardNotPolled");
  } else if (!loop) {
    // the model reports nothing at all — see the switch card
    cell.textContent = "—";
    cell.className = "loop-none";
    cell.title = fmt("loopUnsupportedHint", {
      oid: (report && report.sys_object_id) || "—",
    });
  } else if (state === "loop") {
    cell.textContent = t("loopYes");
    cell.className = "loop-alarm";
    cell.title = fmt("loopRawHint", { raw: loop.raw || "—" });
  } else if (state === "ok") {
    cell.textContent = t("loopOk");
    cell.className = "loop-ok";
  } else if (state === "off") {
    cell.textContent = t("loopOff");
    cell.className = "loop-none";
    cell.title = t("loopOffHint");
  } else {
    cell.textContent = t("loopNoData");
    cell.className = "loop-unknown";
    cell.title = t("loopNoDataHint");
  }
  tr.append(cell);
}

function renderPorts(data) {
  els.portsTitle.textContent = fmt("portsTitle", {
    name: data.name || data.switch,
  });
  markSilentColumns(data.columns);
  const limits = data.thresholds || {};
  // Past this age a rate is shown dimmed with its age beside it; past
  // `hide_after_seconds` the server withholds the number itself. Both
  // come from the service, which is where the intervals live.
  const staleAfter = (data.rates || {}).stale_after_seconds ?? Infinity;
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
      // A rate cell carries its own age. The number is real but was
      // measured a while ago — the counters cycle skipped this switch,
      // or it is busy being scanned — and dropping it would draw the
      // same "—" as a column the agent never answers. Those two say
      // very different things about a switch.
      const rateTd = (value, cls) => {
        const stale =
          p.rate_age_seconds != null && p.rate_age_seconds >= staleAfter;
        // expired: a dash that has something to say, so it asks to be
        // hovered — a dash with nothing behind it does not
        const expired = value == null && p.rate_age_seconds != null;
        td(
          fmtRate(value),
          (cls || "") +
            (stale && value != null ? " stale-rate" : "") +
            (expired ? " rate-expired" : "")
        );
        const cell = tr.lastChild;
        if (expired) {
          // measured once, too long ago to show: say when, do not
          // leave a bare dash that reads as "never polled"
          cell.title = fmt("rateTooOld", {
            when: fmtAge(p.rate_age_seconds),
          });
        } else if (stale) {
          cell.title = fmt("rateMeasured", {
            when: fmtAge(p.rate_age_seconds),
          });
        }
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
      rateTd(p.in_mbps, "num");
      rateTd(p.out_mbps, "num");
      // damaged frames are a fault, discards are usually filtering
      const overErr = p.errors_per_min > (limits.errors_per_minute ?? Infinity);
      const overDisc =
        p.discards_per_min > (limits.discards_per_minute ?? Infinity);
      rateTd(p.errors_per_min, "num" + (overErr ? " over-error" : ""));
      rateTd(p.discards_per_min, "num" + (overDisc ? " over-discard" : ""));
      loopCell(tr, p.loop, data.loop_detection);
      return tr;
    });
  els.portsBody.replaceChildren(...rows);
  if (highlighted) highlighted.scrollIntoView({ block: "center" });
}

/* Link card: ports of both ends, speed, aggregate members */
function showLinkDetails(edgeId) {
  const link = topology.links.find((l) => linkId(l) === edgeId);
  if (!link) return;
  const swName = switchName;
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
  // What is uncertain about this line, said in the card and not only
  // in a tooltip somebody has to know to hover
  if (link.order_unknown) {
    html += `<p class="hint">${t("linkOrderUnknown")}</p>`;
  }
  if (link.cycle_unresolved) {
    html += `<p class="hint">${t("linkCycleUnresolved")}</p>`;
  }
  shownDetails = { type: "link", id: edgeId };
  setDetails(html);
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
  // several roots on a physically connected network is a legal state,
  // not necessarily a fault — BPDUs travel inside the VLAN of the port
  // they arrive on, so segments in different VLANs form separate trees
  // by design. Said here because the word "fragmented" implies damage.
  if (data.verdict && data.verdict.verdict === "fragmented") {
    const why = document.createElement("p");
    why.className = "hint";
    why.textContent = t("stpFragmentedVlanHint");
    body.append(why);
    // …and the VLANs each island's trunks sit in, which is usually the
    // whole answer to "why are there two of them"
    const byIp = Object.fromEntries(data.switches.map((sw) => [sw.ip, sw]));
    const list = document.createElement("ul");
    list.className = "stp-roots";
    for (const [root, ips] of Object.entries(data.verdict.roots || {})) {
      const vlans = [
        ...new Set(ips.flatMap((ip) => (byIp[ip] || {}).trunk_vlans || [])),
      ].sort((a, b) => a - b);
      const li = document.createElement("li");
      li.textContent =
        root + " — " + ips.map((ip) => (byIp[ip] || {}).name || ip).join(", ") +
        (vlans.length ? "  (" + t("vlan") + " " + vlans.join(", ") + ")" : "");
      list.append(li);
    }
    if (list.childElementCount) body.append(list);
  }

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
  // The root of the tree, as the network agreed it. A switch that had
  // to be identified as root by its neighbours reports zeros about
  // itself, and printing those in its own row would undo the finding.
  const agreedRoot = Object.keys((data.verdict || {}).roots || {})[0] || "";
  const tbody = document.createElement("tbody");
  for (const sw of data.switches) {
    const tr = document.createElement("tr");
    const cells = sw.confirmed_root
      ? [
          sw.name,
          t("stpStateRoot"),
          // it never told us its priority; the root's identity came
          // from the switches that follow it
          "—",
          agreedRoot || sw.designated_root || "—",
          "—",
          "—",
          "—",
          "—",
        ]
      : sw.operating
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
      // the basis for the verdict, either way: an operator who cannot
      // see why a switch is called operating cannot check the claim
      if (i === 1) td.title = sw.reason || "";
      if (i === 1 && !sw.operating) td.className = "stp-off";
      if (i === 1 && sw.is_root) td.className = "stp-root";
      if (i === 3 && sw.root_nonstandard) {
        // the agent put the priority in the low byte and MoonLan put
        // it back; the number here is not the one the switch prints
        td.className = "stp-corrected";
        td.title = t("stpBridgeIdFixed");
      }
      tr.append(td);
    });
    tbody.append(tr);
    // Only for a switch whose tree we say is operating. "BLOCKING" is
    // a statement about a spanning tree, and printing one under a row
    // of dashes that says there is no tree contradicts it in place.
    if (sw.operating && sw.blocking_ports && sw.blocking_ports.length) {
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
  // only the table scrolls sideways; the verdict and the hint above it
  // are prose and have no business moving off the left edge
  const scroller = document.createElement("div");
  scroller.className = "table-scroll";
  scroller.append(table);
  body.append(scroller);
  // Switches that answer dot1dStp* and name no root. They are not a
  // tree of their own — counting them as one turned three RouterOS
  // boxes into a second spanning tree called "unknown" — but they must
  // not quietly disappear from the panel either.
  const noRoot = (data.verdict || {}).rootless || [];
  if (noRoot.length) {
    const byIp = Object.fromEntries(data.switches.map((sw) => [sw.ip, sw]));
    const line = document.createElement("p");
    line.className = "hint";
    line.textContent =
      fmt("stpRootless", {
        switches: noRoot.map((ip) => (byIp[ip] || {}).name || ip).join(", "),
      });
    body.append(line);
  }
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
  watchScan();
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
els.arrangeBtn.addEventListener("click", toggleArrangeMode);
document.addEventListener("click", (event) => {
  if (!els.nodeMenu.contains(event.target)) closeNodeMenu();
});
els.resetLayoutBtn.addEventListener("click", resetLayout);
els.alarmsBtn.addEventListener("click", toggleAlarms);
els.alarmsClose.addEventListener("click", () =>
  els.alarms.classList.add("hidden")
);
els.stpBtn.addEventListener("click", toggleStp);
els.stpClose.addEventListener("click", () => els.stp.classList.add("hidden"));
els.langRu.addEventListener("click", () => setLang("ru"));
els.langEn.addEventListener("click", () => setLang("en"));

/* Keyboard. `P` pins or releases whatever is selected — but only when
   the focus is not in a field, or it would fire while somebody types
   a MAC into the search box. */
document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing =
    target &&
    (target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable);
  if (typing) return;
  if (event.key === "p" || event.key === "P") {
    togglePinOnSelection();
  } else if (event.key === "Escape") {
    if (network) network.unselectAll();
    setSelectedNode(null);
    closeNodeMenu();
  }
});

/* `P` on the selection: if anything in it is loose, pin the lot;
   otherwise release the lot. One key, and its effect is predictable
   from what is on screen. */
function togglePinOnSelection() {
  if (!network) return;
  const ids = network.getSelectedNodes();
  if (!ids.length) return;
  const loose = ids.filter((id) => !isPinned(id));
  if (loose.length) {
    const at = network.getPositions(loose);
    const moved = {};
    for (const id of loose) if (at[id]) moved[id] = at[id];
    pinNodes(moved);
  } else {
    unpinNodes(ids);
  }
}

applyStatic();
applyArrangeMode();
// The layout is fetched before the first map is drawn, so nodes start
// where they were left rather than where the physics engine throws
// them and then get yanked into place a moment later.
loadLayout().then(loadTopology);
setInterval(loadTopology, REFRESH_MS);
