const telegram = window.Telegram?.WebApp;
const Hls = window.Hls;
const video = document.querySelector("#video");
const loading = document.querySelector("#loading");
const status = document.querySelector("#status");
const error = document.querySelector("#error");
const title = document.querySelector("#title");
const subtitle = document.querySelector("#subtitle");
const episode = document.querySelector("#episode");
const source = document.querySelector("#source");
const progress = document.querySelector("#progress");
const fullscreen = document.querySelector("#fullscreen");

let csrfToken = "";
let playbackId = "";
let hls = null;
let lastSavedAt = 0;
let completed = false;

function formatTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function showError(message) {
  loading.hidden = true;
  error.textContent = message;
  error.hidden = false;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }
  if (csrfToken && options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers,
  });
  if (!response.ok) {
    const message = (await response.text()).slice(0, 240);
    throw new Error(message || `Request failed (${response.status})`);
  }
  return response.json();
}

async function saveProgress(force = false, isComplete = false) {
  if (!playbackId || !Number.isFinite(video.currentTime)) {
    return;
  }
  const now = Date.now();
  if (!force && now - lastSavedAt < 10_000) {
    return;
  }
  lastSavedAt = now;
  const body = JSON.stringify({
    playback_id: playbackId,
    position: video.currentTime || 0,
    duration: Number.isFinite(video.duration) ? video.duration : 0,
    completed: isComplete,
  });
  try {
    await api("/api/progress", {
      method: "POST",
      body,
      keepalive: force,
    });
  } catch {
    // A transient progress failure must never interrupt playback.
  }
}

function attachPlayer(info) {
  const streamUrl = info.stream_url;
  const useHls = info.kind === "hls";
  if (useHls && Hls?.isSupported()) {
    hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      maxBufferLength: 30,
      maxMaxBufferLength: 90,
      xhrSetup(xhr) {
        xhr.withCredentials = true;
      },
    });
    hls.loadSource(streamUrl);
    hls.attachMedia(video);
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        showError("The stream stopped. Close the player and try again.");
      }
    });
  } else {
    video.src = streamUrl;
  }

  let resumeApplied = false;
  video.addEventListener("loadedmetadata", () => {
    if (!resumeApplied && info.start_position > 0 && info.start_position < video.duration - 5) {
      video.currentTime = info.start_position;
    }
    resumeApplied = true;
    loading.hidden = true;
  });
  video.addEventListener("timeupdate", () => {
    progress.textContent = formatTime(video.currentTime);
    void saveProgress(false, false);
  });
  video.addEventListener("pause", () => {
    if (!video.ended) void saveProgress(true, false);
  });
  video.addEventListener("ended", () => {
    completed = true;
    void saveProgress(true, true);
  });
  video.addEventListener("error", () => {
    showError("This source cannot be played right now. Try again from the bot.");
  });
}

async function initialize() {
  if (!telegram || !telegram.initData) {
    showError("Open this player from the AniStream Telegram bot.");
    return;
  }
  telegram.ready();
  telegram.expand();
  telegram.setHeaderColor?.("secondary_bg_color");
  telegram.enableClosingConfirmation?.();

  const launchToken = new URLSearchParams(window.location.search).get("launch") || "";
  try {
    if (launchToken) {
      const auth = await api("/api/auth/telegram", {
        method: "POST",
        body: JSON.stringify({
          init_data: telegram.initData,
          launch_token: launchToken,
        }),
      });
      csrfToken = auth.csrf_token;
      history.replaceState(null, "", window.location.pathname);
    } else {
      const current = await api("/api/session");
      csrfToken = current.csrf_token;
    }
    status.textContent = "Finding the best available source…";
    const info = await api("/api/playback");
    playbackId = info.playback_id;
    title.textContent = info.title;
    subtitle.textContent = [info.season, info.language].filter(Boolean).join(" · ");
    episode.textContent = String(info.episode);
    source.textContent = info.source;
    attachPlayer(info);
  } catch (reason) {
    showError(reason instanceof Error ? reason.message : "Playback could not be started.");
  }
}

fullscreen.addEventListener("click", async () => {
  try {
    if (telegram?.requestFullscreen) {
      telegram.requestFullscreen();
    } else if (video.requestFullscreen) {
      await video.requestFullscreen();
    }
  } catch {
    // Fullscreen is optional and platform-dependent.
  }
});

window.addEventListener("pagehide", () => {
  if (!completed) void saveProgress(true, false);
  hls?.destroy();
});

void initialize();
