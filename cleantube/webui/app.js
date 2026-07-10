"use strict";

/* ---------- helpers ---------- */

const $ = (sel, root = document) => root.querySelector(sel);
const nf = new Intl.NumberFormat("nl-BE");
const nf1 = new Intl.NumberFormat("nl-BE", { maximumFractionDigits: 1 });

// Alle dynamische tekst (titels, foutmeldingen) gaat via textContent.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
}

function fmtBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let value = bytes, i = 0;
  while (value >= 1000 && i < units.length - 1) { value /= 1000; i += 1; }
  return `${nf1.format(value)} ${units[i]}`;
}

function fmtDur(seconds) {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} d ${h} u`;
  if (h > 0) return `${h} u ${m} min`;
  if (m > 0) return `${m} min ${s % 60} s`;
  return `${s} s`;
}

function fmtDT(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("nl-BE", {
    dateStyle: "medium", timeStyle: "short",
  });
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString("nl-BE", {
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtRel(iso, withSuffix = true) {
  if (!iso) return "—";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  let text;
  if (abs < 45) text = "enkele seconden";
  else if (abs < 3600) text = `${Math.round(abs / 60)} min`;
  else if (abs < 86400) {
    const h = Math.floor(abs / 3600), m = Math.round((abs % 3600) / 60);
    text = m ? `${h} u ${m} min` : `${h} u`;
  } else {
    const days = nf1.format(abs / 86400);
    text = days === "1" ? "1 dag" : `${days} dagen`;
  }
  if (!withSuffix) return text;
  return diff < 0 ? `${text} geleden` : `over ${text}`;
}

function dayLabel(iso) {
  const date = new Date(iso);
  const today = new Date();
  const other = new Date(today);
  const diffDays = Math.round(
    (startOfDay(date) - startOfDay(today)) / 86400000
  );
  if (diffDays === 0) return "vandaag";
  if (diffDays === -1) return "gisteren";
  if (diffDays === 1) return "morgen";
  void other;
  return date.toLocaleDateString("nl-BE", {
    weekday: "short", day: "numeric", month: "long",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

function startOfDay(date) {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return copy.getTime();
}

function thumbUrl(videoId, quality = "mqdefault") {
  return `https://i.ytimg.com/vi/${encodeURIComponent(videoId)}/${quality}.jpg`;
}

async function fetchJSON(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).error || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}

const STATUS_NL = {
  downloaded: ["Gedownload", "good"],
  pending: ["In wachtrij", "accent"],
  permanently_failed: ["Definitief gefaald", "critical"],
  skipped: ["Overgeslagen (baseline)", "muted"],
};

/* ---------- tooltip ---------- */

const tooltip = $("#tooltip");

function showTooltip(anchorRect, lines) {
  tooltip.replaceChildren(
    ...lines.map(([cls, text]) => el("div", { class: cls }, text))
  );
  tooltip.hidden = false;
  const box = tooltip.getBoundingClientRect();
  let x = anchorRect.left + anchorRect.width / 2 - box.width / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - box.width - 8));
  let y = anchorRect.top - box.height - 8;
  if (y < 8) y = anchorRect.bottom + 8;
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}

function hideTooltip() { tooltip.hidden = true; }

/* ---------- router ---------- */

const panels = {
  dashboard: $("#panel-dashboard"),
  dagboek: $("#panel-dagboek"),
  video: $("#panel-video"),
};

let activeTab = null;

function route() {
  const hash = location.hash.slice(1) || "dashboard";
  const [name, arg] = hash.split("/", 2);
  const tab = panels[name] ? name : "dashboard";
  activeTab = tab;
  for (const [key, panel] of Object.entries(panels)) {
    panel.hidden = key !== tab;
    $(`#tab-${key}`).classList.toggle("active", key === tab);
  }
  hideTooltip();
  if (tab === "dashboard") loadDashboard();
  else if (tab === "dagboek") loadTimeline();
  else loadVideo(arg || null);
}

window.addEventListener("hashchange", route);

/* ---------- dashboard ---------- */

const refreshStatus = $("#refresh-status");
let dashboardLoading = false;

