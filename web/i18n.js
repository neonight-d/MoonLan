/* MoonLan UI strings (en / ru).
   This is the only file in web/ that is allowed to contain Russian text.
   Placeholders like {name} and {n} are substituted by fmt() in app.js. */

const I18N = {
  en: {
    title: "MoonLan — network map",
    tagline: "local network map",
    journalBtn: "Journal",
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
    lagMembersShort: "({active}/{total} members)",
    clearBtn: "Clear",
    clearConfirm: "Clear this alarm?",
    flapChip: "FLAP",
    flapTooltip: "Notifications muted: {n} raises in {h} h",
    lastRaiseLabel: "last raise",
    lastSeenLabel: "Last seen",
    lastArpLabel: "Last ARP entry",
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
    showDevices: "Show devices",
    hideDevices: "Collapse",
  },
  ru: {
    title: "MoonLan — карта сети",
    tagline: "карта локальной сети",
    journalBtn: "Журнал",
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
    lagMembersShort: "({active}/{total})",
    clearBtn: "Снять",
    clearConfirm: "Снять эту тревогу?",
    flapChip: "FLAP",
    flapTooltip: "Уведомления приглушены: {n} подъёмов за {h} ч",
    lastRaiseLabel: "последний подъём",
    lastSeenLabel: "Последний раз виден",
    lastArpLabel: "Последняя ARP-запись",
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
    showDevices: "Показать устройства",
    hideDevices: "Свернуть",
  },
};
