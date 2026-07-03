/* MoonLan web UI, v0.1 */

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
  emptyState: document.getElementById("empty-state"),
};

let network = null;
let topology = { switches: [], links: [], hosts: [] };

const colors = {
  moon: "#e8e4d5",
  link: "#7fb4d9",
  text: "#d7dee9",
  dim: "#8593a8",
  panel: "#141c2c",
};

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

function li(main, sub, onClick) {
  const item = document.createElement("li");
  const name = document.createElement("span");
  name.textContent = main;
  const extra = document.createElement("span");
  extra.className = "sub";
  extra.textContent = sub;
  item.append(name, extra);
  item.dataset.search = (main + " " + sub).toLowerCase();
  item.addEventListener("click", onClick);
  return item;
}

function renderSidebar() {
  els.switchList.replaceChildren(
    ...topology.switches.map((sw) =>
      li(sw.name, sw.ip, () => focusNode("sw:" + sw.ip))
    )
  );
  els.hostList.replaceChildren(
    ...topology.hosts.map((h) =>
      li(h.name || h.mac, h.switch + " / " + h.port, () =>
        focusNode("host:" + h.mac)
      )
    )
  );
  els.switchCount.textContent = topology.switches.length;
  els.hostCount.textContent = topology.hosts.length;
  applySearchFilter();
}

function renderGraph() {
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
    edges.push({
      from: "sw:" + link.a,
      to: "sw:" + link.b,
      label: link.speed_mbps ? link.speed_mbps / 1000 + " Гбит/с" : "",
      color: { color: colors.moon, opacity: 0.8 },
      width: 3,
      font: { color: colors.dim, size: 11, strokeWidth: 0 },
    });
  }

  for (const host of topology.hosts) {
    nodes.push({
      id: "host:" + host.mac,
      label: host.name || host.mac,
      shape: "dot",
      size: 9,
      color: { background: colors.link, border: colors.link },
      font: { color: colors.dim, size: 11, face: "ui-monospace" },
    });
    edges.push({
      from: "sw:" + host.switch,
      to: "host:" + host.mac,
      color: { color: colors.link, opacity: 0.35 },
      width: 1,
    });
  }

  const data = {
    nodes: new vis.DataSet(nodes),
    edges: new vis.DataSet(edges),
  };
  const options = {
    physics: {
      solver: "forceAtlas2Based",
      forceAtlas2Based: { gravitationalConstant: -60, springLength: 90 },
      stabilization: { iterations: 200 },
    },
    interaction: { hover: true },
  };

  if (network) network.destroy();
  network = new vis.Network(els.network, data, options);
  network.on("click", (params) => {
    if (params.nodes.length) showDetails(params.nodes[0]);
    else hideDetails();
  });
}

function focusNode(id) {
  if (!network) return;
  network.focus(id, { scale: 1.2, animation: true });
  network.selectNodes([id]);
  showDetails(id);
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
      <dt>Описание</dt><dd>${sw.descr || "—"}</dd></dl>`;
  } else {
    const host = topology.hosts.find((h) => "host:" + h.mac === nodeId);
    if (!host) return;
    html = `<h3>${host.name || "Устройство"}</h3><dl>
      <dt>MAC-адрес</dt><dd>${host.mac}</dd>
      <dt>Коммутатор</dt><dd>${host.switch}</dd>
      <dt>Порт</dt><dd>${host.port}</dd></dl>`;
  }
  els.detailsBody.innerHTML = html;
  els.details.classList.remove("hidden");
}

function hideDetails() {
  els.details.classList.add("hidden");
}

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

loadTopology();