async function loadDashboard(silent = false) {
  if (dashboardLoading) return;
  dashboardLoading = true;
  try {
    const data = await fetchJSON("/api/dashboard");
    renderDashboard(data);
    refreshStatus.textContent =
      `bijgewerkt om ${new Date().toLocaleTimeString("nl-BE")}`;
  } catch (err) {
    refreshStatus.textContent = `geen verbinding (${err.message})`;
    if (!silent && !panels.dashboard.hasChildNodes()) {
      panels.dashboard.replaceChildren(
        el("div", { class: "empty" }, `Kon dashboard niet laden: ${err.message}`)
      );
    }
  } finally {
    dashboardLoading = false;
  }
}

setInterval(() => {
  if (activeTab === "dashboard") loadDashboard(true);
}, 15000);

function tile({ label, value, valueSmall, sub, dot, subParts, onclick }) {
  const subNode = el("div", { class: "sub" });
  if (dot) subNode.append(el("span", { class: `dot ${dot}` }));
  if (sub != null) subNode.append(sub);
  if (subParts) subNode.append(...subParts);
  const node = el(
    "div",
    { class: `tile${onclick ? " clickable" : ""}` },
    el("div", { class: "label" }, label),
    el("div", { class: `value${valueSmall ? " small" : ""}` },
      value == null ? "—" : value),
    subNode
  );
  if (onclick) {
    node.addEventListener("click", onclick);
    node.tabIndex = 0;
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter") onclick();
    });
  }
  return node;
}

function gotoVideo(videoId) {
  return () => { location.hash = `#video/${videoId}`; };
}

function section(title, ...children) {
  return el("section", { class: "dash-section" },
    el("h2", {}, title), ...children);
}

