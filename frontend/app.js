const $ = (id) => document.getElementById(id);
// If the server sets JALRAKSHAK_ADMIN_KEY, store it once in the browser console:
//   localStorage.setItem("jr_api_key", "<key>")
const apiKey = () => { try { return localStorage.getItem("jr_api_key"); } catch { return null; } };
const api = async (path, opts = {}) => {
  const key = apiKey();
  const headers = { ...(opts.headers || {}), ...(key ? { "X-API-Key": key } : {}) };
  const res = await fetch("/api" + path, { ...opts, headers });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
};
const fmt = (n) => Math.round(n).toLocaleString("en-IN");
const COLORS = ["#0f4c5c", "#2a9db5", "#7cc6d4", "#e9a23b", "#2f9e6e", "#9aa9b0"];
const charts = {};

function draw(key, canvasId, config) {
  charts[key]?.destroy();
  charts[key] = new Chart($(canvasId), config);
}

async function loadSummary() {
  const s = await api("/summary");
  $("todayL").textContent = fmt(s.today_liters);
  $("budgetTxt").textContent = `${s.budget_used_pct}% of ${fmt(s.daily_budget_liters)} L budget`;
  $("budgetTxt").classList.toggle("over", s.budget_used_pct > 100);
  $("kAvg").textContent = fmt(s.avg_7d_liters);
  $("kCap").textContent = fmt(s.per_capita_today);
  $("kLeak").textContent = fmt(s.est_leak_loss_l_per_day);
  $("kAlerts").textContent = s.active_alerts;
  $("asOf").textContent = "Data as of " + new Date(s.as_of).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });

  // tank scale: full tank = 1.5 x budget so the budget line sits at 2/3 height
  const scale = s.daily_budget_liters * 1.5;
  const pct = Math.min(100, (s.today_liters / scale) * 100);
  $("tankFill").style.height = pct + "%";
  $("tankFill").classList.toggle("over", s.budget_used_pct > 100);
  $("budgetLine").style.bottom = (s.daily_budget_liters / scale) * 100 + "%";
  return s;
}

async function loadDaily(budget) {
  const d = await api("/usage/daily?days=14");
  draw("daily", "chDaily", {
    type: "bar",
    data: {
      labels: d.map((x) => new Date(x.date).toLocaleDateString("en-IN", { day: "numeric", month: "short" })),
      datasets: [
        { label: "Litres used", data: d.map((x) => x.liters), backgroundColor: d.map((x) => (x.liters > budget * 1.1 ? "#e4572e" : "#2a9db5")), borderRadius: 5 },
        { label: "Daily budget", data: d.map(() => budget), type: "line", borderColor: "#0c2d3a", borderDash: [6, 5], pointRadius: 0, borderWidth: 2 },
      ],
    },
    options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom" } }, scales: { y: { beginAtZero: true, grid: { color: "#e6f0f2" } }, x: { grid: { display: false } } } },
  });
}

async function loadZones() {
  const z = await api("/usage/zones?days=7");
  draw("zones", "chZones", {
    type: "doughnut",
    data: { labels: z.map((x) => x.zone), datasets: [{ data: z.map((x) => x.liters), backgroundColor: COLORS, borderWidth: 2 }] },
    options: { maintainAspectRatio: false, cutout: "62%", plugins: { legend: { display: false } } },
  });
  $("zoneList").innerHTML = z
    .map((x, i) => `<li><span><span class="dot" style="background:${COLORS[i % COLORS.length]}"></span>${x.icon} ${x.zone}</span><strong>${x.share}%</strong></li>`)
    .join("");
}

async function loadHourly() {
  const h = await api("/usage/hourly");
  draw("hourly", "chHourly", {
    type: "line",
    data: {
      labels: h.hours.map((x) => String(x.hour).padStart(2, "0") + ":00"),
      datasets: [{ label: "Litres", data: h.hours.map((x) => x.liters), borderColor: "#0f4c5c", backgroundColor: "#2a9db555", fill: true, tension: 0.35, pointRadius: 2 }],
    },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, grid: { color: "#e6f0f2" } }, x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } } } },
  });
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

async function loadAlerts() {
  const a = await api("/alerts");
  $("alerts").innerHTML = a.length
    ? a.map((x) => `<div class="item ${x.severity}"><h3>${esc(x.title)}</h3><p>${esc(x.detail)}</p></div>`).join("")
    : `<div class="empty">No unusual usage detected. Nice.</div>`;
}

async function loadRecs() {
  const r = await api("/recommendations");
  $("recs").innerHTML = r
    .map((x) => `<div class="item ${x.priority}"><h3>${esc(x.title)}</h3><p>${esc(x.action)}</p>${x.saves_l_per_month ? `<div class="save">Saves about ${fmt(x.saves_l_per_month)} L/month</div>` : ""}</div>`)
    .join("");
}

async function loadZoneSelect() {
  const z = await api("/zones");
  $("fZone").innerHTML = z.map((x) => `<option value="${x.id}">${x.icon} ${esc(x.name)}</option>`).join("");
}

async function refresh() {
  const s = await loadSummary();
  await Promise.all([loadDaily(s.daily_budget_liters), loadZones(), loadHourly(), loadAlerts(), loadRecs()]);
}

$("btnAdd").addEventListener("click", async () => {
  const msg = $("fMsg");
  try {
    await api("/readings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ zone_id: +$("fZone").value, liters: +$("fLiters").value }),
    });
    msg.textContent = "Reading added.";
    refresh();
  } catch (e) {
    msg.textContent = "Could not add reading: " + e.message;
  }
});

$("btnReseed").addEventListener("click", async () => {
  await api("/demo/reseed", { method: "POST" });
  refresh();
});

$("btnSettings").addEventListener("click", async () => {
  const s = await api("/settings");
  $("sHouse").value = s.household_size;
  $("sLpcd").value = s.lpcd_target;
  $("dlgSettings").showModal();
});
$("btnSaveSettings").addEventListener("click", async () => {
  await api("/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ household_size: +$("sHouse").value, lpcd_target: +$("sLpcd").value }),
  });
  refresh();
});

loadZoneSelect().then(refresh);
