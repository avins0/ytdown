const form = document.querySelector("#download-form");
const jobsEl = document.querySelector("#jobs");
const template = document.querySelector("#job-template");
const refreshButton = document.querySelector("#refresh");
const shutdownButton = document.querySelector("#shutdown");
const healthEl = document.querySelector("#health");
const urlInput = document.querySelector("#url");
const playlistInput = document.querySelector("#playlist");
const outputDirInput = document.querySelector("#output-dir");
const submitButton = form.querySelector("button[type='submit']");

let pollTimer = null;
let playlistTouched = false;

function prettyDate(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusLabel(status) {
  const labels = {
    queued: "Queued",
    running: "Running",
    complete: "Complete",
    error: "Error",
    cancelled: "Cancelled",
  };
  return labels[status] || status;
}

function formatJobTitle(job) {
  return job.title || job.url;
}

function formatJobMeta(job) {
  const parts = [
    job.mediaType.toUpperCase(),
    job.playlist ? "Playlist" : "Single item",
    prettyDate(job.createdAt),
  ];

  if (job.filename) {
    parts.push(job.filename);
  }

  return parts.join(" / ");
}

function looksLikePlaylist(urlValue) {
  try {
    const url = new URL(urlValue);
    const path = url.pathname.toLowerCase().replace(/\/$/, "");
    return path.endsWith("/playlist") || (url.searchParams.has("list") && !url.searchParams.has("v"));
  } catch {
    return false;
  }
}

function renderJobs(jobs) {
  jobsEl.innerHTML = "";

  if (!jobs.length) {
    jobsEl.innerHTML = `
      <div class="empty">
        <div>
          <h3>No downloads yet</h3>
          <p>Paste a YouTube or YouTube Music link to start a job.</p>
        </div>
      </div>
    `;
    return;
  }

  for (const job of jobs) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.add(job.status);
    node.querySelector(".job-title").textContent = formatJobTitle(job);
    node.querySelector(".job-meta").textContent = formatJobMeta(job);

    const pill = node.querySelector(".status-pill");
    pill.textContent = statusLabel(job.status);
    pill.classList.add(job.status);

    const progress = job.status === "complete" ? 100 : Math.max(0, Math.min(100, job.progress || 0));
    node.querySelector(".progress-bar").style.width = `${progress}%`;

    const message = job.error || job.message || "";
    node.querySelector(".job-message").textContent = `${message}${progress ? ` / ${progress.toFixed(0)}%` : ""}`;
    node.querySelector(".job-speed").textContent = job.speed || "";
    node.querySelector(".job-eta").textContent = job.eta ? `ETA ${job.eta}` : "";
    node.querySelector(".job-log pre").textContent = (job.logs || []).join("\n");

    node.querySelector(".cancel-job").addEventListener("click", () => cancelJob(job.id));
    jobsEl.appendChild(node);
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Request failed with ${response.status}`);
  }
  return payload;
}

async function refreshJobs() {
  try {
    const payload = await fetchJson("/api/jobs");
    renderJobs(payload.jobs || []);
  } catch (error) {
    renderJobs([
      {
        id: "local-error",
        url: "Unable to load jobs",
        mediaType: "mp4",
        playlist: false,
        createdAt: Date.now() / 1000,
        status: "error",
        error: error.message,
        logs: [],
      },
    ]);
  }
}

async function loadHealth() {
  try {
    const health = await fetchJson("/api/health");
    outputDirInput.placeholder = health.defaultOutputDir;
    if (!outputDirInput.value) {
      outputDirInput.value = health.defaultOutputDir;
    }

    const missing = [];
    if (!health.ytDlpAvailable) missing.push("yt-dlp");
    if (!health.ffmpegAvailable) missing.push("FFmpeg");

    healthEl.classList.toggle("ready", missing.length === 0);
    healthEl.classList.toggle("warn", missing.length > 0);
    healthEl.querySelector("span:last-child").textContent =
      missing.length === 0
        ? `Ready: yt-dlp ${health.ytDlpVersion || ""}`.trim()
        : `Missing ${missing.join(" and ")}`;
  } catch (error) {
    healthEl.classList.add("warn");
    healthEl.querySelector("span:last-child").textContent = "Tool check failed";
  }
}

async function cancelJob(jobId) {
  try {
    await fetchJson(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    await refreshJobs();
  } catch (error) {
    window.alert(error.message);
  }
}

async function shutdownServer() {
  const shouldShutdown = window.confirm("Shutdown the downloader server?");
  if (!shouldShutdown) return;

  shutdownButton.disabled = true;
  try {
    await fetchJson("/api/shutdown", { method: "POST" });
    window.clearInterval(pollTimer);
    healthEl.classList.remove("ready");
    healthEl.classList.add("warn");
    healthEl.querySelector("span:last-child").textContent = "Server stopped";
    jobsEl.insertAdjacentHTML(
      "afterbegin",
      `<div class="shutdown-banner">The downloader server has been shut down. You can close this tab.</div>`,
    );
  } catch (error) {
    shutdownButton.disabled = false;
    window.alert(error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;

  const formData = new FormData(form);
  const payload = {
    url: formData.get("url"),
    mediaType: formData.get("mediaType"),
    playlist: formData.get("playlist") === "on",
    outputDir: formData.get("outputDir"),
  };

  try {
    await fetchJson("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    form.reset();
    playlistTouched = false;
    outputDirInput.value = outputDirInput.placeholder;
    await refreshJobs();
  } catch (error) {
    window.alert(error.message);
  } finally {
    submitButton.disabled = false;
  }
});

urlInput.addEventListener("input", () => {
  if (!playlistTouched) {
    playlistInput.checked = looksLikePlaylist(urlInput.value);
  }
});

playlistInput.addEventListener("change", () => {
  playlistTouched = true;
});

refreshButton.addEventListener("click", refreshJobs);
shutdownButton.addEventListener("click", shutdownServer);

loadHealth();
refreshJobs();
pollTimer = window.setInterval(refreshJobs, 1200);
window.addEventListener("beforeunload", () => window.clearInterval(pollTimer));