function renderDashboard(data) {
  const { daemon, totals, library, errors, queue, charts, disk } = data;
  const grid = (...tiles) => el("div", { class: "tile-grid" }, ...tiles);

  /* -- Nu -- */
  const current = daemon.current_download;
  const scanBusy = daemon.last_scan_started_at &&
    (!daemon.last_scan_finished_at ||
      daemon.last_scan_started_at > daemon.last_scan_finished_at);
  const nowTiles = grid(
    tile({
      label: "Daemon actief",
      value: daemon.started_at ? fmtRel(daemon.started_at, false) : "—",
      sub: daemon.started_at ? `sinds ${fmtDT(daemon.started_at)}` : "status onbekend",
      dot: daemon.started_at ? "good" : "warning",
    }),
    tile({
      label: "Laatste kanaal-scan",
      value: scanBusy ? "bezig…"
        : daemon.last_scan_finished_at
          ? fmtRel(daemon.last_scan_finished_at) : "—",
      sub: daemon.last_scan_channel_count != null
        ? `${nf.format(daemon.last_scan_channel_count)} kanalen in subscriptions`
        : "",
      dot: scanBusy ? "accent" : null,
    }),
    tile({
      label: "Volgende scan",
      value: daemon.next_scan_at ? fmtRel(daemon.next_scan_at) : "—",
      sub: daemon.next_scan_at
        ? `${fmtDT(daemon.next_scan_at)} · elke ${fmtDur(daemon.poll_interval_seconds)}`
        : "",
    }),
    tile({
      label: "Nu aan het downloaden",
      value: current ? current.title : "niets",
      valueSmall: !!current,
      sub: current
        ? `${current.channel} · gestart ${fmtRel(current.started_at)}`
        : "wachtrij stil",
      dot: current ? "accent" : null,
      onclick: current ? gotoVideo(current.video_id) : null,
    }),
    tile({
      label: "Laatste voltooide download",
      value: library.last_download
        ? fmtRel(library.last_download.downloaded_at) : "—",
      sub: library.last_download
        ? `${library.last_download.channel_handle} · ${library.last_download.title}`
        : "nog geen downloads",
      dot: library.last_download ? "good" : null,
      onclick: library.last_download
        ? gotoVideo(library.last_download.video_id) : null,
    }),
    tile({
      label: "Volgende geplande download",
      value: queue.next ? fmtRel(queue.next.eta) : "—",
      sub: queue.next
        ? `${queue.next.channel_handle} · ${queue.next.title}`
        : "wachtrij leeg",
      onclick: queue.next ? gotoVideo(queue.next.video_id) : null,
    }),
    tile({
      label: "Video's in wachtrij",
      value: nf.format(queue.size),
      sub: `${nf.format(queue.deferred_premieres)} premières · ` +
        `${nf.format(queue.held_back)} in retry-wacht`,
      dot: queue.size > 0 ? "accent" : null,
    }),
    tile({
      label: "Wachtrij leeg (geschat)",
      value: queue.drained_at ? fmtRel(queue.drained_at) : "—",
      sub: queue.drained_at
        ? fmtDT(queue.drained_at)
        : "geen geplande downloads",
    }),
  );

  /* -- Bibliotheek -- */
  const avgBytes = totals.downloaded
    ? library.total_bytes / totals.downloaded : null;
  const avgDur = totals.downloaded
    ? library.total_duration_seconds / totals.downloaded : null;
  const libTiles = grid(
    tile({
      label: "Video's gedownload",
      value: nf.format(totals.downloaded),
      subParts: [el("span", { class: "delta-good" },
        `+${nf.format(library.downloads_7d)} laatste 7 dagen`)],
    }),
    tile({
      label: "Totale bestandsgrootte",
      value: fmtBytes(library.total_bytes),
      sub: `gemiddeld ${fmtBytes(avgBytes)} per video`,
    }),
    tile({
      label: "Totale speelduur",
      value: fmtDur(library.total_duration_seconds),
      sub: `gemiddeld ${fmtDur(avgDur)} per video`,
    }),
    tile({
      label: "SponsorBlock-segmenten geknipt",
      value: nf.format(library.sponsorblock_cuts),
      sub: totals.downloaded
        ? `gemiddeld ${nf1.format(library.sponsorblock_cuts / totals.downloaded)} per video`
        : "",
    }),
    tile({
      label: "Kanalen gevolgd",
      value: nf.format(totals.channels),
      sub: daemon.subscription_count != null
        ? `${nf.format(daemon.subscription_count)} in subscriptions.txt`
        : "",
    }),
    tile({
      label: "Downloads laatste 30 dagen",
      value: nf.format(library.downloads_30d),
      sub: `${fmtBytes(library.bytes_7d)} in de laatste 7 dagen`,
    }),
    tile({
      label: "Grootste video",
      value: library.biggest ? fmtBytes(library.biggest.file_size_bytes) : "—",
      sub: library.biggest
        ? `${library.biggest.channel_handle} · ${library.biggest.title}` : "",
      onclick: library.biggest ? gotoVideo(library.biggest.video_id) : null,
    }),
    tile({
      label: "Langste video",
      value: library.longest ? fmtDur(library.longest.duration_seconds) : "—",
      sub: library.longest
        ? `${library.longest.channel_handle} · ${library.longest.title}` : "",
      onclick: library.longest ? gotoVideo(library.longest.video_id) : null,
    }),
    tile({
      label: "Eerste download",
      value: library.first_download_at
        ? fmtRel(library.first_download_at, false) : "—",
      sub: library.first_download_at
        ? `geleden · ${fmtDT(library.first_download_at)}` : "",
    }),
  );
  if (disk) {
    const usedPct = disk.total ? (disk.used / disk.total) * 100 : 0;
    const diskTile = tile({
      label: "Vrije schijfruimte (downloadmap)",
      value: fmtBytes(disk.free),
      sub: `${fmtBytes(disk.used)} van ${fmtBytes(disk.total)} gebruikt ` +
        `(${nf1.format(usedPct)}%)`,
      dot: usedPct > 90 ? "critical" : usedPct > 75 ? "warning" : null,
    });
    const meter = el("div", { class: "meter" }, el("span"));
    meter.firstChild.style.width = `${Math.min(100, usedPct)}%`;
    diskTile.insertBefore(meter, diskTile.lastChild);
    libTiles.append(diskTile);
  }

  /* -- Betrouwbaarheid -- */
  const sr = errors.success_rate;
  const asr = errors.attempt_success_rate;
  const lastError = errors.last_error;
  const errTiles = grid(
    tile({
      label: "Succespercentage (video's)",
      value: sr == null ? "—" : `${nf1.format(sr)}%`,
      sub: `${nf.format(totals.downloaded)} gelukt · ` +
        `${nf.format(errors.permanently_failed)} definitief gefaald`,
      dot: sr == null ? null : sr >= 99 ? "good" : sr >= 90 ? "warning" : "critical",
    }),
    tile({
      label: "Succespercentage (pogingen)",
      value: asr == null ? "—" : `${nf1.format(asr)}%`,
      sub: `${nf.format(errors.failed_attempts_total)} mislukte pogingen in totaal`,
      dot: asr == null ? null : asr >= 95 ? "good" : asr >= 80 ? "warning" : "critical",
    }),
    tile({
      label: "Definitief gefaald",
      value: nf.format(errors.permanently_failed),
      sub: `na ${nf.format(daemon.max_download_attempts)} pogingen opgegeven`,
      dot: errors.permanently_failed > 0 ? "critical" : "good",
    }),
    tile({
      label: "In wachtrij met fouten",
      value: nf.format(errors.pending_with_errors),
      sub: "wordt opnieuw geprobeerd",
      dot: errors.pending_with_errors > 0 ? "serious" : "good",
    }),
    tile({
      label: "Laatste fout",
      value: lastError ? fmtRel(lastError.last_attempt_at) : "geen",
      sub: lastError
        ? `${lastError.channel_handle} · ${lastError.title} — ${lastError.last_error}`
        : "geen foutmeldingen in de database",
      dot: lastError ? "serious" : "good",
      onclick: lastError ? gotoVideo(lastError.video_id) : null,
    }),
    tile({
      label: "Overgeslagen (baseline)",
      value: nf.format(totals.skipped),
      sub: "bestond al bij het abonneren, nooit gedownload",
    }),
  );

  /* -- grafieken -- */
  const chartsGrid = el("div", { class: "chart-grid" },
    columnChartCard(
      "Downloads per dag", "laatste 30 dagen", charts.per_day),
    channelChartCard(
      "Downloads per kanaal", "gedownloade video's", charts.per_channel),
  );

  panels.dashboard.replaceChildren(
    section("Nu", nowTiles),
    section("Bibliotheek", libTiles),
    section("Betrouwbaarheid", errTiles),
    section("Grafieken", chartsGrid),
  );
}

