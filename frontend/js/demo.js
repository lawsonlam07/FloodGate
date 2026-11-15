// Demo landing page: the problem in one screen (a drifting wall of every fictional site) and the way to the board.
// Screenshots and their list come from frontend/shoot_sites.py (img/sites/sites.json).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

if (new URLSearchParams(location.search).get("mock") === "1") {      // keep mock mode on the way to the board
  $("cta").href = $("brand").href = "index.html?mock=1";
}

const SPEEDS = [16, 21, 18, 23, 17, 20];     // px per second, per column: never in lockstep
const wall = $("wall");
let sites = [];
let columns = 0;

// Social, official and news in turn, each kind starting at a different scenario, so neighbours never match.
function interleave(list) {
  const kinds = ["social", "official", "news"];
  const lanes = kinds.map((k, i) => {
    const arr = list.filter((s) => s.kind === k);
    const at = Math.floor((i * arr.length) / kinds.length);
    return arr.slice(at).concat(arr.slice(0, at));
  });
  lanes.push(list.filter((s) => !kinds.includes(s.kind)));
  const out = [];
  for (let i = 0; i < Math.max(...lanes.map((l) => l.length)); i++) for (const l of lanes) if (l[i]) out.push(l[i]);
  return out;
}

function tile(s, copy) {
  const label = `${s.name} (${s.kind}, fictional): ${s.scenario}, ${s.region}`;
  return `<a class="tile" href="${esc(s.href)}" target="_blank" rel="noopener" title="${esc(label)}"${copy ? ' tabindex="-1" aria-hidden="true"' : ""}>`
    + `<img src="${esc(s.img)}" width="${s.w}" height="${s.h}" alt="${copy ? "" : esc(label)}" decoding="async">`
    + `<span class="cap"><span class="kind ${esc(s.kind)}">${esc(s.kind)}</span><span class="name">${esc(s.name)}</span><span class="lang">${esc(s.language)}</span></span></a>`;
}

// Deal tiles into the shortest column (heights balance, kinds mix), then repeat each column once for a seamless loop.
function build() {
  const n = Math.max(2, Math.min(6, Math.round(wall.clientWidth / 250)));
  if (n !== columns) {
    columns = n;
    const cols = Array.from({ length: n }, () => ({ items: [], h: 0 }));
    for (const s of interleave(sites)) {
      const c = cols.reduce((a, b) => (b.h < a.h ? b : a));
      c.items.push(s);
      c.h += s.h / s.w + 0.12;                 // + caption, in column widths
    }
    wall.innerHTML = cols.map((c) => `<div class="col"><div class="track">${c.items.map((s) => tile(s, false)).join("")}`
      + `${c.items.map((s) => tile(s, true)).join("")}</div></div>`).join("");
  }
  wall.querySelectorAll(".track").forEach((t, i) => {     // same speed whatever the column's height
    const dur = t.scrollHeight / 2 / SPEEDS[i % SPEEDS.length];
    t.style.animationDuration = `${dur.toFixed(1)}s`;
    t.style.animationDelay = `${(-dur * ((i * 0.37) % 1)).toFixed(1)}s`;
  });
}

// How many real official warnings (feeds, not scenarios) are on the board right now; else it says "5 real feeds".
fetch("api/incidents")
  .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
  .then((d) => {
    const n = (d.incidents || []).filter((i) => i.kind === "official_warning" && i.status !== "ended").length;
    if (n > 0) { $("n-real").textContent = n; $("real-label").innerHTML = "real warnings<br>live now"; }
  })
  .catch(() => {});

fetch("img/sites/sites.json")
  .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
  .then((list) => {
    sites = list;
    $("n-sites").textContent = list.length;
    $("n-emergencies").textContent = new Set(list.map((s) => s.scenario)).size;
    $("n-languages").textContent = new Set(list.map((s) => s.language)).size;
    build();
    let timer;
    window.addEventListener("resize", () => { clearTimeout(timer); timer = setTimeout(build, 150); });
  })
  .catch(() => {});                              // no wall: the pitch and the link to the board still work
