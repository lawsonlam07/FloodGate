// Photos from the sources (backend/images.py): a strip on the incident page, plus a small thumbnail on each report
// whose post carried a photo. Photos are context, never proof: always credited and labelled "unverified".
import { esc, icon } from "./ui.js";

let photos = [];
let observer = null;

export function renderPhotos(detail) {
  photos = (detail?.images || []).filter((im) => im.url);
  const el = document.getElementById("photos");
  if (!el) return;
  if (!photos.length || detail?.incident?.kind === "official_warning") {
    el.classList.add("hidden");
    el.innerHTML = "";
  } else {
    el.classList.remove("hidden");
    el.innerHTML = `
    <div class="card p-4">
      <div class="flex items-baseline justify-between gap-2">
        <h2 class="text-base font-bold tracking-tight flex items-center gap-2">${icon("photo_library", "text-authority")}Photos from the sources</h2>
        <span class="label text-faint text-right">unverified · not used for verdicts</span>
      </div>
      <div class="mt-3 flex gap-3 overflow-x-auto scroll-x pb-2">${photos.map(photoCard).join("")}</div>
    </div>`;
  }
  watchTimeline();
  decorate();
}

function photoCard(im) {
  return `
  <a href="${esc(im.source_url || im.url)}" target="_blank" rel="noopener" class="shrink-0 w-64 group" title="${esc(im.alt)}">
    <img src="${esc(im.url)}" alt="${esc(im.alt)}" loading="lazy" width="256" height="144"
      class="w-64 h-36 object-cover rounded border border-line group-hover:border-line2 bg-elevated">
    <span class="mt-1 block text-xs text-muted truncate">from ${esc(im.source_name)} · unverified</span>
  </a>`;
}

// Thumbnails on reports: re-applied whenever the timeline re-renders.
function decorate() {
  for (const im of photos) {
    for (const id of im.item_ids || []) {
      const li = document.querySelector(`[data-item="${CSS.escape(id)}"]`);
      const btn = li?.querySelector("[data-toggle]");
      if (!btn || btn.querySelector("[data-photo-thumb]")) continue;
      const img = document.createElement("img");
      img.src = im.url;
      img.alt = im.alt || "";
      img.loading = "lazy";
      img.dataset.photoThumb = "1";
      img.title = `Photo posted with this report by ${im.source_name} (unverified)`;
      img.className = "w-20 h-12 object-cover rounded border border-line shrink-0 mt-0.5";
      btn.insertBefore(img, btn.lastElementChild);       // before the expand chevron
    }
  }
}

function watchTimeline() {
  const tl = document.getElementById("timeline");
  if (!tl || observer) return;
  observer = new MutationObserver(() => decorate());
  observer.observe(tl, { childList: true, subtree: true });
}