/* ---------- grafieken (SVG) ---------- */

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

function niceTicks(max) {
  if (max <= 0) return [0, 1];
  const rough = max / 3;
  const pow = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map((k) => k * pow).find((k) => k >= rough);
  const ticks = [];
  for (let v = 0; v <= max; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks.length * step);
  return ticks;
}

// Staaf met afgeronde datakant (4px) en vlakke basislijn.
function roundedTopRect(x, y, width, height) {
  const r = Math.min(4, height, width / 2);
  return `M${x},${y + r} q0,${-r} ${r},${-r} h${width - 2 * r} ` +
    `q${r},0 ${r},${r} v${height - r} h${-width} Z`;
}

function chartCard(title, subtitle, svg, table) {
  const toggle = el("button", { class: "table-toggle", type: "button" }, "tabel");
  table.hidden = true;
  toggle.addEventListener("click", () => {
    const showTable = table.hidden;
    table.hidden = !showTable;
    svg.style.display = showTable ? "none" : "block";
    toggle.textContent = showTable ? "grafiek" : "tabel";
  });
  return el("figure", { class: "chart-card" },
    el("figcaption", {},
      title, el("span", { class: "chart-sub" }, subtitle), toggle),
    svg, table);
}

function columnChartCard(title, subtitle, perDay) {
  const W = 560, H = 190;
  const margin = { top: 12, right: 6, bottom: 24, left: 34 };
  const plotW = W - margin.left - margin.right;
  const plotH = H - margin.top - margin.bottom;
  const maxCount = Math.max(1, ...perDay.map((d) => d.count));
  const ticks = niceTicks(maxCount);
  const yMax = ticks[ticks.length - 1];
  const y = (v) => margin.top + plotH - (v / yMax) * plotH;
  const slot = plotW / perDay.length;
  const barW = Math.min(24, Math.max(2, slot - 2)); // 2px oppervlak-tussenruimte

  const svg = svgEl("svg", {
    class: "chart-svg", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": `${title}, ${subtitle}`,
  });
  for (const tick of ticks) {
    if (tick > 0) {
      svg.append(svgEl("line", {
        class: "gridline",
        x1: margin.left, x2: W - margin.right, y1: y(tick), y2: y(tick),
      }));
    }
    svg.append(svgEl("text", {
      x: margin.left - 6, y: y(tick) + 3.5, "text-anchor": "end",
    }, nf.format(tick)));
  }
  svg.append(svgEl("line", {
    class: "baseline",
    x1: margin.left, x2: W - margin.right,
    y1: y(0), y2: y(0),
  }));

  perDay.forEach((day, i) => {
    const cx = margin.left + slot * i + slot / 2;
    if (day.count > 0) {
      const barH = (day.count / yMax) * plotH;
      svg.append(svgEl("path", {
        class: "bar", d: roundedTopRect(cx - barW / 2, y(0) - barH, barW, barH),
      }));
    }
    if (i % 5 === 0) {
      const [, month, dayNum] = day.day.split("-");
      svg.append(svgEl("text", {
        x: cx, y: H - 8, "text-anchor": "middle",
      }, `${Number(dayNum)}/${Number(month)}`));
    }
    // Ruim hit-doel over de volledige kolomhoogte, met toetsenbordfocus.
    const hit = svgEl("rect", {
      class: "bar-hit", x: margin.left + slot * i, y: margin.top,
      width: slot, height: plotH, tabindex: "0",
    });
    const lines = () => [
      ["t-value", `${nf.format(day.count)} video's · ${fmtBytes(day.bytes)}`],
      ["t-label", new Date(`${day.day}T12:00:00`).toLocaleDateString("nl-BE", {
        weekday: "long", day: "numeric", month: "long",
      })],
    ];
    hit.addEventListener("pointerenter", () =>
      showTooltip(hit.getBoundingClientRect(), lines()));
    hit.addEventListener("focus", () =>
      showTooltip(hit.getBoundingClientRect(), lines()));
    hit.addEventListener("pointerleave", hideTooltip);
    hit.addEventListener("blur", hideTooltip);
    svg.append(hit);
  });

  const table = el("table", { class: "chart-table" },
    el("thead", {}, el("tr", {},
      el("th", {}, "Dag"),
      el("th", { class: "num" }, "Video's"),
      el("th", { class: "num" }, "Grootte"))),
    el("tbody", {}, perDay.filter((d) => d.count > 0).map((d) =>
      el("tr", {},
        el("td", {}, d.day),
        el("td", { class: "num" }, nf.format(d.count)),
        el("td", { class: "num" }, fmtBytes(d.bytes))))));
  return chartCard(title, subtitle, svg, table);
}

