/* MoonLan UI strings (en / ru).
   This is the only file in web/ that is allowed to contain Russian text.
   Placeholders like {name} and {n} are substituted by fmt() in app.js. */

const I18N = {
  en: {
    title: "MoonLan — network map",
    tagline: "local network map",
    journalBtn: "Journal",
    freezeBtn: "Freeze layout",
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
    stpBlockingHint:
      "Spanning tree holds this port in the blocking state: the link "
      + "exists but carries no user traffic. It is the standby path of "
      + "a redundant pair.",
    stpVerdictNone: "STP is not running anywhere in this network",
    stpVerdictSingle: "One spanning tree, root {root}",
    stpVerdictFragmented: "The tree is fragmented: {n} separate roots",
    al_stp_root_changed: "STP root changed",
    al_stp_topology_change: "STP topology changes",
    al_stp_fragmented: "STP fragmented",
    al_port_flapping: "Port flapping",
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
    offlineGroup: "Offline",
    offlineGroupTitle: "Offline devices · {n}",
    offlineGroupHint:
      "These devices are missing from the current switch tables; they "
      + "are shown on the port where they were seen last.",
  },
  ru: {
    title: "MoonLan — карта сети",
    tagline: "карта локальной сети",
    journalBtn: "Журнал",
    freezeBtn: "Заморозить раскладку",
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
    stpBlockingHint:
      "Остовное дерево держит этот порт в состоянии blocking: связь "
      + "есть, но пользовательский трафик через неё не идёт. Это "
      + "резервный путь избыточной пары.",
    stpVerdictNone: "STP в этой сети не работает",
    stpVerdictSingle: "Единое дерево, корень {root}",
    stpVerdictFragmented: "Дерево фрагментировано: {n} корней",
    al_stp_root_changed: "Смена корня STP",
    al_stp_topology_change: "Изменения топологии STP",
    al_stp_fragmented: "Фрагментация STP",
    al_port_flapping: "Флаппинг порта",
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
    offlineGroup: "Офлайн",
    offlineGroupTitle: "Офлайн-устройства · {n}",
    offlineGroupHint:
      "Устройства не найдены в текущих таблицах коммутаторов; показаны "
      + "на порту, где были замечены в последний раз.",
  },
};
