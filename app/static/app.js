/* Barq console — vanilla JS, no build step.
   The token lives in localStorage; every request re-reads it so a sign-out in
   another tab takes effect on the next call. */

const API = "/api";
const TOKEN_KEY = "barq.token";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const state = { token: localStorage.getItem(TOKEN_KEY), user: null, feeders: [] };

/* ---------------- transport ---------------- */
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;

  const response = await fetch(`${API}${path}`, { ...options, headers });

  if (response.status === 401) {
    signOut();
    throw new Error("Session expired — please sign in again.");
  }

  const text = await response.text();
  const body = text ? JSON.parse(text) : null;

  if (!response.ok) {
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.errors
          ? detail.errors.map((e) => `${e.field}: ${e.message}`).join(", ")
          : `Request failed (${response.status})`;
    throw new Error(message);
  }
  return body;
}

/* ---------------- auth ---------------- */
function showAuthError(message) {
  $("#auth-error").textContent = message || "";
}

async function signIn(email, password) {
  const body = await api("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  state.token = body.access_token;
  state.user = body.user;
  localStorage.setItem(TOKEN_KEY, state.token);
  await enterApp();
}

async function register(fullName, email, password) {
  const body = await api("/auth/register", {
    method: "POST",
    body: JSON.stringify({ full_name: fullName, email, password }),
  });
  state.token = body.access_token;
  state.user = body.user;
  localStorage.setItem(TOKEN_KEY, state.token);
  await enterApp();
}

function signOut() {
  state.token = null;
  state.user = null;
  localStorage.removeItem(TOKEN_KEY);
  $("#app-screen").classList.add("is-hidden");
  $("#auth-screen").classList.remove("is-hidden");
}

async function enterApp() {
  if (!state.user) {
    state.user = await api("/auth/me");
  }
  $("#auth-screen").classList.add("is-hidden");
  $("#app-screen").classList.remove("is-hidden");
  $("#user-chip").textContent = `${state.user.email} · ${state.user.role}`;
  await refresh();
}

/* ---------------- data ---------------- */
async function refresh() {
  try {
    const [dashboard, feeders, alerts] = await Promise.all([
      api("/dashboard"),
      api("/feeders"),
      api("/alerts?limit=20"),
    ]);
    renderKpis(dashboard);
    state.feeders = feeders;
    renderFeederOptions(feeders);
    renderAlerts(alerts);
    await renderFleet(feeders);
  } catch (error) {
    console.error(error);
  }
}

function renderKpis(d) {
  $("#kpi-load").textContent = d.total_load_mw.toFixed(0);
  $("#kpi-capacity").textContent = d.total_capacity_mw.toFixed(0);
  $("#kpi-renewable").textContent = (d.renewable_share * 100).toFixed(1);
  $("#kpi-frequency").textContent = d.average_frequency_hz.toFixed(2);
  $("#kpi-alerts").textContent = d.open_alerts;
  $("#kpi-critical").textContent = `${d.critical_alerts} critical`;
}

function renderFeederOptions(feeders) {
  const select = $("#feeder-select");
  const previous = select.value;
  select.innerHTML = "";
  if (!feeders.length) {
    select.appendChild(el("option", null, "No feeders yet"));
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const feeder of feeders) {
    const option = el("option", null, `${feeder.code} — ${feeder.name}`);
    option.value = feeder.id;
    select.appendChild(option);
  }
  if (previous && feeders.some((f) => String(f.id) === previous)) select.value = previous;
}

async function renderFleet(feeders) {
  const body = $("#fleet-body");
  body.innerHTML = "";
  if (!feeders.length) {
    const row = el("tr");
    const cell = el("td", null, "No assets yet — create a feeder and add units.");
    cell.colSpan = 7;
    cell.style.color = "var(--text-faint)";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  for (const feeder of feeders) {
    const assets = await api(`/feeders/${feeder.id}/assets`);
    for (const asset of assets) {
      const row = el("tr");
      row.appendChild(el("td", null, asset.code));
      row.appendChild(el("td", `kind kind-${asset.kind}`, asset.kind));
      row.appendChild(el("td", null, `${asset.capacity_mw.toFixed(0)} MW`));
      row.appendChild(el("td", null, `${asset.min_stable_mw.toFixed(0)} MW`));
      row.appendChild(el("td", null, `${asset.ramp_mw_per_min.toFixed(0)}/min`));
      row.appendChild(el("td", null, `PKR ${asset.marginal_cost.toFixed(0)}`));
      const soc =
        asset.state_of_charge_mwh != null && asset.storage_capacity_mwh
          ? `${((asset.state_of_charge_mwh / asset.storage_capacity_mwh) * 100).toFixed(0)}%`
          : "—";
      row.appendChild(el("td", null, soc));
      body.appendChild(row);
    }
  }
}

function renderAlerts(alerts) {
  const list = $("#alerts-list");
  list.innerHTML = "";
  if (!alerts.length) {
    list.appendChild(el("p", "alert-empty", "No open alerts. The grid is inside its limits."));
    return;
  }
  for (const alert of alerts) {
    const card = el("div", `alert ${alert.severity}`);
    const top = el("div", "alert-top");
    top.appendChild(el("span", "alert-title", alert.title));
    top.appendChild(el("span", "alert-sev", alert.severity));
    card.appendChild(top);
    card.appendChild(el("p", "alert-msg", alert.message));
    list.appendChild(card);
  }
}

/* ---------------- dispatch ---------------- */
function renderDispatch(result) {
  const box = $("#dispatch-result");
  box.classList.remove("is-hidden");

  const badge = $("#result-status");
  badge.textContent = result.status;
  badge.className = `badge ${result.status}`;

  $("#result-cost").textContent = `PKR ${result.expected_cost.toLocaleString(undefined, {
    maximumFractionDigits: 0,
  })} this interval`;

  $("#res-gen").textContent = `${result.generation_mw.toFixed(1)} MW`;
  $("#res-storage").textContent = `${result.storage_net_mw.toFixed(1)} MW`;
  $("#res-curtailed").textContent = `${result.curtailed_mw.toFixed(1)} MW`;
  $("#res-unserved").textContent = `${result.unserved_mw.toFixed(1)} MW`;
  $("#res-reserve").textContent = `${result.reserve_mw.toFixed(0)} / ${result.reserve_required_mw.toFixed(0)} MW`;
  $("#res-price").textContent = `PKR ${result.marginal_price.toFixed(0)}/MWh`;

  const notes = $("#result-notes");
  notes.innerHTML = "";
  for (const note of result.notes || []) {
    notes.appendChild(el("div", "note", note));
  }

  const allocations = $("#allocations");
  allocations.innerHTML = "";
  const sorted = [...result.allocations].sort((a, b) => b.setpoint_mw - a.setpoint_mw);

  for (const unit of sorted) {
    const card = el("div", "alloc");
    const top = el("div", "alloc-top");

    const left = el("span", "alloc-code", unit.asset_code);
    if (unit.was_ramp_limited) left.appendChild(el("span", "alloc-flag", "ramp-limited"));
    top.appendChild(left);
    top.appendChild(el("span", "alloc-mw", `${unit.setpoint_mw.toFixed(1)} MW`));
    card.appendChild(top);

    const bar = el("div", "alloc-bar");
    const fill = el("div", "alloc-fill");
    const pct = unit.capacity_mw > 0 ? (unit.setpoint_mw / unit.capacity_mw) * 100 : 0;
    fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    bar.appendChild(fill);
    card.appendChild(bar);

    card.appendChild(el("p", "alloc-reason", unit.reason));
    allocations.appendChild(card);
  }
}

async function runDispatch(persist) {
  const feederId = Number($("#feeder-select").value);
  const demand = Number($("#demand-input").value);
  const interval = Number($("#interval-select").value);

  if (!feederId) {
    alert("Create a feeder first.");
    return;
  }

  const path = persist ? "/dispatch" : "/dispatch/preview";
  try {
    const result = await api(path, {
      method: "POST",
      body: JSON.stringify({
        feeder_id: feederId,
        demand_mw: demand,
        interval_minutes: interval,
        persist,
      }),
    });
    renderDispatch(result);
    if (persist) await refresh();
  } catch (error) {
    alert(error.message);
  }
}

/* ---------------- wiring ---------------- */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.remove("is-active");
      t.setAttribute("aria-selected", "false");
    });
    tab.classList.add("is-active");
    tab.setAttribute("aria-selected", "true");
    const isLogin = tab.dataset.tab === "login";
    $("#login-form").classList.toggle("is-hidden", !isLogin);
    $("#register-form").classList.toggle("is-hidden", isLogin);
    showAuthError("");
  });
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showAuthError("");
  const data = new FormData(event.target);
  try {
    await signIn(data.get("email"), data.get("password"));
  } catch (error) {
    showAuthError(error.message);
  }
});

$("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showAuthError("");
  const data = new FormData(event.target);
  try {
    await register(data.get("full_name"), data.get("email"), data.get("password"));
  } catch (error) {
    showAuthError(error.message);
  }
});

$("#logout-btn").addEventListener("click", signOut);

$("#dispatch-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runDispatch(true);
});

$("#preview-btn").addEventListener("click", () => runDispatch(false));

/* ---------------- boot ---------------- */
(async function boot() {
  if (!state.token) return;
  try {
    await enterApp();
  } catch {
    signOut();
  }
})();