function channelChartCard(title, subtitle, perChannel) {
  // Top 10; de staart vouwt samen tot "overige".
  let rows = perChannel.map((c) => ({
    label: c.channel_handle, count: c.n, bytes: c.bytes,
  }));
  if (rows.length > 10) {
    const rest = rows.slice(9);
    rows = rows.slice(0, 9);
    rows.push({
      label: `overige (${rest.length})`,
      count: rest.reduce((sum, r) => sum + r.count, 0),
      bytes: rest.reduce((sum, r) => sum + r.bytes, 0),
    });
  }
  const W = 560;
  const rowH = 26, barH = 16;
  const margin = { top: 4, right: 48, bottom: 6, left: 130 };
  const H = margin.top + margin.bottom + rowH * Math.max(rows.length, 1);
  const plotW = W - margin.left - margin.right;
  const maxCount = Math.max(1, ...rows.map((r) => r.count));

  const svg = svgEl("svg", {
    class: "chart-svg", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": `${title}, ${subtitle}`,
  });
  svg.append(svgEl("line", {
    class: "baseline", x1: margin.left, x2: margin.left,
    y1: margin.top, y2: H - margin.bottom,
  }));
  rows.forEach((row, i) => {
    const yTop = margin.top + rowH * i + (rowH - barH) / 2;
    const width = Math.max(2, (row.count / maxCount) * plotW);
    const r = Math.min(4, barH / 2, width);
    // Liggende staaf: afgeronde datakant rechts, vlak aan de basislijn.
    svg.append(svgEl("path", {
      class: "bar",
      d: `M${margin.left},${yTop} h${width - r} q${r},0 ${r},${r} ` +
        `v${barH - 2 * r} q0,${r} ${-r},${r} h${-(width - r)} Z`,
    }));
    const name = svgEl("text", {
      x: margin.left - 8, y: yTop + barH / 2 + 3.5, "text-anchor": "end",
    });
    name.textContent = row.label.length > 18
      ? `${row.label.slice(0, 17)}…` : row.label;
    svg.append(name);
    // Direct label aan de datakant.
    svg.append(svgEl("text", {
      class: "bar-label", x: margin.left + width + 6,
      y: yTop + barH / 2 + 3.5,
    }, nf.format(row.count)));
    const hit = svgEl("rect", {
      class: "bar-hit", x: 0, y: margin.top + rowH * i,
      width: W, height: rowH, tabindex: "0",
    });
    const lines = () => [
      ["t-value", `${nf.format(row.count)} video's · ${fmtBytes(row.bytes)}`],
      ["t-label", row.label],
    ];
    hit.addEventListener("pointerenter", () =>
      showTooltip(hit.getBoundingClientRect(), lines()));
    hit.addEventListener("focus", () =>
      showTooltip(hit.getBoundingClientRect(), lines()));
    hit.addEventListener("pointerleave", hideTooltip);
    hit.addEventListener("blur", hideTooltip);
    svg.append(hit);
  });

  const table = el("table", { class: "chart-table" },
    el("thead", {}, el("tr", {},
      el("th", {}, "Kanaal"),
      el("th", { class: "num" }, "Video's"),
      el("th", { class: "num" }, "Grootte"))),
    el("tbody", {}, perChannel.map((c) =>
      el("tr", {},
        el("td", {}, c.channel_handle),
        el("td", { class: "num" }, nf.format(c.n)),
        el("td", { class: "num" }, fmtBytes(c.bytes))))));
  return chartCard(title, subtitle, svg, table);
}

