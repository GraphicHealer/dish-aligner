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
const CX = 150, CY = 150, R = 110;

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
    // Don't blow away the DOM mid-drag or while a text field has focus -
    // a state update arriving mid-keystroke would clear what they're typing
    const editing = this.shadowRoot?.activeElement?.tagName === "INPUT";
    if (this._dragging || editing) {
      this._renderSkipped = true;
      return;
    }
    this._render();
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
    const [x, y] = this._pt(CX, CY, R - 8, this._dragAngle);
    const line = this.shadowRoot.querySelector(".target-line");
    const ind = this.shadowRoot.querySelector(".target-ind");
    if (line) {
      line.setAttribute("x2", x); line.setAttribute("y2", y);
    }
    if (ind) ind.setAttribute("transform", `rotate(${this._dragAngle} ${CX} ${CY})`);
    const num = this.shadowRoot.querySelector(".drag-num");
    if (num) num.textContent = `${Math.round(this._dragAngle)}°`;
  }
  _up(e) {
    if (!this._dragging) return;
    this._dragging = false;
    const az = Math.round(this._dragAngle);
    this._dragAngle = null;
    this._svc("aim", { azimuth: az });
    this._render();
  }

  _fmtSpeed(v) {
    if (v?.mbps == null) return "";
    return `↓${v.mbps}${v.upload_mbps != null ? ` ↑${v.upload_mbps}` : ""} ${v.unit || "Mbps"}`;
  }

  _setNotice(msg) {
    this._notice = msg;
    clearTimeout(this._noticeTimer);
    this._noticeTimer = setTimeout(() => {
      this._notice = null;
      if (!this._dragging) this._render();
    }, 6000);
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

  // Discrete signal bands as flat annular sectors centered on the AP's
  // bearing - red widest, yellow, then a green core - sized off a fixed
  // dB-per-degree falloff against the same thresholds as _dbmColor, so a
  // strong AP earns all three bands while a weak one may only rate red.
  _signalArc(ap) {
    if (ap?.azimuth == null || ap.dbm == null) return "";
    const K = 1.5, Rin = 42, Rout = 138, pk = ap.azimuth;
    let out = "";
    for (const [th, color] of [[-80, "#c0392b"], [-74, "#f39c12"], [-65, "#00a803"]]) {
      const half = Math.min(30, Math.max(th === -80 ? 1.2 : 0, (ap.dbm - th) / K));
      if (half <= 0) continue;
      const [x1, y1] = this._pt(CX, CY, Rout, pk - half);
      const [x2, y2] = this._pt(CX, CY, Rout, pk + half);
      const [x3, y3] = this._pt(CX, CY, Rin, pk + half);
      const [x4, y4] = this._pt(CX, CY, Rin, pk - half);
      out += `<path d="M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${Rout} ${Rout} 0 0 1 ${x2.toFixed(1)} ${y2.toFixed(1)} L ${x3.toFixed(1)} ${y3.toFixed(1)} A ${Rin} ${Rin} 0 0 0 ${x4.toFixed(1)} ${y4.toFixed(1)} Z" fill="${color}"/>`;
    }
    return out;
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

  // Start a join by expanding the AP's row for credentials - no prompts
  _join(ap) {
    this._unpin();
    this._listOpen = true;
    this._joinOpen = ap.bssid;
    this._jpass = null; // fresh field for this AP
    this._render();
  }

  _submitJoin(ap) {
    if (!ap) return;
    const password = this.shadowRoot.getElementById("jpass")?.value ?? "";
    this._jpass = null;
    this._joinOpen = null;
    this._pendingJoin = { ssid: ap.ssid, bssid: ap.bssid };
    this._svc("join", { ssid: ap.ssid, bssid: ap.bssid, password });
  }

  _manualJoin() {
    const ssid = this.shadowRoot.getElementById("mssid")?.value.trim();
    const password = this.shadowRoot.getElementById("mpass")?.value ?? "";
    if (!ssid) return;
    // Keep the SSID so a failed attempt doesn't make them retype it;
    // the password is deliberately not retained
    this._mssid = ssid;
    this._mpass = null;
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
    const connAp = aps.find(
      (a) => (connBssid && a.bssid === connBssid) || (connSsid && a.ssid === connSsid)
    );
    const shown = this._dragging ? this._dragAngle : target;
    const busy = status !== "idle" && status !== "unknown";

    // AP list collapse state: starts open unless already connected; a fresh
    // connection collapses it into map view, a new survey re-opens it so the
    // user can watch results fill in
    if (this._listOpen === undefined) this._listOpen = !connAp;
    if (connAp && !this._prevConnBssid &&
        status !== "surveying" && status !== "finding") {
      this._listOpen = false;
    }
    this._prevConnBssid = connAp?.bssid ?? null;
    if (status === "surveying" && this._prevStatus !== "surveying") this._listOpen = true;
    const mapMode = !!connAp && !this._listOpen;

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
        // Rejected association - reopen the credential row (or the manual
        // row for a typed SSID) so the user can retry right there
        if (pending && /password/i.test(connAttrs.last_error)) {
          if (pending.bssid) {
            this._listOpen = true;
            this._joinOpen = pending.bssid;
          } else {
            this._manualOpen = true;
          }
        }
      } else if (connSsid && connBssid && connBssid !== this._preOpBssid) {
        this._setNotice(`Connected to ${connSsid}`);
      } else if (this._prevStatus === "aligning" && connAttrs.last_align) {
        const la = connAttrs.last_align;
        const v = la.verified?.mbps != null ? la.verified : la.verified_ext;
        this._setNotice(`Aligned at ${Math.round(la.azimuth)}°` +
          (v?.mbps != null ? ` - ${this._fmtSpeed(v)}` : ""));
      }
    }
    this._prevBusy = busy;
    this._prevStatus = status;
    const err = this._callErr || connAttrs.last_error;
    const errShown = err && err !== this._dismissedErr;

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

    // Signal band: whichever AP is selected (pinned callout) gets concentric
    // rim arcs; in connected map view the connected AP's band is always on.
    const pinnedAp = this._pin ? aps.find((a) => a.bssid === this._pin) : null;
    const selAp = mapMode ? connAp : pinnedAp;
    const lobe = selAp ? this._signalArc(selAp) : "";

    // AP markers: bearing = best azimuth, radius = signal strength.
    // Overlapping markers get nudged outward so they stay clickable. In map
    // mode only the connected AP is drawn, over its beam lobe.
    let markers = "";
    const placed = [];
    for (const ap of (mapMode ? [connAp] : aps)) {
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
      r = Math.min(r, 130); // keep clear of the rim setpoint triangle
      placed.push({ az: ap.azimuth, r });
      const [mx, my] = this._pt(CX, CY, r, ap.azimuth);
      const isConn =
        (connBssid && ap.bssid === connBssid) ||
        (connSsid && ap.ssid === connSsid);
      markers += `<g class="ap${isConn ? " connected" : ""}" data-bssid="${ap.bssid}" data-vx="${mx}" data-vy="${my}">
        ${isConn ? `<circle class="ap-halo" cx="${mx}" cy="${my}" r="10"/>` : ""}
        <circle cx="${mx}" cy="${my}" r="5.5" fill="${this._dbmColor(ap.dbm)}"/>
      </g>`;
    }

    const rows = aps.map((ap) => `
      <tr data-bssid="${ap.bssid}">
        <td class="ssid">${ap.ssid || "<i>hidden</i>"}</td>
        <td style="color:${this._dbmColor(ap.dbm)}">${ap.dbm ?? "?"}</td>
        <td>${ap.azimuth ?? "?"}&deg;</td>
        <td>${ap.channel ?? "?"}</td>
        <td>${this._encLabel(ap)}</td>
        <td><button class="join" data-bssid="${ap.bssid}">Join</button></td>
      </tr>
      ${this._joinOpen === ap.bssid ? `
      <tr class="join-row">
        <td colspan="6">
          ${ap.crypto?.enabled === false
            ? `<span class="hint-inline">Open network - no password needed.</span>`
            : `<input id="jpass" type="password" placeholder="Password for ${ap.ssid}" value="${(this._jpass ?? "").replace(/"/g, "&quot;")}"/><button class="peek" data-for="jpass">Show</button>`}
          <button class="join" id="jgo">Connect</button>
          <button class="join jcancel" id="jcancel">Cancel</button>
        </td>
      </tr>` : ""}`).join("");

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
        .target-ind path { fill:var(--accent-color, var(--primary-color)); stroke:#fff; stroke-width:1.5; }
        .grid-ring { fill:none; stroke:var(--divider-color,#444); stroke-width:.7; stroke-dasharray:2 4; opacity:.6; }
        .grid-label { fill:var(--secondary-text-color,#888); font-size:8px; text-anchor:start; dominant-baseline:middle; }
        .dish { stroke-linecap:round; }
        .drag-num { fill:var(--primary-text-color,#fff); font-size:20px;
                    font-weight:600; text-anchor:middle;
                    dominant-baseline:middle; pointer-events:none; }
        .ap { cursor:pointer; }
        .ap circle:not(.ap-halo) { stroke:rgba(0,0,0,.4); stroke-width:1; }
        .ap.connected circle:not(.ap-halo) { stroke:#fff; stroke-width:2; }
        .ap-halo { fill:none; stroke:#fff; stroke-width:1.5; animation:halo 1.6s ease-in-out infinite; }
        @keyframes halo { 0%,100% { stroke-opacity:.9 } 50% { stroke-opacity:.25 } }
        .status { text-align:center; margin:8px 0 4px; font-size:14px; }
        .status b { text-transform:capitalize; }
        #az-in { cursor:pointer; border-bottom:1px dashed var(--secondary-text-color,#888); }
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
        .dial-wrap { position:relative; width:fit-content; margin:0 auto; }
        .tip { position:absolute; transform:translate(-50%, calc(-100% - 14px));
               background:var(--card-background-color,#fff); color:var(--primary-text-color,#111);
               border:1px solid var(--divider-color,#ccc); border-radius:8px;
               padding:8px 10px 9px; font-size:12px; min-width:140px; z-index:10;
               box-shadow:0 4px 14px rgba(0,0,0,.35); }
        .tip::after { content:""; position:absolute; top:100%; left:50%;
                      transform:translateX(-50%); border:7px solid transparent;
                      border-top-color:var(--card-background-color,#fff); }
        .tip .trow { color:var(--secondary-text-color,#777); margin-top:2px; }
        .tip .tip-join { margin-top:7px; }
        .tip .tx { position:absolute; top:1px; right:7px; background:transparent;
                   color:var(--secondary-text-color,#888); padding:0; font-size:15px; }
        tr[data-bssid] { cursor:pointer; }
        .join-row td { padding:6px; background:var(--secondary-background-color,rgba(128,128,128,.08)); }
        .join-row input { padding:5px 9px; font-size:13px; margin-right:6px;
                          border:1px solid var(--divider-color,#ccc); border-radius:6px;
                          width:52%; background:var(--card-background-color,#fff);
                          color:var(--primary-text-color,#111); }
        .join-row .hint-inline { font-size:12px; color:var(--secondary-text-color,#888); margin-right:8px; }
        .join-row button { margin-left:4px; }
        .join-row .jcancel { background:var(--secondary-text-color,#888); }
        .peek { background:transparent; color:var(--secondary-text-color,#888);
                padding:3px 6px; font-size:12px; }
        tr[data-bssid]:hover td { background:var(--secondary-background-color,rgba(128,128,128,.12)); }
        .aplist-head { width:100%; text-align:left; padding:7px 10px; margin-top:8px;
                       border-radius:6px; font-size:13px;
                       background:var(--secondary-background-color,#e8e8e8);
                       color:var(--primary-text-color,#111); }
        .aplist-head .caret { display:inline-block; margin-right:6px;
                              transition:transform .15s; }
      </style>
      <ha-card style="position:relative">
        <div class="dial-wrap">
        <svg viewBox="0 0 300 300">
          <circle cx="${CX}" cy="${CY}" r="${R}" class="ring"/>
          ${grid}
          ${ticks}${labels}
          <path class="wedge" d="${this._wedge(CX, CY, R - 4, az, this._beamwidth)}"/>
          <line class="beam" x1="${CX}" y1="${CY}" x2="${ax}" y2="${ay}"/>
          ${shown != null ? `<line class="target-line" x1="${CX}" y1="${CY}" x2="${tx}" y2="${ty}"/>
          <g class="target-ind" transform="rotate(${shown} ${CX} ${CY})">
            <path d="M ${CX - 8} ${CY - 146} L ${CX + 8} ${CY - 146} L ${CX} ${CY - 133} Z"/>
          </g>` : ""}
          <g class="dish" transform="rotate(${az} ${CX} ${CY})">
            <path d="M ${CX - 10} ${CY + 2} Q ${CX} ${CY + 14} ${CX + 10} ${CY + 2}"
                  fill="none" stroke="var(--primary-text-color,#fff)" stroke-width="2.4"/>
            <line x1="${CX}" y1="${CY + 2}" x2="${CX}" y2="${CY - 9}"
                  stroke="var(--primary-text-color,#fff)" stroke-width="1.8"/>
            <circle cx="${CX}" cy="${CY - 10.5}" r="2.6" fill="var(--primary-text-color,#fff)"/>
          </g>
          <g class="lobes">${lobe}</g>
          ${markers}
          <text class="drag-num" x="${CX}" y="${CY + 32}" opacity="${this._dragging ? 1 : 0}">${this._dragging ? `${Math.round(this._dragAngle)}°` : ""}</text>
        </svg>
        </div>
        <div class="status">
          <b>${status}</b>${progress ? ` - step ${progress.step}/${progress.total} @ ${Math.round(progress.azimuth)}&deg;` : ""}
          &middot; <span id="az-in" title="Click to enter a bearing">${Math.round(az)}&deg;</span>${connSsid ? ` &middot; ${connSsid}` : ""}${dbm != null ? ` &middot; ${dbm} dBm` : ""}${(() => { const v = connAttrs.last_align?.verified?.mbps != null ? connAttrs.last_align.verified : connAttrs.last_align?.verified_ext; return v?.mbps != null ? ` &middot; ${this._fmtSpeed(v)}` : ""; })()}
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
        ${aps.length ? `
        <button id="aplist-toggle" class="aplist-head"><span class="caret" style="transform:rotate(${this._listOpen ? 90 : 0}deg)">&#9656;</span>Survey results &middot; ${aps.length} AP${aps.length === 1 ? "" : "s"}${connAp ? ` &middot; connected: ${connAp.ssid}` : ""}</button>
        <div style="display:${this._listOpen ? "block" : "none"}">
        <table>
          <tr><th>SSID</th><th>dBm</th><th>Angle</th><th>Ch</th><th>Sec</th><th></th></tr>
          ${rows}
          <tr id="manual-row" style="display:${this._manualOpen ? "table-row" : "none"}">
            <td><input id="mssid" placeholder="SSID" style="width:100%" value="${(this._mssid ?? "").replace(/"/g, "&quot;")}"/></td>
            <td colspan="3"><input id="mpass" type="password" placeholder="Password" style="width:68%" value="${(this._mpass ?? "").replace(/"/g, "&quot;")}"/><button class="peek" data-for="mpass">Show</button></td>
            <td><button id="mconnect" class="join">Connect</button></td>
          </tr>
          <tr>
            <td colspan="5"><button id="manual" class="join" style="margin-top:6px">${this._manualOpen ? "Cancel" : "+ Connect to another network"}</button></td>
          </tr>
        </table>
        </div>` : `<div class="hint">No APs mapped yet - run <b>Wi-Fi Survey</b> to scan the horizon.</div>`}
      </ha-card>`;

    const svg = this.shadowRoot.querySelector("svg");
    svg.addEventListener("pointerdown", (e) => this._down(e));
    svg.addEventListener("pointermove", (e) => this._move(e));
    svg.addEventListener("pointerup", (e) => this._up(e));
    this.shadowRoot.getElementById("align")?.addEventListener("click", () => this._svc("auto_align"));
    this.shadowRoot.getElementById("survey")?.addEventListener("click", () => this._svc("survey"));
    this.shadowRoot.getElementById("home")?.addEventListener("click", () => this._svc("home"));
    this.shadowRoot.getElementById("abort")?.addEventListener("click", () => this._svc("abort"));
    this.shadowRoot.getElementById("az-in")?.addEventListener("click", () => {
      const v = window.prompt("Aim to azimuth (0-359\u00b0):",
        String(Math.round(target ?? az)));
      if (v === null) return;
      const n = Number(v);
      if (!Number.isFinite(n) || n < 0 || n > 359) {
        this._callErr = "Azimuth must be a number from 0 to 359";
        this._render();
        return;
      }
      this._svc("aim", { azimuth: Math.round(n) });
    });
    this.shadowRoot.querySelectorAll("g.ap").forEach((g) => {
      const ap = aps.find((a) => a.bssid === g.dataset.bssid);
      g.addEventListener("pointerdown", (e) => e.stopPropagation());
      g.addEventListener("mouseenter", () => {
        if (!this._pin) { this._tipAtMarker(ap, g); this._lobeHover(ap); }
      });
      g.addEventListener("mouseleave", () => {
        if (!this._pin) { this._hideTip(); this._lobeHover(null); }
      });
      g.addEventListener("click", () => {
        if (this._pin === ap.bssid) { this._unpin(); this._join(ap); }
        else { this._pin = ap.bssid; this._tipAtMarker(ap, g); }
      });
    });
    this.shadowRoot.querySelectorAll("tr[data-bssid]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("button,input")) return;
        const ap = aps.find((a) => a.bssid === tr.dataset.bssid);
        const g = this.shadowRoot.querySelector(`g.ap[data-bssid="${ap.bssid}"]`);
        this._pin = ap.bssid;
        if (g) this._tipAtMarker(ap, g);
        else this._tipAtEl(ap, tr);
      });
    });
    this.shadowRoot.querySelectorAll(".join[data-bssid]").forEach((b) =>
      b.addEventListener("click", () =>
        this._join(aps.find((a) => a.bssid === b.dataset.bssid))
      )
    );
    this.shadowRoot.querySelector("ha-card").addEventListener("click", (e) => {
      if (this._pin && !e.target.closest(".tip") && !e.target.closest("g.ap") &&
          !e.target.closest("tr[data-bssid]")) this._unpin();
    });
    // Re-show a pinned callout after this render pass
    if (this._pin) {
      const ap = aps.find((a) => a.bssid === this._pin);
      const g = this.shadowRoot.querySelector(`g.ap[data-bssid="${this._pin}"]`);
      if (ap && g) this._tipAtMarker(ap, g); else this._pin = null;
    }
    this.shadowRoot.querySelectorAll(".x[data-dismiss]").forEach((b) =>
      b.addEventListener("click", () => {
        if (b.dataset.dismiss === "err") this._dismissedErr = err;
        else this._notice = null;
        this._render();
      })
    );
    this.shadowRoot.getElementById("aplist-toggle")?.addEventListener("click", () => {
      this._listOpen = !this._listOpen;
      this._render();
    });
    this.shadowRoot.getElementById("jgo")?.addEventListener("click", () =>
      this._submitJoin(aps.find((a) => a.bssid === this._joinOpen))
    );
    this.shadowRoot.getElementById("jcancel")?.addEventListener("click", () => {
      this._joinOpen = null;
      this._jpass = null;
      this._render();
    });
    this.shadowRoot.getElementById("jpass")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.shadowRoot.getElementById("jgo")?.click();
    });
    this.shadowRoot.getElementById("jpass")?.focus();

    // Keep typed values as drafts so a re-render can't lose them, and
    // flush any update skipped while a field had focus once it blurs
    for (const [id, key] of [["mssid", "_mssid"], ["mpass", "_mpass"], ["jpass", "_jpass"]]) {
      this.shadowRoot.getElementById(id)?.addEventListener("input", (e) => {
        this[key] = e.target.value;
      });
    }
    this.shadowRoot.querySelectorAll("input").forEach((inp) =>
      inp.addEventListener("focusout", () => {
        if (this._renderSkipped) {
          this._renderSkipped = false;
          this._render();
        }
      })
    );
    this.shadowRoot.querySelectorAll(".peek").forEach((b) =>
      b.addEventListener("click", () => {
        const inp = this.shadowRoot.getElementById(b.dataset.for);
        if (!inp) return;
        const show = inp.type === "password";
        inp.type = show ? "text" : "password";
        b.textContent = show ? "Hide" : "Show";
        inp.focus();
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

  // -- AP callout bubble ----------------------------------------------------

  _tip(ap, left, top, host) {
    this._hideTip();
    const tip = document.createElement("div");
    tip.className = "tip";
    tip.style.left = left;
    tip.style.top = top;
    tip.innerHTML = `<button class="tx" title="Close">&times;</button>
      <b>${ap.ssid || "hidden"}</b>
      <div class="trow">${ap.dbm ?? "?"} dBm &middot; ${ap.azimuth ?? "?"}&deg; &middot; ch ${ap.channel ?? "?"}</div>
      <div class="trow">${this._encLabel(ap)} &middot; ${ap.bssid}</div>
      <button class="join tip-join">Join</button>`;
    tip.querySelector(".tip-join").addEventListener("click", (e) => {
      e.stopPropagation();
      this._unpin();
      this._join(ap);
    });
    tip.querySelector(".tx").addEventListener("click", (e) => {
      e.stopPropagation();
      this._unpin();
    });
    tip.addEventListener("click", (e) => e.stopPropagation());
    host.appendChild(tip);
  }

  _tipAtMarker(ap, g) {
    this._tip(ap, (+g.dataset.vx) / 3 + "%", (+g.dataset.vy) / 3 + "%",
      this.shadowRoot.querySelector(".dial-wrap"));
  }

  // Anchor to a table row (AP has no marker on the dial)
  _tipAtEl(ap, el) {
    this._tip(ap,
      el.offsetLeft + el.offsetWidth / 2 + "px",
      el.offsetTop + "px",
      this.shadowRoot.querySelector("ha-card"));
  }

  // Inject a signal band into the .lobes group without a full re-render -
  // used while hovering a marker (the pinned/connected band is baked in)
  _lobeHover(ap) {
    const lobes = this.shadowRoot?.querySelector(".lobes");
    if (!lobes) return;
    lobes.querySelector(".hover-lobe")?.remove();
    if (ap) {
      lobes.insertAdjacentHTML(
        "beforeend",
        `<g class="hover-lobe">${this._signalArc(ap)}</g>`
      );
    }
  }

  _hideTip() { this.shadowRoot?.querySelector(".tip")?.remove(); }
  _unpin() { this._pin = null; this._hideTip(); }

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
