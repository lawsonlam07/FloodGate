// Demo page: board (left) + compact read-only "behind the scenes" (right); tabs on mobile.
import * as api from "./api.js";
import { setLive, onResume } from "./ui.js";

const $ = (id) => document.getElementById(id);
// In mock mode, pass the simulation switches through to both frames.
const q = new URLSearchParams(location.search);
const extra = api.MOCK ? ["showcase", "paused", "budget", "simdelay", "auth"].filter((k) => q.has(k)).map((k) => `&${k}=${encodeURIComponent(q.get(k))}`).join("") : "";
$("f-board").src = api.link("index.html") + (extra ? (api.MOCK ? extra : "") : "");
$("f-behind").src = api.link("admin.html?embed=1") + extra;

// Mobile tabs (on ≥1024px both panes are visible side by side).
document.querySelectorAll("[data-tab]").forEach((b) => {
  b.onclick = () => {
    const behind = b.dataset.tab === "behind";
    document.querySelectorAll("[data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t === b)));
    $("pane-board").classList.toggle("hidden", behind);
    $("pane-behind").classList.toggle("hidden", !behind);
    $("pane-behind").classList.toggle("flex-1", true);
  };
});

// "How it works" modal; the grouping model name comes from the server.
const open = () => $("how-modal").classList.remove("hidden");
const close = () => $("how-modal").classList.add("hidden");
$("how-btn").onclick = open;
$("how-close").onclick = close;
$("how-modal").addEventListener("click", (e) => { if (e.target.id === "how-modal") close(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
if (new URLSearchParams(location.search).get("how") === "1") open();   // screenshot aid
api.getHealth().then((h) => {
  if (h.grouping_model) $("how-model").textContent = h.llm_provider === "gemini" ? `Google Gemini, ${h.grouping_model}` : h.grouping_model;
}).catch(() => {});

// LIVE dot for the demo frame itself (the iframes have their own connections).
api.subscribe({}, setLive);
onResume(() => { $("f-board").contentWindow?.location.reload(); $("f-behind").contentWindow?.location.reload(); });