/* ---------- dagboek / tijdlijn ---------- */

const PAGE_SIZE = 50;
const timelineRoot = $("#timeline");
let timeline = null;

async function loadTimeline() {
  timelineRoot.replaceChildren(el("div", { class: "tl-loader" }, "laden…"));
  try {
    const [past, future] = await Promise.all([
      fetchJSON(`/api/timeline/past?limit=${PAGE_SIZE}`),
      fetchJSON("/api/timeline/future"),
    ]);
    renderTimeline(past.events, future.events);
  } catch (err) {
    timelineRoot.replaceChildren(
      el("div", { class: "empty" }, `Kon dagboek niet laden: ${err.message}`)
    );
  }
}

function renderTimeline(pastEvents, futureEvents) {
  timeline = {
    oldest: pastEvents.length
      ? pastEvents[pastEvents.length - 1].at : null,
    endReached: pastEvents.length < PAGE_SIZE,
    loading: false,
  };
  const rail = el("div", { class: "timeline-rail" });
  const topSentinel = el("div", { class: "tl-loader" },
    timeline.endReached ? "begin van de geschiedenis" : "ouder laden…");
  rail.append(topSentinel);

  const pastContainer = el("div");
  appendEvents(pastContainer, [...pastEvents].reverse(), null);
  rail.append(pastContainer);

  const nowMarker = el("div", { class: "tl-now" },
    el("span", { class: "line" }),
    el("span", { class: "badge" }, "NU"),
    el("span", { class: "line" }));
  rail.append(nowMarker);

  if (futureEvents.length) {
    rail.append(el("div", { class: "tl-section-label" },
      "gepland — geschatte downloadtijd"));
    const futureContainer = el("div");
    appendEvents(futureContainer, futureEvents, null, true);
    rail.append(futureContainer);
    rail.append(el("div", { class: "tl-end" }, "einde van de wachtrij"));
  } else {
    rail.append(el("div", { class: "tl-end" },
      "wachtrij leeg — niets gepland"));
  }

  timelineRoot.replaceChildren(rail);
  nowMarker.scrollIntoView({ block: "center" });

  const observer = new IntersectionObserver(async (entries) => {
    if (!entries[0].isIntersecting) return;
    if (timeline.endReached || timeline.loading || !timeline.oldest) return;
    timeline.loading = true;
    try {
      const older = await fetchJSON(
        `/api/timeline/past?limit=${PAGE_SIZE}` +
        `&before=${encodeURIComponent(timeline.oldest)}`);
      const events = older.events;
      if (events.length < PAGE_SIZE) {
        timeline.endReached = true;
        topSentinel.textContent = "begin van de geschiedenis";
      }
      if (events.length) {
        timeline.oldest = events[events.length - 1].at;
        const heightBefore = document.documentElement.scrollHeight;
        prependEvents(pastContainer, [...events].reverse());
        window.scrollBy(0, document.documentElement.scrollHeight - heightBefore);
      }
    } catch {
      topSentinel.textContent = "laden mislukt — scroll om opnieuw te proberen";
    } finally {
      timeline.loading = false;
    }
  });
  observer.observe(topSentinel);
}

