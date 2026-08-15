document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const id = btn.getAttribute("data-copy");
    const el = document.getElementById(id);
    if (!el) return;
    const text = el.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
      const prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => {
        btn.textContent = prev;
      }, 1500);
    } catch {
      btn.textContent = "Copy failed";
    }
  });
});

function formatLiveStatus(data) {
  if (data.error) return data.error;
  if (data.publishing) {
    const yt = data.youtube_running
      ? "pushing"
      : data.youtube_enabled
        ? "starting…"
        : "off";
    const fb = data.facebook_running
      ? "pushing"
      : data.facebook_enabled
        ? "starting…"
        : "off";
    const mode = data.enhance_enabled ? "Enhance on." : "Direct copy.";
    return `Live — Zoom is publishing. YouTube ${yt}, Facebook ${fb}. ${mode}`;
  }
  const hint = data.enhance_enabled ? " Enhance will apply on the next push." : "";
  return `Idle — no Zoom stream is publishing.${hint}`;
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status", { credentials: "same-origin" });
    if (!res.ok) return;
    const data = await res.json();
    const live = document.getElementById("live-status");
    if (live) live.textContent = formatLiveStatus(data);
  } catch {
    /* ignore transient network errors */
  }
}

if (document.getElementById("live-status")) {
  setInterval(refreshStatus, 5000);
}
