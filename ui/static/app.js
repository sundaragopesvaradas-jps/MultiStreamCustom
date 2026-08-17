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

function setMeetingStatus(state, message) {
  const el = document.getElementById("meeting-status");
  if (!el) return;
  el.dataset.state = state;
  el.textContent = message;
}

async function refreshMeetingStatus() {
  const el = document.getElementById("meeting-status");
  const input = document.getElementById("meeting_id");
  if (!el || !input) return;

  if (el.dataset.zoomReady !== "1") {
    setMeetingStatus("unconfigured", "Zoom API is not configured.");
    return;
  }

  const meetingId = input.value.trim();
  if (!meetingId) {
    setMeetingStatus("missing", "No meeting ID entered.");
    return;
  }

  setMeetingStatus("checking", "Checking meeting status…");
  try {
    const url =
      "/api/meeting-status?meeting_id=" + encodeURIComponent(meetingId);
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) {
      setMeetingStatus("error", "Could not check meeting status.");
      return;
    }
    const data = await res.json();
    setMeetingStatus(data.state || "error", data.message || "Unknown status.");
  } catch {
    setMeetingStatus("error", "Could not check meeting status.");
  }
}

(function initMeetingStatus() {
  const input = document.getElementById("meeting_id");
  const el = document.getElementById("meeting-status");
  if (!input || !el) return;

  let debounce = null;
  const schedule = () => {
    clearTimeout(debounce);
    debounce = setTimeout(refreshMeetingStatus, 350);
  };

  refreshMeetingStatus();
  input.addEventListener("change", refreshMeetingStatus);
  input.addEventListener("blur", refreshMeetingStatus);
  input.addEventListener("input", schedule);
})();