function eventDayKey(event) {
  return new Date(event.at).toDateString();
}

function appendEvents(container, events, lastDayKey, future = false) {
  let dayKey = lastDayKey;
  for (const event of events) {
    const key = eventDayKey(event);
    if (key !== dayKey) {
      container.append(el("div", { class: "tl-day" }, dayLabel(event.at)));
      dayKey = key;
    }
    container.append(timelineCard(event, future));
  }
  return dayKey;
}

function prependEvents(container, olderEventsAsc) {
  // Oudere items komen boven de bestaande; bouw ze in een eigen fragment en
  // laat een dubbel dagkopje onderaan weg als de dag doorloopt.
  const fragment = el("div");
  const lastKey = appendEvents(fragment, olderEventsAsc, null);
  const firstExistingDay = container.querySelector(".tl-day");
  if (firstExistingDay && olderEventsAsc.length) {
    const nextKey = eventDayKey({ at: firstExistingDayIso(container) });
    if (nextKey === lastKey) firstExistingDay.remove();
  }
  container.prepend(...fragment.childNodes);
}

function firstExistingDayIso(container) {
  const card = container.querySelector(".tl-item");
  return card ? card.dataset.at : new Date().toISOString();
}

const EVENT_LABEL = {
  downloaded: "gedownload",
  failed_attempt: "poging mislukt — wordt opnieuw geprobeerd",
  permanently_failed: "definitief gefaald",
};

function timelineCard(event, future = false) {
  const classes = ["tl-item"];
  if (future) classes.push("future");
  else if (event.type === "downloaded") classes.push("ok");
  else classes.push(event.type);

  const meta = [];
  meta.push(event.channel_handle);
  if (event.duration_seconds != null) meta.push(fmtDur(event.duration_seconds));
  if (event.file_size_bytes != null) meta.push(fmtBytes(event.file_size_bytes));
  if (event.sponsorblock_cuts) {
    meta.push(`${nf.format(event.sponsorblock_cuts)} SB-knips`);
  }
  if (!future && event.type !== "downloaded") {
    meta.push(`poging ${nf.format(event.attempt_count)}`);
  }
  if (future) {
    if (event.kind === "premiere") {
      meta.push(`première · beschikbaar ${fmtRel(event.available_at)}`);
    } else if (event.kind === "retry") {
      meta.push(`nieuwe poging ${nf.format(event.attempt_count + 1)}`);
    } else {
      meta.push("gepland");
    }
  } else {
    meta.push(EVENT_LABEL[event.type] || event.type);
  }

  const body = el("div", { class: "tl-body" },
    el("div", { class: "tl-title" }, event.title),
    el("div", { class: "tl-meta" }, meta.join(" · ")));
  if (event.error) {
    body.append(el("div", { class: "tl-error" }, "⚠ ", event.error));
  }

  const thumb = el("img", {
    class: "tl-thumb", loading: "lazy", alt: "",
    src: thumbUrl(event.video_id),
  });
  thumb.addEventListener("error", () => { thumb.style.visibility = "hidden"; });

  const when = future ? event.eta : event.at;
  const card = el("div", {
    class: classes.join(" "),
    dataset: { at: event.at },
    tabindex: "0",
    role: "link",
  },
    thumb, body,
    el("div", { class: "tl-time" },
      el("div", {}, future ? `~ ${fmtTime(when)}` : fmtTime(when)),
      el("div", {}, fmtRel(when))));
  const open = gotoVideo(event.video_id);
  card.addEventListener("click", open);
  card.addEventListener("keydown", (keyEvent) => {
    if (keyEvent.key === "Enter") open();
  });
  return card;
}

/* ---------- video-detail ---------- */

async function loadVideo(videoId) {
  panels.video.replaceChildren(el("div", { class: "empty" }, "laden…"));
  try {
    const data = await fetchJSON(
      videoId ? `/api/video/${encodeURIComponent(videoId)}` : "/api/video/latest");
    renderVideo(data, videoId == null);
  } catch (err) {
    panels.video.replaceChildren(
      el("div", { class: "empty" }, `Kon video niet laden: ${err.message}`)
    );
  }
}

