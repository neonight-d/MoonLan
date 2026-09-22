/* MoonLan UI strings (en / ru).
   This is the only file in web/ that is allowed to contain Russian text.
   Placeholders like {name} and {n} are substituted by fmt() in app.js. */

const I18N = {
  en: {
    title: "MoonLan — network map",
    tagline: "local network map",
    journalBtn: "Journal",
    freezeBtn: "Freeze layout",
    layoutPinnedMark: "placed by hand: {n}",
    layoutPinnedHint:
      "That many nodes are where somebody put them, out of reach of the "
      + "layout engine. Everything else is arranged automatically, and "
      + "where it ends up is recorded on its own \u2014 there is nothing "
      + "to save by hand. Turn on Arrange to place a node, or press P "
      + "with it selected; the same releases it again.",
    menuPin: "Place here",
    menuUnpin: "Release",
    menuOpenCard: "Open card",
    menuOpenPorts: "Ports",
    menuSelected: "{n} nodes selected",
    menuPing: "Ping",
    menuTraceroute: "Traceroute",
    reasonNo_ip: "IP unknown",
    reasonNoService: "no connection to the service",
    reasonNoPing: "no ping on the server",
    reasonNoTraceroute: "no traceroute on the server",
    actionStarting: "Starting…",
    actionRunning: "Running on the MoonLan server…",
    actionDone: "Done",
    actionSimulated: "Demo mode: nothing is sent to the network, the result is simulated.",
    actionTarget: "Target",
    actionThisRun: "This run: 4 packets from the MoonLan server",
    actionMonitor: "Continuous monitoring",
    actionTool: "Tool",
    pingLoss: "loss {loss}% ({received} of {sent} answered)",
    pingRtt: "RTT min / avg / max",
    ms: "ms",
    monitorNone: "not monitored",
    monitorUp: "answering; last reply {time}",
    monitorDown: "not answering; last reply {time}",
    monitorNever: "not answering; it never has",
    verdictOk: "answers",
    verdictPartial: "losses",
    verdictNoReply: "no reply",
    verdictNoIp: "IP unknown",
    verdictTimeout: "timed out",
    verdictFailed: "failed",
    refuseBusy: "The server is already running {limit} checks, which is the ceiling (context_menu.max_running). Try again in a few seconds.",
    refuseNoTool: "There is no {tool} on the MoonLan server.",
    refuseAddress: "The server takes node ids, not addresses: {list}",
    refuseUnknown: "The server does not know these nodes — the map may be older than its last scan; reload the page: {list}",
    refuseJobGone: "The server no longer keeps this result.",
    refuseOther: "The server refused: {error}",
    arrangeBtn: "Arrange",
    arrangeOnBtn: "Arranging",
    arrangeHint:
      "Arrange mode is on: dragging a node places it and keeps it "
      + "there, and the right button lets it go again. Outside this "
      + "mode dragging only moves the view of the map.",
    resetLayoutBtn: "Reset layout",
    pinnedMark: "\ud83d\udccc",
    pinnedHint:
      "This node is where somebody put it, not where the layout "
      + "engine would have put it. It is out of the physics and stays "
      + "here for everyone looking at this map, through reloads and "
      + "restarts.",
    unpinBtn: "Release this node",
    unpinConfirm: "Release \u201c{node}\u201d back to the layout engine?",
    resetLayoutConfirm:
      "Forget every saved position, for everyone? The map will lay "
      + "itself out from scratch, and nodes placed by hand will lose "
      + "their places. This cannot be undone.",
    unfreezeBtn: "Unfreeze",
    rescanBtn: "Rescan network",
    searchPlaceholder: "Search: name, IP or MAC…",
    switchesHeader: "Switches",
    devicesHeader: "Devices",
    journalTitle: "Event journal",
    noEvents: "No events yet",
    emptyLine1: "The map is empty.",
    emptyLine2:
      "Add switches to <code>config.yaml</code> and press " +
      "“Rescan network”, or start the service with " +
      "<code>MOONLAN_DEMO=1</code> to see a demo network.",
    scanning: "Scanning the network…",
    scanPrefix: "Last scan: ",
    scanFailed: "Last scan failed: ",
    noData: "No data yet",
    close: "Close",
    ev_new_mac: "New device",
    ev_host_down: "Host down",
    ev_host_up: "Host back online",
    ev_alarm_raised: "Alarm raised",
    ev_alarm_cleared: "Alarm cleared",
    name: "Name",
    ipAddr: "IP address",
    macAddr: "MAC address",
    bridgeMac: "Bridge MAC",
    switchLabel: "Switch",
    portLabel: "Port",
    vlan: "VLAN",
    lastReply: "Last reply",
    firstSeen: "First seen",
    portsUpTotal: "Ports (up/total)",
    descr: "Description",
    speed: "Speed",
    pseudoTitle: "Switch without SNMP",
    pseudoHint:
      "An unmanaged switch or access point is visible behind this port " +
      "({n} devices).",
    devicesBehindPort: "Devices behind port",
    devicesCount: "{n} ({live} seen right now)",
    portOnSide: "Port on {name} side",
    linkSource: "Source",
    srcLldp: "LLDP",
    srcFdb: "MAC tables",
    srcBoth: "LLDP and MAC tables",
    srcHintLldp:
      "Both devices announce each other over LLDP: the link and the "
      + "ports on both ends are their own statement, not an inference.",
    srcHintFdb:
      "Inferred from the MAC address tables. The devices do not "
      + "announce each other over LLDP (it may be off, or the frames "
      + "may not reach), so the ports are the best available guess.",
    chassisId: "Chassis ID",
    mgmtIp: "Management address",
    remotePort: "Port on the neighbour",
    capabilities: "Capabilities",
    capsUnknown: "not announced",
    bridgeHint:
      "LLDP reports a bridge behind this port — a switch MoonLan does "
      + "not poll. It was not configured here; the devices behind it "
      + "hang off this node.",
    lldpUnknownHint:
      "An LLDP device that announces no capabilities (the optional TLVs "
      + "are disabled on it). The absence of the bridge flag proves "
      + "nothing about what it is, so it gets no node of its own and "
      + "raises no alarm — it is listed on its port instead.",
    al_unmanaged_bridge_detected: "Unmanaged bridge",
    bridgeSharesPortHint:
      "Several LLDP devices answer behind this port, so an unmanaged "
      + "switch sits on the cable and they hang off it. The devices on "
      + "the port stay with that switch: nothing says which bridge each "
      + "of them is behind.",
    lldpLabel: "LLDP",
    lldpUnidentified: "unidentified LLDP device",
    lldpCrowded: "several LLDP devices",
    lldpCrowdedHint:
      "Several LLDP devices answer behind this port, so an unmanaged "
      + "switch sits on the cable. They are all real and all shown — "
      + "but which of them is on the cable itself cannot be told, so no "
      + "link is drawn from this port.",
    portLabelName: "Port name configured on the switch (LLDP)",
    andMore: "and {n} more",
    sameDevice: "Same device as",
    kind_router: "router",
    kind_access_point: "access point",
    kind_phone: "phone",
    kind_bridge: "bridge",
    kind_repeater: "repeater",
    kind_station: "end station",
    kind_other: "device",
    kind_unknown: "unidentified LLDP device",
    nameFromLldpHint:
      "The caption comes from LLDP: the device named itself, but no ARP "
      + "table gave it an address and reverse DNS has no record of it. "
      + "Routers are deliberately kept out of the host inventory.",
    externalNetwork: "External network",
    externalPort: "external uplink",
    externalHint:
      "This port leaves the network — a provider handover or an uplink "
      + "to someone else's equipment (config.uplink_ports). What is "
      + "behind it is collected under one node instead of being drawn "
      + "device by device, and switches there raise no alarms: they are "
      + "the boundary, not something anyone forgot to configure.",
    bridgeAssumedHint:
      "Probably a bridge — an inference, not the device's own claim. "
      + "This switch reports LLDP capabilities for no neighbour at all, "
      + "so the empty field says nothing; the device was taken for a "
      + "bridge because it announced a system name and a management "
      + "address. Its alarm is raised at info for the same reason.",
    routerAddr: "Router address (routers:)",
    uplinkHintChip: "way out?",
    uplinkWhy_public_address: "A device behind this port answers at {ip}.",
    uplinkWhy_foreign_subnet:
      "{name} is managed at {ip}, in a subnet MoonLan sees nowhere else, "
      + "and nothing of ours answers behind this port.",
    uplinkHint:
      "This looks like a way out of the network. Add "
      + "\"{switch}:{port}\" to uplink_ports in config.yaml and the "
      + "devices behind it will be collected under one \"External "
      + "network\" node, with no bridge alarms raised there.",
    colNoAnswer:
      "The switch did not answer for this counter ({oids}), so the "
      + "column shows \u2014 rather than 0. Unknown is not zero: no alarm "
      + "is raised or cleared on it either.",
    remembered: "from the inventory",
    fromSaved: "from a saved reading",
    readingFrom: "Reading taken",
    hostFromSavedHint:
      "{switch} did not finish its poll inside the budget, so this "
      + "device is drawn where the last reading that did arrive put it, "
      + "taken at {time}. Nobody has looked for it since: \u201clast "
      + "seen\u201d above is the last time it really was found, and it "
      + "is not moving while this lasts. Cables do not change every ten "
      + "minutes, which is why the place is still shown \u2014 but a "
      + "copy of a reading is not an observation.",
    rememberedHint:
      "The MAC is visible only on trunk ports right now, but the "
      + "inventory knows the port this device sits on, so it is drawn "
      + "there rather than guessed onto a trunk.",
    offlineGroupGuessHint:
      "These devices are missing from the current switch tables and the "
      + "port shown is a trunk — nothing was ever seen there as a "
      + "device, so this location is inherited guesswork rather than a "
      + "record of where they were last seen.",
    behindBridge: "Behind",
    colPartial:
      "The switch answered for this counter ({oids}) and then stopped "
      + "partway through the table; {filled} port(s) were read one at a "
      + "time from the 32-bit counter instead. Any port still showing "
      + "\u2014 is unknown, not zero.",
    lldpNeighbour: "LLDP neighbour",
    lldpForwarded: "LLDP forwarded",
    lldpForwardedHint:
      "This LLDP did not come from the cable: the same neighbour is on "
      + "another port, or its MAC sits in the switch's forwarding table "
      + "behind a different one. LLDP frame forwarding is most likely "
      + "enabled. The data is unreliable and is not used to draw links.",
    stpBtn: "STP",
    stpTitle: "Spanning tree",
    stpHint:
      "A switch with STP disabled still answers every dot1dStp* object: "
      + "priority 0, cost 0 and itself as the root. Root, cost and root "
      + "port are therefore shown only where the tree demonstrably "
      + "operates — at least one port enabled and not disabled, and a "
      + "topology change that actually happened.",
    stpState: "State",
    stpStateOff: "not operating",
    stpStateMember: "in the tree",
    stpStateRoot: "root bridge",
    stpPriority: "Priority",
    stpRoot: "Root",
    stpCost: "Cost to root",
    stpRootPort: "Root port",
    stpChanges: "Topology changes",
    stpLastChange: "Since the last one",
    stpRootMark: "STP root",
    stpBlocking: "BLOCKING",
    linkOrderUnknown:
      "The order of the switches behind this port could not be worked "
      + "out: their MAC tables do not show each other, and neither side "
      + "reports the other over LLDP. They are all drawn on the nearest "
      + "known switch, which is where they are reachable through \u2014 "
      + "not necessarily what they are plugged into. A dashed line means "
      + "a guess at the order, not a measured cable.",
    linkCycleUnresolved:
      "This link is part of a ring among polled switches in which no "
      + "port is held in discarding by the spanning tree. One of its "
      + "links is wrong, and nothing in the data says which \u2014 so "
      + "none was removed, and all of them are marked rather than shown "
      + "as facts.",
    linkStpBlocking:
      "The spanning tree is holding a port of this link in discarding, "
      + "so it carries no traffic. A ring with a blocked port is what a "
      + "working network with a physical ring looks like.",
    switchLinks: "Links ({n})",
    stpRootless:
      "Answer dot1dStp* and name no root: {switches}. Their port tables "
      + "are real, but they do not implement the objects that hold the "
      + "root \u2014 RouterOS is one such agent. A bridge in a tree "
      + "knows its root, so these are not counted as a spanning tree of "
      + "their own, and they do not make the network fragmented.",
    stpBlockingHint:
      "Spanning tree holds this port in the blocking state: the link "
      + "exists but carries no user traffic. It is the standby path of "
      + "a redundant pair.",
    stpVerdictNone: "STP is not running anywhere in this network",
    stpVerdictSingle: "One spanning tree, root {root}",
    stpVerdictFragmented: "The tree is fragmented: {n} separate roots",
    stpFragmentedVlanHint:
      "Several roots on a physically connected network is not "
      + "necessarily a fault. BPDUs are untagged and are handled in "
      + "the VLAN of the port they arrive on, so segments whose trunk "
      + "ports sit in different VLANs form separate trees by design "
      + "and never meet. Check the VLAN membership of the trunk ports "
      + "before treating this as damage.",
    stpBridgeIdFixed:
      "This switch reports the Bridge ID with the priority in the low "
      + "byte, where the standard puts it in the high one — its own "
      + "reading of the number is 256 times smaller. MoonLan shows the "
      + "corrected value, which is why it differs from the switch's "
      + "own web interface.",
    al_stp_root_changed: "STP root changed",
    al_stp_topology_change: "STP topology changes",
    al_stp_fragmented: "STP fragmented",
    al_port_flapping: "Port flapping",
    al_loop_detected: "Loop detected",
    al_loop_detection_disabled: "Loop detection off",
    colLoop: "Loop",
    loopOk: "none",
    loopYes: "LOOP",
    loopNoData: "no data",
    loopOff: "\u2014",
    loopRawHint:
      "Loop Detection reports {raw} for this port. Only the value that "
      + "means \u201cno loop\u201d is known for this model, so anything "
      + "else is read as a loop \u2014 the raw value is in the alarm so "
      + "the first real one documents itself.",
    loopOffHint:
      "Loop Detection is switched off for this port on the switch "
      + "itself, so nothing here is being watched.",
    loopNoDataHint:
      "No status arrived for this port. That is unknown, not "
      + "\u201cno loop\u201d.",
    loopMark: "LOOP",
    loopDetection: "Loop Detection",
    loopCardOn: "on, interval {interval} s, recovery {recover} s",
    loopCardOff: "switched off on the switch",
    loopCardUnknown: "state unknown \u2014 the switch did not report it",
    loopCardLoop: "LOOP on {ports}",
    loopCardUnsupported: "the model does not report it over SNMP",
    loopCardNotPolled: "no data yet \u2014 the counters cycle has not run since the last scan",
    loopCardPartial: "answered only in part \u2014 some ports are unknown",
    loopUnsupportedHint:
      "This switch model keeps no loop-detection state in any MIB "
      + "MoonLan can read, so nothing is claimed about it in either "
      + "direction. Its sysObjectID is {oid} \u2014 see \u201cLoop "
      + "Detection\u201d in the README for how to walk the private "
      + "branch and add a profile to config.yaml.",
    loopProfileHint: "profile {profile}, matched by {how}",
    dataFrom: "Data from",
    staleSwitchScans: "{n} scans running without a full poll",
    staleSwitchHint:
      "This switch has answered but not finished a full poll for {n} "
      + "scans running, so everything on this card is from {time}. It is "
      + "reachable \u2014 no \u201cswitch down\u201d alarm is raised for "
      + "it \u2014 but its numbers have stopped moving. Its poll budget "
      + "is too small for it: give it more time, or a shorter SNMP "
      + "timeout of its own so the requests it never answers are given "
      + "up on sooner. See switches: and snmp.host_budget_seconds in "
      + "config.yaml.",
    al_switch_stale: "Switch data not refreshing",
    ageUnderMinute: "less than a minute ago",
    ageMinutes: "{n} min ago",
    ageHours: "{h} h ago",
    ageHoursMinutes: "{h} h {n} min ago",
    rateMeasured:
      "Measured {when}. The counters cycle has not reached this switch "
      + "since \u2014 it is being scanned, or it answers slowly. The "
      + "number is real, only not current.",
    rateTooOld:
      "Last measured {when}. That is too long ago to show as a rate, so "
      + "the cell is empty \u2014 but this port has been measured, "
      + "which a port that never has never says. See "
      + "stale_rate_hide_minutes in config.yaml.",
    scanningProgress: "Scanning: {done} of {total}",
    scanOverBudgetMark: "\u00b7 {n} switch(es) ran out of time",
    scanOverBudgetHint:
      "These switches did not finish answering inside their poll "
      + "budget, so their part of the map is from an earlier scan: "
      + "{switches}. They are not unreachable \u2014 they answer, only "
      + "too slowly \u2014 and no alarm is raised for them. Their cards "
      + "say when their data was taken.",
    serviceOffline: "No connection to the service · data from {time}",
    serviceOfflineHint:
      "The page is not getting answers from MoonLan \u2014 it may be "
      + "restarting, or this machine has lost the route to it. The map "
      + "is the last picture that arrived, and it is not being "
      + "refreshed. This says nothing about the switches: they are not "
      + "being polled from here right now, that is all. Polling "
      + "continues, and the notice clears by itself.",
    overBudgetHint:
      "This switch did not finish answering inside its poll budget, so "
      + "everything on this card is the last reading that did arrive, "
      + "taken at {time}. That is not the same as the switch being "
      + "unreachable \u2014 it answers, only too slowly \u2014 and no "
      + "\u201cswitch down\u201d alarm is raised for it. Give it more "
      + "time or a shorter SNMP timeout of its own: see "
      + "snmp.host_budget_seconds in config.yaml.",
    portFlapping: "flapping",
    portFlappingHint:
      "The port has changed link state {n} time(s) inside the window; "
      + "the last transition was {when}. A cable, a connector, a dying "
      + "transceiver — or someone unplugging it.",
    lagAggregate: "Aggregate (LACP)",
    portsOf: "Ports of {name}",
    lacp: "LACP",
    lagTrunk: "LAG trunk",
    gbps: "Gbit/s",
    mbps: "Mbit/s",
    portsBtn: "Ports",
    portsTitle: "Ports — {name}",
    colPort: "Port",
    colSpeed: "Speed",
    colIn: "In, Mbit/s",
    colOut: "Out, Mbit/s",
    colErr: "Err/min",
    colDisc: "Disc/min",
    errTooltip:
      "Damaged frames: bad cable or patch cord, duplex mismatch, a dying "
      + "transceiver. Rare on healthy hardware — worth looking into.",
    discTooltip:
      "Frames dropped on purpose or for lack of buffer: VLAN filtering, "
      + "storm control, traffic bursts. Usually normal.",
    openPortsLink: "Open switch ports",
    alarmsBtn: "Alarms",
    alarmsTitle: "Alarms",
    alarmsActive: "Active",
    alarmsCleared: "Recently cleared",
    noAlarms: "None",
    al_host_down: "Host down",
    al_switch_down: "Switch down",
    al_port_errors: "Port errors",
    al_port_discards: "Port discards",
    al_port_util: "High port load",
    al_new_mac: "New device",
    clearedAt: "cleared ",
    durS: "s",
    durM: "m",
    durH: "h",
    monitorBtn: "Monitor",
    al_port_hosts_down: "Mass host outage",
    al_lag_degraded: "LAG degraded",
    al_port_frame_corruption: "Frame corruption",
    suspectMacs: "Suspected frame corruption: {n} addresses",
    suspectMacsHint:
      "The MAC table of this port holds addresses a few bits away from "
      + "a real one: the switch learned them from damaged frames. Check "
      + "the cable, the patch cord and the port itself.",
    lagMembersShort: "({active}/{total} members)",
    clearBtn: "Clear",
    clearConfirm: "Clear this alarm?",
    flapChip: "FLAP",
    flapTooltip: "Notifications muted: {n} raises in {h} h",
    lastRaiseLabel: "last raise",
    lastSeenLabel: "Last seen",
    lastArpLabel: "Last ARP entry",
    ipConfirmedLabel: "IP confirmed (ARP)",
    ipNotConfirmed: "not confirmed",
    randomMac: "random MAC",
    randomMacHint:
      "A locally administered address. Phones and laptops randomize it "
      + "per network, so such devices keep reappearing under new MACs.",
    staleButAliveHint:
      "The MAC is missing from the switch tables, but the address is "
      + "answering: the device may have changed its MAC or it sits "
      + "behind equipment MoonLan does not poll.",
    approximate: "approximate",
    approximateHint:
      "Approximate location: the MAC address is visible only on trunk "
      + "ports, so the device sits behind a switch MoonLan does not poll. "
      + "It is drawn on the trunk it was seen through.",
    approximateWhy:
      "Seen on the downlink trunk {switch} {port} — the side of that "
      + "cable that leads away from the root, which is why the device "
      + "is drawn behind it.",
    uplinkOnlyHint:
      "Seen only on uplink ports, so the exact place is unknown. A MAC "
      + "address on an uplink says the device is on the far side of "
      + "that cable — not behind the switch that reported it — so "
      + "nothing here can be drawn.",
    uplinkOnlySeenOn: "Seen on",
    ev_ip_released: "IP address released",
    staleHint:
      "The MAC address is missing from the current switch tables — " +
      "the device is shown at the port where it was seen last.",
    unlocatedHeader: "Not on map",
    unlocatedHint:
      "The device is known from ARP but was not found on any port of " +
      "the polled switches — it is most likely behind a router or an " +
      "unpolled switch.",
    offMapHint:
      "The MAC address has not shown up in any switch table for a " +
      "while; the device stays in the inventory until the retention " +
      "window ends.",
    ev_hosts_purged: "Old hosts removed",
    ev_link_dropped: "Link withdrawn",
    ev_layout_saved: "Layout saved",
    ev_layout_cleared: "Layout reset",
    ev_layout_pinned: "Placed by hand",
    ev_layout_released: "Released",
    evNodes: "{n}: {names}",
    evNodesMore: "{n}: {names} and {more} more",
    offlineGroup: "Offline",
    offlineGroupTitle: "Offline devices · {n}",
    offlineGroupHint:
      "These devices are missing from the current switch tables; they "
      + "are shown on the port where they were seen last.",
    trunkGroup: "Beyond the trunk",
    trunkGroupTitle: "Beyond the trunk · {n}",
    trunkGroupHint:
      "Visible through the trunk {switch} {port}. The exact place is "
      + "unknown: the devices are somewhere past that cable.",
    trunkGroupSilent: "Of those, not answering",
  },
  ru: {
    title: "MoonLan — карта сети",
    tagline: "карта локальной сети",
    journalBtn: "Журнал",
    freezeBtn: "Заморозить раскладку",
    layoutPinnedMark: "поставлено руками: {n}",
    layoutPinnedHint:
      "Столько узлов стоят там, куда их поставил человек, и движку "
      + "раскладки недоступны. Всё остальное раскладывается само, и "
      + "куда оно встало — запоминается само же: сохранять руками "
      + "нечего. Чтобы поставить узел, включите «Расстановку» или "
      + "нажмите P на выделенном; тем же способом он и отпускается.",
    menuPin: "Поставить здесь",
    menuUnpin: "Отпустить",
    menuOpenCard: "Открыть карточку",
    menuOpenPorts: "Порты",
    menuSelected: "Выбрано узлов: {n}",
    menuPing: "Ping",
    menuTraceroute: "Traceroute",
    reasonNo_ip: "IP неизвестен",
    reasonNoService: "нет связи с сервисом",
    reasonNoPing: "на сервере нет ping",
    reasonNoTraceroute: "на сервере нет traceroute",
    actionStarting: "Запуск…",
    actionRunning: "Выполняется на сервере MoonLan…",
    actionDone: "Готово",
    actionSimulated: "Демо-режим: в сеть ничего не отправляется, результат имитирован.",
    actionTarget: "Цель",
    actionThisRun: "Этот запуск: 4 пакета с сервера MoonLan",
    actionMonitor: "Непрерывный мониторинг",
    actionTool: "Утилита",
    pingLoss: "потери {loss}% (ответов {received} из {sent})",
    pingRtt: "RTT мин / сред / макс",
    ms: "мс",
    monitorNone: "не опрашивается",
    monitorUp: "отвечает; последний ответ {time}",
    monitorDown: "не отвечает; последний ответ {time}",
    monitorNever: "не отвечает; ответа не было ни разу",
    verdictOk: "отвечает",
    verdictPartial: "с потерями",
    verdictNoReply: "нет ответа",
    verdictNoIp: "IP неизвестен",
    verdictTimeout: "превышено время",
    verdictFailed: "ошибка",
    refuseBusy: "На сервере уже идёт {limit} проверок — это потолок (context_menu.max_running). Повторите через несколько секунд.",
    refuseNoTool: "На сервере MoonLan нет {tool}.",
    refuseAddress: "Сервер принимает идентификаторы узлов, а не адреса: {list}",
    refuseUnknown: "Сервер не знает таких узлов — возможно, карта старше его последнего опроса; обновите страницу: {list}",
    refuseJobGone: "Этот результат на сервере больше не хранится.",
    refuseOther: "Сервер отказал: {error}",
    arrangeBtn: "Расстановка",
    arrangeOnBtn: "Расставляю",
    arrangeHint:
      "Режим расстановки включён: перетаскивание ставит узел и "
      + "оставляет его там, правая кнопка отпускает обратно. Вне "
      + "режима перетаскивание только двигает вид карты.",
    resetLayoutBtn: "Сбросить раскладку",
    pinnedMark: "\ud83d\udccc",
    pinnedHint:
      "Этот узел стоит там, куда его поставил человек, а не там, куда "
      + "его положил бы алгоритм раскладки. Он выведен из-под физики и "
      + "остаётся здесь для всех, кто смотрит на эту карту, — и после "
      + "перезагрузки страницы, и после перезапуска сервиса.",
    unpinBtn: "Открепить узел",
    unpinConfirm: "Открепить «{node}» и вернуть его под раскладку?",
    resetLayoutConfirm:
      "Забыть все сохранённые позиции — для всех? Карта разложится "
      + "заново, а узлы, поставленные руками, потеряют свои места. "
      + "Отменить это будет нельзя.",
    unfreezeBtn: "Разморозить",
    rescanBtn: "Опросить сеть",
    searchPlaceholder: "Поиск: имя, IP или MAC…",
    switchesHeader: "Коммутаторы",
    devicesHeader: "Устройства",
    journalTitle: "Журнал событий",
    noEvents: "Событий пока нет",
    emptyLine1: "Схема пока пуста.",
    emptyLine2:
      "Укажите коммутаторы в <code>config.yaml</code> и нажмите " +
      "«Опросить сеть», либо запустите сервис с " +
      "<code>MOONLAN_DEMO=1</code>, чтобы увидеть демо-сеть.",
    scanning: "Идёт опрос сети…",
    scanPrefix: "Опрос: ",
    scanFailed: "Последний опрос не удался: ",
    noData: "Данных пока нет",
    close: "Закрыть",
    ev_new_mac: "Новое устройство",
    ev_host_down: "Хост недоступен",
    ev_host_up: "Хост снова в сети",
    ev_alarm_raised: "Тревога поднята",
    ev_alarm_cleared: "Тревога снята",
    name: "Имя",
    ipAddr: "IP-адрес",
    macAddr: "MAC-адрес",
    bridgeMac: "MAC моста",
    switchLabel: "Коммутатор",
    portLabel: "Порт",
    vlan: "VLAN",
    lastReply: "Отвечал",
    firstSeen: "Впервые замечен",
    portsUpTotal: "Порты (активно/всего)",
    descr: "Описание",
    speed: "Скорость",
    pseudoTitle: "Коммутатор без SNMP",
    pseudoHint:
      "За этим портом виден неуправляемый коммутатор или точка доступа " +
      "({n} устройств).",
    devicesBehindPort: "Устройств за портом",
    devicesCount: "{n} (сейчас активно {live})",
    portOnSide: "Порт со стороны {name}",
    linkSource: "Источник",
    srcLldp: "LLDP",
    srcFdb: "таблицы MAC",
    srcBoth: "LLDP и таблицы MAC",
    srcHintLldp:
      "Устройства объявляют друг друга по LLDP: связь и порты с обеих "
      + "сторон — их собственные слова, а не вывод алгоритма.",
    srcHintFdb:
      "Связь выведена из таблиц MAC-адресов. По LLDP устройства друг "
      + "друга не объявляют (LLDP выключен или кадры не доходят), "
      + "поэтому порты — наилучшее предположение.",
    chassisId: "Chassis ID",
    mgmtIp: "Управляющий адрес",
    remotePort: "Порт со стороны соседа",
    capabilities: "Возможности",
    capsUnknown: "не объявлены",
    bridgeHint:
      "LLDP сообщает, что за этим портом стоит мост — коммутатор, "
      + "который MoonLan не опрашивает. В конфигурации его нет; "
      + "устройства за ним показаны через этот узел.",
    lldpUnknownHint:
      "Устройство LLDP, которое не объявляет своих возможностей "
      + "(необязательные TLV на нём выключены). Отсутствие признака "
      + "bridge ничего не говорит о том, что это за устройство, поэтому "
      + "своего узла оно не получает и тревог не поднимает — его видно "
      + "в карточке порта.",
    al_unmanaged_bridge_detected: "Неучтённый мост",
    bridgeSharesPortHint:
      "За этим портом отвечают несколько LLDP-устройств — значит, на "
      + "кабеле стоит неуправляемый коммутатор, а они подключены за ним. "
      + "Устройства порта остаются за этим коммутатором: за каким именно "
      + "мостом каждое из них — данных нет.",
    lldpLabel: "LLDP",
    lldpUnidentified: "неопознанное устройство LLDP",
    lldpCrowded: "несколько устройств LLDP",
    lldpCrowdedHint:
      "За портом отвечают несколько LLDP-устройств — значит, на кабеле "
      + "стоит неуправляемый коммутатор. Все они настоящие и все "
      + "показаны, но какое именно на кабеле — определить нельзя, "
      + "поэтому связь по этому порту не строится.",
    portLabelName: "Подпись порта, заданная на коммутаторе (LLDP)",
    andMore: "и ещё {n}",
    sameDevice: "Тот же аппарат, что",
    kind_router: "маршрутизатор",
    kind_access_point: "точка доступа",
    kind_phone: "телефон",
    kind_bridge: "мост",
    kind_repeater: "повторитель",
    kind_station: "оконечное устройство",
    kind_other: "устройство",
    kind_unknown: "неопознанное устройство LLDP",
    nameFromLldpHint:
      "Подпись взята из LLDP: устройство назвало себя само, но адреса "
      + "ему не дала ни одна ARP-таблица, и обратный DNS о нём не знает. "
      + "Маршрутизаторы сознательно не входят в инвентарь хостов.",
    externalNetwork: "Внешняя сеть",
    externalPort: "внешний аплинк",
    externalHint:
      "Этот порт ведёт наружу — стык с провайдером или аплинк к чужому "
      + "оборудованию (config.uplink_ports). То, что за ним, собрано в "
      + "один узел вместо россыпи устройств, и коммутаторы там не "
      + "поднимают тревог: это граница сети, а не то, что кто-то забыл "
      + "настроить.",
    bridgeAssumedHint:
      "Предположительно мост — это вывод, а не заявление самого "
      + "устройства. Коммутатор не сообщает LLDP-возможности ни одного "
      + "соседа, поэтому пустое поле ни о чём не говорит; мостом "
      + "устройство сочтено потому, что объявило системное имя и "
      + "управляющий адрес. По той же причине тревога по нему — info.",
    routerAddr: "Адрес маршрутизатора (routers:)",
    uplinkHintChip: "выход наружу?",
    uplinkWhy_public_address: "Устройство за этим портом отвечает по {ip}.",
    uplinkWhy_foreign_subnet:
      "{name} управляется по {ip} — в подсети, которой MoonLan больше "
      + "нигде не видит, и ничего нашего за этим портом не отвечает.",
    uplinkHint:
      "Похоже на выход из сети. Добавьте «{switch}:{port}» в "
      + "uplink_ports в config.yaml — и устройства за этим портом "
      + "соберутся в узел «Внешняя сеть», а тревоги о мостах там "
      + "подниматься не будут.",
    colNoAnswer:
      "Коммутатор не ответил по этому счётчику ({oids}), поэтому в "
      + "колонке \u2014, а не 0. Неизвестно — это не ноль: тревоги по "
      + "такому значению не поднимаются и не снимаются.",
    remembered: "по инвентарю",
    fromSaved: "по сохранённым данным",
    readingFrom: "Показание снято",
    hostFromSavedHint:
      "{switch} не уложился в бюджет опроса, поэтому устройство "
      + "нарисовано там, куда его поместило последнее полученное "
      + "показание, снятое {time}. С тех пор его никто не искал: "
      + "«последний раз виден» выше — это когда его действительно "
      + "нашли, и пока так продолжается, это время не двигается. "
      + "Кабели не переключаются каждые десять минут, поэтому место "
      + "по-прежнему показано, — но копия показания не наблюдение.",
    rememberedHint:
      "Сейчас MAC виден только на магистральных портах, но в инвентаре "
      + "известен порт, на котором это устройство стоит, — поэтому оно "
      + "показано там, а не угадано на магистрали.",
    offlineGroupGuessHint:
      "Устройства не найдены в текущих таблицах коммутаторов, а "
      + "показанный порт — магистральный: устройствами их там никогда не "
      + "видели, так что это унаследованная догадка, а не запись о том, "
      + "где их видели в последний раз.",
    behindBridge: "За устройством",
    colPartial:
      "Коммутатор отвечал по этому счётчику ({oids}) и замолчал на "
      + "середине таблицы; {filled} порт(ов) дочитано поштучно из "
      + "32-битного счётчика. Там, где всё ещё стоит \u2014, значение "
      + "неизвестно, а не равно нулю.",
    lldpNeighbour: "Сосед по LLDP",
    lldpForwarded: "пересылка LLDP",
    lldpForwardedHint:
      "Эти LLDP-кадры пришли не с кабеля: тот же сосед виден на другом "
      + "порту либо его MAC стоит в таблице коммутатора за другим "
      + "портом. Вероятно, включена пересылка LLDP-кадров. Данные "
      + "ненадёжны и для построения связей не используются.",
    stpBtn: "STP",
    stpTitle: "Остовное дерево (STP)",
    stpHint:
      "Коммутатор с выключенным STP всё равно отвечает на все объекты "
      + "dot1dStp*: приоритет 0, стоимость 0 и он сам в роли корня. "
      + "Поэтому корень, стоимость и корневой порт показываются только "
      + "там, где дерево доказуемо работает: есть включённые и не "
      + "выключенные порты и реально произошедшее изменение топологии.",
    stpState: "Состояние",
    stpStateOff: "не работает",
    stpStateMember: "участвует",
    stpStateRoot: "корень",
    stpPriority: "Приоритет",
    stpRoot: "Корень",
    stpCost: "Стоимость до корня",
    stpRootPort: "Корневой порт",
    stpChanges: "Изменений топологии",
    stpLastChange: "С последнего",
    stpRootMark: "корень STP",
    stpBlocking: "BLOCKING",
    linkOrderUnknown:
      "Порядок коммутаторов за этим портом установить не удалось: их "
      + "таблицы MAC не видят друг друга, и ни один не сообщает о другом "
      + "по LLDP. Все они нарисованы на ближайшем известном коммутаторе "
      + "— через него они достижимы, но не обязательно в него включены. "
      + "Пунктир означает догадку о порядке, а не измеренный кабель.",
    linkCycleUnresolved:
      "Эта связь входит в кольцо между опрашиваемыми коммутаторами, в "
      + "котором ни один порт не заблокирован остовным деревом. Одна из "
      + "его связей неверна, а какая именно — из данных не следует, "
      + "поэтому не снято ничего, а помечены все: кольцо, которое не "
      + "удалось объяснить, не должно выглядеть как факт.",
    linkStpBlocking:
      "Остовное дерево держит порт этой связи в блокировке, и трафика по "
      + "ней нет. Кольцо с заблокированным портом — это то, как выглядит "
      + "исправная сеть с физическим кольцом.",
    switchLinks: "Связи ({n})",
    stpRootless:
      "Отвечают на dot1dStp*, но корня не называют: {switches}. Таблицы "
      + "портов у них настоящие, а объекты, где лежит корень, они не "
      + "реализуют — так делает, например, RouterOS. Мост, участвующий "
      + "в дереве, свой корень знает, поэтому отдельным деревом они не "
      + "считаются и фрагментации не создают.",
    stpBlockingHint:
      "Остовное дерево держит этот порт в состоянии blocking: связь "
      + "есть, но пользовательский трафик через неё не идёт. Это "
      + "резервный путь избыточной пары.",
    stpVerdictNone: "STP в этой сети не работает",
    stpVerdictSingle: "Единое дерево, корень {root}",
    stpVerdictFragmented: "Дерево фрагментировано: {n} корней",
    stpFragmentedVlanHint:
      "Несколько корней при физически связной сети — не обязательно "
      + "поломка. BPDU нетегированы и обрабатываются в VLAN того "
      + "порта, куда пришли, поэтому сегменты, чьи магистральные порты "
      + "лежат в разных VLAN, законно образуют разные деревья и "
      + "никогда не встречаются. Прежде чем считать это поломкой, "
      + "проверьте принадлежность магистральных портов к VLAN.",
    stpBridgeIdFixed:
      "Этот коммутатор отдаёт Bridge ID с приоритетом в младшем байте, "
      + "тогда как по стандарту он в старшем: его собственное значение "
      + "в 256 раз меньше. MoonLan показывает исправленное — поэтому "
      + "оно отличается от того, что пишет веб-интерфейс коммутатора.",
    al_stp_root_changed: "Смена корня STP",
    al_stp_topology_change: "Изменения топологии STP",
    al_stp_fragmented: "Фрагментация STP",
    al_port_flapping: "Флаппинг порта",
    al_loop_detected: "Петля на порту",
    al_loop_detection_disabled: "Loop Detection выключен",
    colLoop: "Петля",
    loopOk: "норма",
    loopYes: "ПЕТЛЯ",
    loopNoData: "нет данных",
    loopOff: "\u2014",
    loopRawHint:
      "Loop Detection сообщает по этому порту значение {raw}. Для этой "
      + "модели достоверно известно только значение «петли нет», "
      + "поэтому всё остальное считается петлёй \u2014 сырое значение "
      + "попадает в тревогу, и первая же настоящая петля покажет, чем "
      + "она отличается.",
    loopOffHint:
      "Loop Detection на этом порту выключен на самом коммутаторе: "
      + "здесь ничего не отслеживается.",
    loopNoDataHint:
      "Состояние по этому порту не пришло. Это «неизвестно», а не "
      + "«петли нет».",
    loopMark: "ПЕТЛЯ",
    loopDetection: "Loop Detection",
    loopCardOn: "включён, интервал {interval} с, восстановление {recover} с",
    loopCardOff: "выключен на коммутаторе",
    loopCardUnknown: "состояние неизвестно \u2014 коммутатор его не сообщил",
    loopCardLoop: "ПЕТЛЯ на {ports}",
    loopCardUnsupported: "модель не сообщает по SNMP",
    loopCardNotPolled: "данных пока нет \u2014 цикл счётчиков после скана ещё не отработал",
    loopCardPartial: "ответ неполный \u2014 часть портов неизвестна",
    loopUnsupportedHint:
      "Эта модель не отдаёт состояние Loop Detection ни в одной MIB, "
      + "которую MoonLan умеет читать, поэтому о ней не утверждается ни "
      + "«петля есть», ни «петли нет». Её sysObjectID \u2014 {oid}; как "
      + "обойти приватную ветку и добавить профиль в config.yaml, "
      + "написано в README, раздел «Loop Detection».",
    loopProfileHint: "профиль {profile}, опознан по {how}",
    dataFrom: "Данные от",
    staleSwitchScans: "{n} сканов подряд без полного опроса",
    staleSwitchHint:
      "Коммутатор отвечает, но не завершил полный опрос уже {n} сканов "
      + "подряд, поэтому всё на этой карточке — от {time}. Он доступен, "
      + "и тревога «коммутатор недоступен» по нему не поднимается, — но "
      + "его показания перестали меняться. Бюджет опроса для него мал: "
      + "дайте ему больше времени или свой, более короткий таймаут SNMP, "
      + "чтобы запросы, на которые он не ответит, бросали раньше. См. "
      + "switches: и snmp.host_budget_seconds в config.yaml.",
    al_switch_stale: "Данные коммутатора не обновляются",
    ageUnderMinute: "меньше минуты назад",
    ageMinutes: "{n} мин назад",
    ageHours: "{h} ч назад",
    ageHoursMinutes: "{h} ч {n} мин назад",
    rateMeasured:
      "Измерено {when}. С тех пор цикл счётчиков до этого коммутатора не "
      + "добрался — его опрашивает скан, либо он отвечает медленно. "
      + "Значение настоящее, просто не сиюминутное.",
    rateTooOld:
      "Последний раз измерено {when}. Это слишком давно, чтобы "
      + "показывать как скорость, поэтому ячейка пуста, — но порт "
      + "измеряли, чего никогда не скажет порт, который не измеряли ни "
      + "разу. См. stale_rate_hide_minutes в config.yaml.",
    scanningProgress: "Идёт опрос: {done} из {total}",
    scanOverBudgetMark: "\u00b7 не уложились: {n}",
    scanOverBudgetHint:
      "Эти коммутаторы не успели ответить целиком за отведённый бюджет "
      + "опроса, поэтому их часть карты \u2014 с прошлого скана: "
      + "{switches}. Недоступными они не считаются: они отвечают, просто "
      + "медленно, и тревог по ним не поднимается. Время, на которое "
      + "сняты их данные, написано в карточке каждого.",
    serviceOffline: "Нет связи с сервисом · данные от {time}",
    serviceOfflineHint:
      "Страница не получает ответов от MoonLan — возможно, он "
      + "перезапускается или до него пропал маршрут с этой машины. На "
      + "карте последняя пришедшая картина, и она не обновляется. О "
      + "коммутаторах это не говорит ничего: просто их сейчас отсюда не "
      + "опрашивают. Опрос продолжается, и пометка снимется сама.",
    overBudgetHint:
      "Коммутатор не уложился в бюджет опроса, поэтому всё на этой "
      + "карточке \u2014 последнее полученное измерение, снятое {time}. "
      + "Это не то же самое, что «нет ответа»: он отвечает, просто "
      + "слишком медленно, и тревога «коммутатор недоступен» по нему не "
      + "поднимается. Дайте ему больше времени или свой, более короткий "
      + "таймаут SNMP \u2014 см. snmp.host_budget_seconds в config.yaml.",
    portFlapping: "флаппинг",
    portFlappingHint:
      "Порт менял состояние линка {n} раз(а) за окно; последний переход "
      + "— {when}. Кабель, разъём, умирающий трансивер — или кто-то "
      + "выдёргивает шнур.",
    lagAggregate: "Агрегат (LACP)",
    portsOf: "Порты {name}",
    lacp: "LACP",
    lagTrunk: "LAG-транк",
    gbps: "Гбит/с",
    mbps: "Мбит/с",
    portsBtn: "Порты",
    portsTitle: "Порты — {name}",
    colPort: "Порт",
    colSpeed: "Скорость",
    colIn: "Вх., Мбит/с",
    colOut: "Исх., Мбит/с",
    colErr: "Ошиб/мин",
    colDisc: "Отбр/мин",
    errTooltip:
      "Испорченные кадры: плохой кабель или патч-корд, рассогласование "
      + "дуплекса, умирающий трансивер. На исправном оборудовании редки — "
      + "стоит разобраться.",
    discTooltip:
      "Кадры, отброшенные намеренно или из-за нехватки буфера: фильтрация "
      + "VLAN, storm control, всплески трафика. Обычно норма.",
    openPortsLink: "Открыть порты коммутатора",
    alarmsBtn: "Тревоги",
    alarmsTitle: "Тревоги",
    alarmsActive: "Активные",
    alarmsCleared: "Недавно снятые",
    noAlarms: "Нет",
    al_host_down: "Хост недоступен",
    al_switch_down: "Коммутатор недоступен",
    al_port_errors: "Ошибки на порту",
    al_port_discards: "Отбрасывания на порту",
    al_port_util: "Высокая загрузка порта",
    al_new_mac: "Новое устройство",
    clearedAt: "снята ",
    durS: "с",
    durM: "м",
    durH: "ч",
    monitorBtn: "Наблюдать",
    al_port_hosts_down: "Массовое отключение хостов",
    al_lag_degraded: "Деградация LAG",
    al_port_frame_corruption: "Повреждение кадров",
    suspectMacs: "Подозрение на повреждение кадров: {n} адресов",
    suspectMacsHint:
      "В таблице MAC этого порта есть адреса, отличающиеся от реального "
      + "на несколько бит: коммутатор выучил их из повреждённых кадров. "
      + "Проверьте кабель, патч-корд и сам порт.",
    lagMembersShort: "({active}/{total})",
    clearBtn: "Снять",
    clearConfirm: "Снять эту тревогу?",
    flapChip: "FLAP",
    flapTooltip: "Уведомления приглушены: {n} подъёмов за {h} ч",
    lastRaiseLabel: "последний подъём",
    lastSeenLabel: "Последний раз виден",
    lastArpLabel: "Последняя ARP-запись",
    ipConfirmedLabel: "IP подтверждён (ARP)",
    ipNotConfirmed: "не подтверждён",
    randomMac: "случайный MAC",
    randomMacHint:
      "Локально администрируемый адрес. Телефоны и ноутбуки меняют его "
      + "для каждой сети, поэтому такие устройства появляются снова и "
      + "снова под новыми MAC.",
    staleButAliveHint:
      "MAC отсутствует в таблицах коммутаторов, но адрес активен: "
      + "устройство могло сменить MAC или подключено за необслуживаемым "
      + "устройством.",
    approximate: "приблизительно",
    approximateHint:
      "Расположение приблизительное: MAC виден только на магистральных "
      + "портах — устройство подключено за неопрашиваемым коммутатором. "
      + "Показано на магистрали, через которую его видно.",
    approximateWhy:
      "Виден на нисходящей магистрали {switch} {port} — на той стороне "
      + "кабеля, что ведёт от корня, поэтому устройство и нарисовано "
      + "за ней.",
    uplinkOnlyHint:
      "Виден только на приходящих портах, точное место неизвестно. MAC "
      + "на аплинке говорит, что устройство по ту сторону кабеля, а не "
      + "за коммутатором, который его увидел, — рисовать тут нечего.",
    uplinkOnlySeenOn: "Виден на",
    ev_ip_released: "IP-адрес освобождён",
    staleHint:
      "MAC-адреса нет в текущих таблицах коммутаторов — устройство " +
      "показано на порту, где оно было замечено в последний раз.",
    unlocatedHeader: "Вне карты",
    unlocatedHint:
      "Устройство известно по ARP, но не найдено на портах опрашиваемых " +
      "коммутаторов — вероятно, находится за маршрутизатором или " +
      "неопрашиваемым коммутатором.",
    offMapHint:
      "MAC-адрес давно не появлялся в таблицах коммутаторов; устройство " +
      "остаётся в инвентаре до истечения срока хранения.",
    ev_hosts_purged: "Удалены старые хосты",
    ev_link_dropped: "Связь снята",
    ev_layout_saved: "Раскладка сохранена",
    ev_layout_cleared: "Раскладка сброшена",
    ev_layout_pinned: "Поставлено руками",
    ev_layout_released: "Отпущено",
    evNodes: "{n}: {names}",
    evNodesMore: "{n}: {names} и ещё {more}",
    offlineGroup: "Офлайн",
    offlineGroupTitle: "Офлайн-устройства · {n}",
    offlineGroupHint:
      "Устройства не найдены в текущих таблицах коммутаторов; показаны "
      + "на порту, где были замечены в последний раз.",
    trunkGroup: "За магистралью",
    trunkGroupTitle: "За магистралью · {n}",
    trunkGroupHint:
      "Видно через магистраль {switch} {port}. Точное место "
      + "неизвестно: устройства где-то за этим кабелем.",
    trunkGroupSilent: "Из них не отвечают",
  },
};
