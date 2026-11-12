/* Dish Aligner card - radial drag-to-aim + Wi-Fi survey picker.
 *
 * config:
 *   type: custom:dish-aligner-card
 *   angle_entity:    sensor.*_current_angle      (required)
 *   target_entity:   number.*_azimuth_target     (required)
 *   status_entity:   sensor.*_status             (dish_aligner status)
 *   survey_entity:   sensor.*_survey             (dish_aligner survey)
 *   signal_entity:   sensor.*_signal             (dBm readout, optional)
 *   beamwidth:       degrees of wedge drawn (default 50)
 *
 * Drag the needle to aim (calls dish_aligner.aim). Survey APs are drawn on
 * a polar plot - bearing = best-surveyed azimuth, radius = signal strength
 * (strong = toward the dish) - and listed in a table below; clicking a
 * marker or "Join" calls dish_aligner.join. The associated AP gets a
 * pulsing halo via status_entity's connected_bssid attribute.
 */

const TAU = Math.PI * 2;

class DishAlignerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._dragging = false;
    this._dragAngle = null;
  }

  setConfig(config) {
    this._config = config;
    this._beamwidth = config.beamwidth ?? 50;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._dragging) this._render(); // don't blow away the DOM mid-drag
  }

  _st(eid) { return eid ? this._hass?.states[eid] : undefined; }
  _num(eid) {
    const v = parseFloat(this._st(eid)?.state);
    return Number.isFinite(v) ? v : null;
  }

  // azimuth deg -> svg point on radius r (0 deg = up, clockwise)
  _pt(cx, cy, r, deg) {
    const a = ((deg - 90) * Math.PI) / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }

  _wedge(cx, cy, r, az, width) {
    const [x1, y1] = this._pt(cx, cy, r, az - width / 2);
    const [x2, y2] = this._pt(cx, cy, r, az + width / 2);
    const large = width > 180 ? 1 : 0;
    return `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
  }

  _onPointer(e) {
    const svg = this.shadowRoot.querySelector("svg");
    const rect = svg.getBoundingClientRect();
    const x = e.clientX - (rect.left + rect.width / 2);
    const y = e.clientY - (rect.top + rect.height / 2);
    let deg = (Math.atan2(y, x) * 180) / Math.PI + 90;
    return (deg + 360) % 360;
  }

  _down(e) {
    this._dragging = true;
    this._dragAngle = this._onPointer(e);
    this._render(); // draw needle first, then capture on the new svg node
    this.shadowRoot.querySelector("svg")?.setPointerCapture?.(e.pointerId);
  }
  _move(e) {
    if (!this._dragging) return;
    this._dragAngle = this._onPointer(e);
    // mutate the needle in place - full re-render would drop pointer capture
    const [x, y] = this._pt(150, 150, 102, this._dragAngle);
    const line = this.shadowRoot.querySelector(".target-line");
    const knob = this.shadowRoot.querySelector(".target-dot");
    if (line && knob) {
      line.setAttribute("x2", x); line.setAttribute("y2", y);
      knob.setAttribute("cx", x); knob.setAttribute("cy", y);
    }
  }
  _up(e) {
    if (!this._dragging) return;
    this._dragging = false;
    const az = Math.round(this._dragAngle);
    this._dragAngle = null;
    this._svc("aim", { azimuth: az });
    this._render();
  }

  _svc(name, data = {}) {
    this._callErr = null;
    this._notice = null;
    this._hass.callService("dish_aligner", name, data).catch((e) => {
      // Immediate failures only (busy guard, bad args) - op failures land
      // in status_entity's last_error attribute instead
      this._callErr = e.message || String(e);
      this._render();
    });
  }

  // Same thresholds as the dashboard's signal mini-graph
  _dbmColor(dbm) {
    if (dbm == null) return "var(--disabled-text-color,#666)";
    if (dbm >= -65) return "#00a803";
    if (dbm >= -74) return "#f39c12";
    if (dbm >= -80) return "#d35400";
    return "#c0392b";
  }

  // dBm -> plot radius: -40 hugs the dish, -95 sits at the rim
  _dbmRadius(dbm) {
    if (dbm == null) return 138;
    const t = Math.min(1, Math.max(0, (dbm + 40) / -55));
    return 38 + t * 100;
  }

  // Short encryption label for the table's Sec column
  _encLabel(ap) {
    const c = ap.crypto || {};
    if (c.enabled === false) return "open";
    if (c.description) return c.description.replace(/ \(.*\)/, "");
    const auths = c.authentication || [];
    if (auths.includes("sae")) return "WPA3";
    if (auths.includes("eap")) return "802.1x";
    if (auths.includes("psk")) return "WPA2";
    if (auths.includes("owe")) return "OWE";
    return c.enabled ? "secured" : "?";
  }

  _join(ap) {
    let { ssid } = ap;
    if (!ssid) {
      // Hidden network - we know its BSSID/angle but not the name
      ssid = window.prompt(`Hidden network (${ap.bssid}) - enter its SSID`, "");
      if (!ssid) return;
    }
    const secured = ap.crypto?.enabled !== false;
    const password = secured
      ? window.prompt(`Password for "${ssid}"`, "")
      : "";
    if (secured && password === null) return; // cancelled
    if (window.confirm(`Aim at ${ssid} (${ap.azimuth}\u00b0) and join it?`)) {
      this._pendingJoin = { ssid, bssid: ap.bssid };
      this._svc("join", { ssid, bssid: ap.bssid, password });
    }
  }

  // Bad password / wrong encryption -> re-prompt for the same SSID
  _retryJoin(j) {
    const password = window.prompt(
      `Couldn't connect to "${j.ssid}" - the password may be wrong ` +
      `(or the network uses a different encryption type). Password:`,
      ""
    );
    if (password === null) return;
    this._pendingJoin = j;
    this._svc("join", { ssid: j.ssid, ...(j.bssid ? { bssid: j.bssid } : {}), password });
  }

  _manualJoin() {
    const ssid = this.shadowRoot.getElementById("mssid")?.value.trim();
    const password = this.shadowRoot.getElementById("mpass")?.value ?? "";
    if (!ssid) return;
    if (window.confirm(`Repoint the uplink to "${ssid}" at the current heading?`)) {
      this._pendingJoin = { ssid };
      this._svc("join", { ssid, password });
    }
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const c = this._config;
    const az = this._num(c.angle_entity) ?? 0;
    const target = this._num(c.target_entity);
    const status = this._st(c.status_entity)?.state ?? "unknown";
    const progress = this._st(c.status_entity)?.attributes?.progress;
    const dbm = this._num(c.signal_entity);
    const aps = (this._st(c.survey_entity)?.attributes?.access_points ?? [])
      .filter((ap) => (ap.ssid || "").trim());
    const connAttrs = this._st(c.status_entity)?.attributes ?? {};
    const connBssid = (connAttrs.connected_bssid || "").toLowerCase();
    const connSsid = connAttrs.connected_ssid;
    const shown = this._dragging ? this._dragAngle : target;
    const busy = status !== "idle" && status !== "unknown";

    // Remember which AP we were on before this op started, so the
    // "Connected" notice only fires when the association actually moved
    if (!this._prevBusy && busy) {
      this._preOpBssid = connBssid;
    }
    // Surface a clean result when an op finishes: red banner with the
    // controller's reason on failure, green notice on a fresh connect
    if (this._prevBusy && status === "idle") {
      const pending = this._pendingJoin;
      this._pendingJoin = null;
      if (connAttrs.last_error) {
        this._notice = null;
        // Rejected association - reopen the password prompt for that SSID
        if (pending && /password/i.test(connAttrs.last_error)) {
          setTimeout(() => this._retryJoin(pending), 50);
        }
      } else if (connSsid && connBssid && connBssid !== this._preOpBssid) {
        this._notice = `Connected to ${connSsid}`;
        clearTimeout(this._noticeTimer);
        this._noticeTimer = setTimeout(() => {
          this._notice = null;
          if (!this._dragging) this._render();
        }, 6000);
      }
    }
    this._prevBusy = busy;
    const err = this._callErr || connAttrs.last_error;
    const errShown = err && err !== this._dismissedErr;

    const CX = 150, CY = 150, R = 110;
    let ticks = "";
    for (let d = 0; d < 360; d += 15) {
      const [x1, y1] = this._pt(CX, CY, R, d);
      const [x2, y2] = this._pt(CX, CY, R - (d % 45 === 0 ? 12 : 6), d);
      ticks += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="tick${d % 45 === 0 ? " major" : ""}"/>`;
    }
    let labels = "";
    [["0","N"],["90","E"],["180","S"],["270","W"]].forEach(([d, t]) => {
      const [x, y] = this._pt(CX, CY, R - 24, +d);
      labels += `<text x="${x}" y="${y}" class="dir">${t}</text>`;
    });
    const [tx, ty] = shown != null ? this._pt(CX, CY, R - 8, shown) : [0, 0];
    const [ax, ay] = this._pt(CX, CY, R - 20, az);

    // Faint signal-strength grid rings at -50 / -65 / -80 dBm
    let grid = "";
    for (const d of [-50, -65, -80]) {
      const r = this._dbmRadius(d);
      grid += `<circle cx="${CX}" cy="${CY}" r="${r}" class="grid-ring"/>
               <text x="${CX + 4}" y="${CY - r + 4}" class="grid-label">${d}</text>`;
    }

    // AP markers: bearing = best azimuth, radius = signal strength.
    // Overlapping markers get nudged outward so they stay clickable.
    let markers = "";
    const placed = [];
    for (const ap of aps) {
      if (ap.azimuth == null) continue;
      let r = this._dbmRadius(ap.dbm);
      while (
        r < 130 &&
        placed.some(
          (p) =>
            Math.abs(((ap.azimuth - p.az + 540) % 360) - 180) > 173 &&
            Math.abs(r - p.r) < 12
        )
      ) r += 12;
      placed.push({ az: ap.azimuth, r });
      const [mx, my] = this._pt(CX, CY, r, ap.azimuth);
      const isConn =
        (connBssid && ap.bssid === connBssid) ||
        (connSsid && ap.ssid === connSsid);
      markers += `<g class="ap${isConn ? " connected" : ""}" data-bssid="${ap.bssid}">
        <title>${ap.ssid || "hidden"} · ${ap.dbm ?? "?"} dBm · ${ap.bssid}</title>
        ${isConn ? `<circle class="ap-halo" cx="${mx}" cy="${my}" r="10"/>` : ""}
        <circle cx="${mx}" cy="${my}" r="5.5" fill="${this._dbmColor(ap.dbm)}"/>
      </g>`;
    }

    const rows = aps.map((ap) => `
      <tr>
        <td class="ssid">${ap.ssid || "<i>hidden</i>"}</td>
        <td style="color:${this._dbmColor(ap.dbm)}">${ap.dbm ?? "?"}</td>
        <td>${ap.azimuth ?? "?"}&deg;</td>
        <td>${ap.channel ?? "?"}</td>
        <td>${this._encLabel(ap)}</td>
        <td><button class="join" data-bssid="${ap.bssid}">Join</button></td>
      </tr>`).join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; }
        ha-card { padding: 16px; }
        svg { width:100%; max-width:320px; display:block; margin:0 auto; touch-action:none; cursor:crosshair; }
        .ring { fill:none; stroke:var(--divider-color,#444); stroke-width:1.5; }
        .tick { stroke:var(--secondary-text-color,#888); stroke-width:1; }
        .tick.major { stroke-width:2; }
        .dir { fill:var(--secondary-text-color,#888); font-size:12px; text-anchor:middle; dominant-baseline:middle; }
        .wedge { fill:var(--primary-color); opacity:.22; }
        .beam { stroke:var(--primary-color); stroke-width:3; }
        .target-line { stroke:var(--accent-color, var(--primary-color)); stroke-dasharray:6 4; stroke-width:2; }
        .target-dot { fill:var(--accent-color, var(--primary-color)); stroke:#fff; stroke-width:1.5; }
        .grid-ring { fill:none; stroke:var(--divider-color,#444); stroke-width:.7; stroke-dasharray:2 4; opacity:.6; }
        .grid-label { fill:var(--secondary-text-color,#888); font-size:8px; text-anchor:start; dominant-baseline:middle; }
        .dish { stroke-linecap:round; }
        .ap { cursor:pointer; }
        .ap circle:not(.ap-halo) { stroke:rgba(0,0,0,.4); stroke-width:1; }
        .ap.connected circle:not(.ap-halo) { stroke:#fff; stroke-width:2; }
        .ap-halo { fill:none; stroke:#fff; stroke-width:1.5; animation:halo 1.6s ease-in-out infinite; }
        @keyframes halo { 0%,100% { stroke-opacity:.9 } 50% { stroke-opacity:.25 } }
        .status { text-align:center; margin:8px 0 4px; font-size:14px; }
        .status b { text-transform:capitalize; }
        .btns { display:flex; gap:8px; justify-content:center; flex-wrap:wrap; margin:10px 0; }
        button { background:var(--primary-color); color:#fff; border:0; border-radius:6px;
                 padding:6px 12px; font-size:13px; cursor:pointer; }
        button:disabled { opacity:.4; cursor:default; }
        button.danger { background:var(--error-color,#c00); }
        table { width:100%; border-collapse:collapse; font-size:13px; }
        th, td { padding:4px 6px; text-align:left; }
        th { color:var(--secondary-text-color,#888); font-weight:600; border-bottom:1px solid var(--divider-color,#444); }
        .ssid { font-weight:600; }
        .join { padding:2px 10px; font-size:12px; }
        .banner { display:flex; align-items:center; gap:8px; border-radius:6px;
                  padding:8px 10px; margin:8px 0; font-size:13px; color:#fff; }
        .banner.err { background:var(--error-color,#c00); }
        .banner.ok { background:#1e7d32; }
        .banner .x { background:transparent; padding:0 2px; margin-left:auto;
                     font-size:16px; line-height:1; }
        .legend { display:flex; gap:12px; justify-content:center; align-items:center;
                  font-size:11px; color:var(--secondary-text-color,#888); margin-top:2px; }
        .legend i { display:inline-block; width:8px; height:8px; border-radius:50%;
                    margin-right:3px; vertical-align:baseline; }
        .hint { text-align:center; color:var(--secondary-text-color,#888);
                font-size:12px; margin:6px 0; }
      </style>
      <ha-card>
        <svg viewBox="0 0 300 300">
          <circle cx="${CX}" cy="${CY}" r="${R}" class="ring"/>
          ${grid}
          ${ticks}${labels}
          <path class="wedge" d="${this._wedge(CX, CY, R - 4, az, this._beamwidth)}"/>
          <line class="beam" x1="${CX}" y1="${CY}" x2="${ax}" y2="${ay}"/>
          ${shown != null ? `<line class="target-line" x1="${CX}" y1="${CY}" x2="${tx}" y2="${ty}"/>
          <circle class="target target-dot" cx="${tx}" cy="${ty}" r="9"/>` : ""}
          <g class="dish" transform="rotate(${az} ${CX} ${CY})">
            <path d="M ${CX - 10} ${CY + 2} Q ${CX} ${CY + 14} ${CX + 10} ${CY + 2}"
                  fill="none" stroke="var(--primary-text-color,#fff)" stroke-width="2.4"/>
            <line x1="${CX}" y1="${CY + 2}" x2="${CX}" y2="${CY - 9}"
                  stroke="var(--primary-text-color,#fff)" stroke-width="1.8"/>
            <circle cx="${CX}" cy="${CY - 10.5}" r="2.6" fill="var(--primary-text-color,#fff)"/>
          </g>
          ${markers}
        </svg>
        <div class="status">
          <b>${status}</b>${progress ? ` - step ${progress.step}/${progress.total} @ ${Math.round(progress.azimuth)}&deg;` : ""}
          &middot; ${Math.round(az)}&deg;${connSsid ? ` &middot; ${connSsid}` : ""}${dbm != null ? ` &middot; ${dbm} dBm` : ""}
        </div>
        ${this._notice ? `<div class="banner ok"><span>${this._notice}</span><button class="x" data-dismiss="notice" title="Dismiss">&times;</button></div>` : ""}
        ${errShown ? `<div class="banner err"><span>&#9888;</span><span>${err}</span><button class="x" data-dismiss="err" title="Dismiss">&times;</button></div>` : ""}
        <div class="legend">
          <span><i style="background:#00a803"></i>&ge;-65</span>
          <span><i style="background:#f39c12"></i>&ge;-74</span>
          <span><i style="background:#d35400"></i>&ge;-80</span>
          <span><i style="background:#c0392b"></i>&lt;-80</span>
          <span>dBm</span>
        </div>
        <div class="btns">
          <button id="align" ${busy ? "disabled" : ""}>Auto-Align</button>
          <button id="survey" ${busy ? "disabled" : ""}>Wi-Fi Survey</button>
          <button id="home" ${busy ? "disabled" : ""}>Re-Home</button>
          <button id="abort" class="danger" ${busy ? "" : "disabled"}>Abort</button>
        </div>
        ${aps.length ? `<table>
          <tr><th>SSID</th><th>dBm</th><th>Angle</th><th>Ch</th><th>Sec</th><th></th></tr>
          ${rows}
          <tr id="manual-row" style="display:${this._manualOpen ? "table-row" : "none"}">
            <td><input id="mssid" placeholder="SSID" style="width:100%"/></td>
            <td colspan="3"><input id="mpass" type="password" placeholder="Password" style="width:100%"/></td>
            <td><button id="mconnect" class="join">Connect</button></td>
          </tr>
          <tr>
            <td colspan="5"><button id="manual" class="join" style="margin-top:6px">${this._manualOpen ? "Cancel" : "+ Connect to another network"}</button></td>
          </tr>
        </table>` : `<div class="hint">No APs mapped yet - run <b>Wi-Fi Survey</b> to scan the horizon.</div>`}
      </ha-card>`;

    const svg = this.shadowRoot.querySelector("svg");
    svg.addEventListener("pointerdown", (e) => this._down(e));
    svg.addEventListener("pointermove", (e) => this._move(e));
    svg.addEventListener("pointerup", (e) => this._up(e));
    this.shadowRoot.getElementById("align")?.addEventListener("click", () => this._svc("auto_align"));
    this.shadowRoot.getElementById("survey")?.addEventListener("click", () => this._svc("survey"));
    this.shadowRoot.getElementById("home")?.addEventListener("click", () => this._svc("home"));
    this.shadowRoot.getElementById("abort")?.addEventListener("click", () => this._svc("abort"));
    this.shadowRoot.querySelectorAll("g.ap").forEach((g) => {
      g.addEventListener("pointerdown", (e) => e.stopPropagation());
      g.addEventListener("click", () =>
        this._join(aps.find((a) => a.bssid === g.dataset.bssid))
      );
    });
    this.shadowRoot.querySelectorAll(".join[data-bssid]").forEach((b) =>
      b.addEventListener("click", () =>
        this._join(aps.find((a) => a.bssid === b.dataset.bssid))
      )
    );
    this.shadowRoot.querySelectorAll(".x[data-dismiss]").forEach((b) =>
      b.addEventListener("click", () => {
        if (b.dataset.dismiss === "err") this._dismissedErr = err;
        else this._notice = null;
        this._render();
      })
    );
    this.shadowRoot.getElementById("manual")?.addEventListener("click", () => {
      this._manualOpen = !this._manualOpen;
      this._render();
    });
    this.shadowRoot.getElementById("mconnect")?.addEventListener("click", () =>
      this._manualJoin()
    );
  }

  getCardSize() { return 8; }
  static getConfigElement() { return document.createElement("dish-aligner-card-editor"); }

  // Auto-guess entities by name so a fresh card mostly self-configures.
  static getStubConfig(hass) {
    const pick = (domain, needle, platform) =>
      Object.values(hass?.entities ?? {}).find(
        (e) =>
          e.entity_id.startsWith(domain + ".") &&
          e.entity_id.includes(needle) &&
          (!platform || e.platform === platform)
      )?.entity_id;
    const cfg = {
      angle_entity: pick("sensor", "current_angle", "esphome"),
      target_entity: pick("number", "azimuth_target", "esphome"),
      status_entity: pick("sensor", "aligner_status", "dish_aligner"),
      survey_entity: pick("sensor", "aligner_survey", "dish_aligner"),
      signal_entity: pick("sensor", "signal", "openwrt"),
      beamwidth: 50,
    };
    return Object.fromEntries(Object.entries(cfg).filter(([, v]) => v !== undefined));
  }
}

// platform = integration domain the entity must come from
const EDITOR_FIELDS = [
  { key: "angle_entity", label: "Azimuth sensor", domains: ["sensor"], platform: "esphome" },
  { key: "target_entity", label: "Aim target (number)", domains: ["number"], platform: "esphome" },
  { key: "status_entity", label: "Dish Aligner status", domains: ["sensor"], platform: "dish_aligner" },
  { key: "survey_entity", label: "Dish Aligner survey", domains: ["sensor"], platform: "dish_aligner" },
  { key: "signal_entity", label: "Signal strength (optional)", domains: ["sensor"], platform: "openwrt" },
];

class DishAlignerCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this.shadowRoot) {
      this.shadowRoot.querySelectorAll("ha-entity-picker").forEach((el) => {
        el.hass = hass;
        const f = EDITOR_FIELDS.find((x) => x.key === el.dataset.key);
        if (f) el.includeEntities = this._entitiesFor(f);
      });
    }
  }

  // entity_ids of `domains` owned by integration `platform` (registry `platform` key)
  _entitiesFor(f) {
    const ids = Object.values(this._hass?.entities ?? {})
      .filter(
        (e) =>
          f.domains.some((d) => e.entity_id.startsWith(d + ".")) &&
          (!f.platform || e.platform === f.platform)
      )
      .map((e) => e.entity_id)
      .sort();
    // empty list would hide EVERYTHING - fall back to domain-only filtering
    return ids.length ? ids : undefined;
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        .row { padding: 8px 0; display: block; }
        .label { display: block; font-size: 12px; color: var(--secondary-text-color); padding: 12px 0 4px; }
        input[type=number] { width: 80px; padding: 6px; font: inherit;
          color: var(--primary-text-color); background: var(--secondary-background-color);
          border: 1px solid var(--divider-color); border-radius: 4px; }
      </style>
      ${EDITOR_FIELDS.map(
        (f) => `<ha-entity-picker class="row" data-key="${f.key}"
          label="${f.label}" .includeDomains="${f.domains}" allow-custom-entity></ha-entity-picker>`
      ).join("")}
      <div class="row">
        <span class="label">Beamwidth (deg)</span>
        <input type="number" min="5" max="180" step="1" data-key="beamwidth"
          value="${this._config?.beamwidth ?? 50}">
      </div>`;
    this.shadowRoot.querySelectorAll("ha-entity-picker").forEach((el) => {
      el.hass = this._hass;
      const f = EDITOR_FIELDS.find((x) => x.key === el.dataset.key);
      if (f) el.includeEntities = this._entitiesFor(f);
      el.value = this._config?.[el.dataset.key] ?? "";
      el.addEventListener("value-changed", (ev) => this._changed(el.dataset.key, ev.detail.value));
    });
    this.shadowRoot.querySelector("input").addEventListener("change", (ev) =>
      this._changed("beamwidth", Number(ev.target.value) || 50)
    );
  }

  _changed(key, value) {
    const cfg = { ...this._config };
    if (value === undefined || value === "") delete cfg[key];
    else cfg[key] = value;
    this._config = cfg;
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: cfg }, bubbles: true, composed: true,
    }));
  }
}

customElements.define("dish-aligner-card", DishAlignerCard);
customElements.define("dish-aligner-card-editor", DishAlignerCardEditor);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "dish-aligner-card",
  name: "Dish Aligner",
  description: "Radial drag-to-aim dish card with Wi-Fi survey picker",
});