function fieldRow(name, value, human) {
  const cell = el("td", {});
  if (value == null || value === "") {
    cell.append(el("span", { class: "raw" }, "NULL"));
  } else if (human != null && human !== String(value)) {
    cell.append(String(human), " ",
      el("span", { class: "raw" }, `(${value})`));
  } else {
    cell.append(String(value));
  }
  return el("tr", {}, el("th", {}, name), cell);
}

function renderVideo(video, isLatest) {
  const [statusLabel, statusDot] = STATUS_NL[video.status] || [video.status, "muted"];

  const thumb = el("img", { alt: "", src: thumbUrl(video.video_id, "hqdefault") });
  thumb.addEventListener("error", () => { thumb.style.display = "none"; });

  const badges = el("div", {},
    el("span", { class: "badge" },
      el("span", { class: `dot ${statusDot}` }), statusLabel),
    " ",
    el("span", { class: "badge" },
      video.file_exists ? "bestand aanwezig op schijf" : "geen bestand op schijf"),
  );

  const head = el("div", { class: "video-head" },
    thumb,
    el("div", { class: "vh-body" },
      isLatest ? el("div", { class: "tl-meta" }, "laatst gedownloade video") : null,
      el("h2", {}, video.title),
      el("div", { class: "vh-channel" }, video.channel_handle),
      badges,
      el("p", {},
        el("a", { href: video.youtube_url, target: "_blank", rel: "noreferrer" },
          "Bekijk op YouTube ↗"))));

  const videoTable = el("table", { class: "field-table" },
    fieldRow("video_id", video.video_id),
    fieldRow("channel_handle", video.channel_handle),
    fieldRow("title", video.title),
    fieldRow("upload_date", video.upload_date),
    fieldRow("duration_seconds", video.duration_seconds,
      fmtDur(video.duration_seconds)),
    fieldRow("filepath", video.filepath),
    fieldRow("file_size_bytes", video.file_size_bytes,
      fmtBytes(video.file_size_bytes)),
    fieldRow("sponsorblock_cuts", video.sponsorblock_cuts),
    fieldRow("downloaded_at", video.downloaded_at,
      video.downloaded_at
        ? `${fmtDT(video.downloaded_at)} (${fmtRel(video.downloaded_at)})` : null),
    fieldRow("status", video.status, statusLabel),
    fieldRow("attempt_count", video.attempt_count),
    fieldRow("last_error", video.last_error),
    fieldRow("last_attempt_at", video.last_attempt_at,
      video.last_attempt_at
        ? `${fmtDT(video.last_attempt_at)} (${fmtRel(video.last_attempt_at)})` : null),
    fieldRow("available_at", video.available_at,
      video.available_at ? fmtDT(video.available_at) : null),
  );

  const cards = [head];
  if (video.last_error) {
    cards.push(el("div", { class: "field-card" },
      el("h3", {}, "Laatste foutmelding"),
      el("div", { style: "padding: 0 10px 12px" },
        el("pre", { class: "errorbox" }, video.last_error))));
  }
  cards.push(el("div", { class: "field-card" },
    el("h3", {}, "Alle velden — tabel videos"), videoTable));

  if (video.channel) {
    cards.push(el("div", { class: "field-card" },
      el("h3", {}, "Kanaal — tabel channels"),
      el("table", { class: "field-table" },
        fieldRow("handle", video.channel.handle),
        el("tr", {}, el("th", {}, "url"),
          el("td", {}, el("a", {
            href: video.channel.url, target: "_blank", rel: "noreferrer",
          }, video.channel.url))),
        fieldRow("first_seen_at", video.channel.first_seen_at,
          fmtDT(video.channel.first_seen_at)),
        fieldRow("last_checked_at", video.channel.last_checked_at,
          video.channel.last_checked_at
            ? `${fmtDT(video.channel.last_checked_at)} (${fmtRel(video.channel.last_checked_at)})`
            : null),
        fieldRow("watermark_date", video.channel.watermark_date))));
  }

  panels.video.replaceChildren(...cards);
}

/* ---------- start ---------- */

route();
