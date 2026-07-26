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
const previous = document.querySelector("#previous");
const next = document.querySelector("#next");
const castButton = document.querySelector("#cast");
const castStatus = document.querySelector("#cast-status");

let csrfToken = "";
let playbackId = "";
let currentInfo = null;
let hls = null;
let playerEvents = null;
let lastSavedAt = 0;
let lastSavedPlaybackId = "";
let completed = false;
let changingEpisode = false;

let googleCastReady = false;
let castContext = null;
let remotePlayer = null;
let remoteController = null;
let castProgressTimer = null;
let castLoadInFlight = false;
let castEndHandled = false;

function formatTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function clearError() {
  error.textContent = "";
  error.hidden = true;
}

function showError(message) {
  loading.hidden = true;
  error.textContent = message;
  error.hidden = false;
}

function setCastStatus(message) {
  castStatus.textContent = message;
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

async function saveProgress(
  force = false,
  isComplete = false,
  positionOverride = null,
  durationOverride = null,
  targetPlaybackId = playbackId,
) {
  const positionValue =
    positionOverride === null ? video.currentTime : Number(positionOverride);
  const durationValue =
    durationOverride === null ? video.duration : Number(durationOverride);
  if (!targetPlaybackId || !Number.isFinite(positionValue)) {
    return false;
  }
  const now = Date.now();
  if (
    !force &&
    targetPlaybackId === lastSavedPlaybackId &&
    now - lastSavedAt < 10_000
  ) {
    return false;
  }
  lastSavedAt = now;
  lastSavedPlaybackId = targetPlaybackId;
  const body = JSON.stringify({
    playback_id: targetPlaybackId,
    position: Math.max(0, positionValue || 0),
    duration: Number.isFinite(durationValue) ? Math.max(0, durationValue) : 0,
    completed: isComplete,
  });
  try {
    await api("/api/progress", {
      method: "POST",
      body,
      keepalive: force,
    });
    return true;
  } catch {
    // A transient progress failure must never interrupt playback.
    return false;
  }
}

function googleCastConnected() {
  const state = castContext?.getCastState?.();
  return (
    googleCastReady &&
    state &&
    state !== window.cast.framework.CastState.NO_DEVICES_AVAILABLE &&
    state !== window.cast.framework.CastState.NOT_CONNECTED
  );
}

function teardownPlayer() {
  playerEvents?.abort();
  playerEvents = null;
  hls?.destroy();
  hls = null;
  video.pause();
  video.removeAttribute("src");
  video.load();
}

function updateEpisodeUi(info) {
  currentInfo = info;
  playbackId = info.playback_id;
  completed = false;
  lastSavedAt = 0;
  lastSavedPlaybackId = "";
  title.textContent = info.title;
  subtitle.textContent = [info.season, info.language].filter(Boolean).join(" · ");
  episode.textContent = `${info.episode} / ${info.total_episodes}`;
  source.textContent = info.source;
  progress.textContent = formatTime(info.start_position);
  previous.disabled = !info.has_previous;
  next.disabled = !info.has_next;
}

function attachPlayer(info, { autoplay = false } = {}) {
  teardownPlayer();
  updateEpisodeUi(info);
  clearError();
  loading.hidden = false;
  status.textContent = `Loading episode ${info.episode}…`;

  const events = new AbortController();
  playerEvents = events;
  const options = { signal: events.signal };
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
      if (data.fatal && !changingEpisode) {
        showError("The stream stopped. Try another episode or reopen it from the bot.");
      }
    });
  } else {
    video.src = streamUrl;
  }

  let resumeApplied = false;
  video.addEventListener(
    "loadedmetadata",
    () => {
      if (
        !resumeApplied &&
        info.start_position > 0 &&
        info.start_position < video.duration - 5
      ) {
        video.currentTime = info.start_position;
      }
      resumeApplied = true;
      loading.hidden = true;
      if (autoplay && !googleCastConnected()) {
        void video.play().catch(() => {
          setCastStatus("Tap play to start the next episode.");
        });
      }
    },
    options,
  );
  video.addEventListener(
    "timeupdate",
    () => {
      progress.textContent = formatTime(video.currentTime);
      void saveProgress(false, false);
    },
    options,
  );
  video.addEventListener(
    "pause",
    () => {
      if (!video.ended && !changingEpisode && !googleCastConnected()) {
        void saveProgress(true, false);
      }
    },
    options,
  );
  video.addEventListener(
    "ended",
    async () => {
      if (changingEpisode) return;
      completed = true;
      await saveProgress(true, true);
      if (info.has_next) {
        await changeEpisode(info.episode + 1, { autoplay: true });
      } else {
        setCastStatus("Season completed.");
      }
    },
    options,
  );
  video.addEventListener(
    "error",
    () => {
      if (!changingEpisode) {
        showError("This source cannot be played right now. Try another episode.");
      }
    },
    options,
  );
}

async function loadCurrentOnGoogleCast(info = currentInfo) {
  if (!info || !googleCastReady || castLoadInFlight) return;
  const session = castContext?.getCurrentSession?.();
  if (!session) return;
  castLoadInFlight = true;
  castEndHandled = false;
  try {
    const grant = await api("/api/cast", {
      method: "POST",
      body: JSON.stringify({ playback_id: info.playback_id }),
    });
    const mediaInfo = new window.chrome.cast.media.MediaInfo(
      grant.url,
      grant.content_type,
    );
    const metadata = new window.chrome.cast.media.GenericMediaMetadata();
    metadata.title = `${grant.title} · Episode ${grant.episode}`;
    metadata.subtitle = [info.season, info.language].filter(Boolean).join(" · ");
    mediaInfo.metadata = metadata;
    const request = new window.chrome.cast.media.LoadRequest(mediaInfo);
    request.autoplay = true;
    request.currentTime = Math.max(
      0,
      Number.isFinite(video.currentTime) ? video.currentTime : info.start_position,
    );
    await session.loadMedia(request);
    video.pause();
    setCastStatus("Casting to your TV.");
  } catch (reason) {
    setCastStatus(
      reason instanceof Error ? reason.message : "The TV could not load this stream.",
    );
  } finally {
    castLoadInFlight = false;
  }
}

async function handleRemoteCastFinished() {
  if (
    castEndHandled ||
    changingEpisode ||
    !currentInfo ||
    !remotePlayer
  ) {
    return;
  }
  const mediaSession = castContext?.getCurrentSession?.()?.getMediaSession?.();
  const finished =
    remotePlayer.playerState === window.chrome.cast.media.PlayerState.IDLE &&
    mediaSession?.idleReason === window.chrome.cast.media.IdleReason.FINISHED;
  if (!finished) return;
  castEndHandled = true;
  completed = true;
  await saveProgress(
    true,
    true,
    remotePlayer.currentTime,
    remotePlayer.duration,
  );
  if (currentInfo.has_next) {
    await changeEpisode(currentInfo.episode + 1, { autoplay: true });
  } else {
    setCastStatus("Season completed on TV.");
  }
}

function setupRemoteCastController() {
  if (remotePlayer || !window.cast?.framework) return;
  remotePlayer = new window.cast.framework.RemotePlayer();
  remoteController = new window.cast.framework.RemotePlayerController(remotePlayer);
  remoteController.addEventListener(
    window.cast.framework.RemotePlayerEventType.CURRENT_TIME_CHANGED,
    () => {
      if (googleCastConnected()) {
        progress.textContent = formatTime(remotePlayer.currentTime);
      }
    },
  );
  remoteController.addEventListener(
    window.cast.framework.RemotePlayerEventType.PLAYER_STATE_CHANGED,
    () => {
      void handleRemoteCastFinished();
    },
  );
  castProgressTimer = window.setInterval(() => {
    if (
      googleCastConnected() &&
      remotePlayer?.isMediaLoaded &&
      !changingEpisode &&
      currentInfo
    ) {
      void saveProgress(
        false,
        false,
        remotePlayer.currentTime,
        remotePlayer.duration,
      );
    }
  }, 10_000);
}

function initializeGoogleCast() {
  if (!window.cast?.framework || !window.chrome?.cast?.media) return;
  castContext = window.cast.framework.CastContext.getInstance();
  castContext.setOptions({
    receiverApplicationId:
      window.chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
    autoJoinPolicy: window.chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
  });
  castContext.addEventListener(
    window.cast.framework.CastContextEventType.SESSION_STATE_CHANGED,
    (event) => {
      const states = window.cast.framework.SessionState;
      if (
        event.sessionState === states.SESSION_STARTED ||
        event.sessionState === states.SESSION_RESUMED
      ) {
        setupRemoteCastController();
        castButton.disabled = false;
        void loadCurrentOnGoogleCast();
      } else if (event.sessionState === states.SESSION_ENDED) {
        if (remotePlayer && currentInfo) {
          const remoteTime = remotePlayer.currentTime;
          const remoteDuration = remotePlayer.duration;
          if (!completed) {
            void saveProgress(true, false, remoteTime, remoteDuration);
          }
          if (Number.isFinite(remoteTime) && remoteTime > 0) {
            video.currentTime = remoteTime;
          }
        }
        setCastStatus("Cast disconnected.");
      }
    },
  );
  googleCastReady = true;
  castButton.disabled = false;
  castButton.title = "Cast to a Google Cast device";
}

function setupCast() {
  const hasAirPlay =
    typeof video.webkitShowPlaybackTargetPicker === "function";
  const hasRemotePlayback =
    video.remote && typeof video.remote.prompt === "function";

  if (hasAirPlay || hasRemotePlayback) {
    castButton.disabled = false;
    castButton.title = hasAirPlay
      ? "AirPlay to a TV"
      : "Play on a remote screen";
  }

  if (hasRemotePlayback) {
    video.remote.onconnecting = () => setCastStatus("Connecting to the TV…");
    video.remote.onconnect = () => setCastStatus("Playing on the TV.");
    video.remote.ondisconnect = () => setCastStatus("Remote playback disconnected.");
    video.remote
      .watchAvailability((available) => {
        if (!googleCastReady && !hasAirPlay) {
          castButton.disabled = !available;
        }
      })
      .catch(() => {
        // Some browsers discover devices only after prompt() is called.
      });
  }

  if (window.location.protocol === "https:") {
    window.__onGCastApiAvailable = (available) => {
      if (available) initializeGoogleCast();
    };
    const script = document.createElement("script");
    script.src =
      "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1";
    script.async = true;
    script.onerror = () => {
      if (!hasAirPlay && !hasRemotePlayback) {
        castButton.title = "Cast is unavailable in this Telegram browser";
      }
    };
    document.head.append(script);
  }
}

async function changeEpisode(targetEpisode, { autoplay = false } = {}) {
  if (changingEpisode || !currentInfo) return;
  const target = Number(targetEpisode);
  if (
    !Number.isInteger(target) ||
    target < 1 ||
    target > currentInfo.total_episodes ||
    target === currentInfo.episode
  ) {
    return;
  }
  changingEpisode = true;
  previous.disabled = true;
  next.disabled = true;
  clearError();
  loading.hidden = false;
  status.textContent = `Preparing episode ${target}…`;
  try {
    if (!completed) {
      if (googleCastConnected() && remotePlayer?.isMediaLoaded) {
        await saveProgress(
          true,
          false,
          remotePlayer.currentTime,
          remotePlayer.duration,
        );
      } else {
        await saveProgress(true, false);
      }
    }
    const info = await api("/api/playback/episode", {
      method: "POST",
      body: JSON.stringify({ episode: target }),
    });
    attachPlayer(info, { autoplay: autoplay && !googleCastConnected() });
    if (googleCastConnected()) {
      await loadCurrentOnGoogleCast(info);
    }
  } catch (reason) {
    showError(
      reason instanceof Error ? reason.message : "The episode could not be loaded.",
    );
    previous.disabled = !currentInfo.has_previous;
    next.disabled = !currentInfo.has_next;
  } finally {
    changingEpisode = false;
  }
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
    attachPlayer(info);
    setupCast();
  } catch (reason) {
    showError(
      reason instanceof Error ? reason.message : "Playback could not be started.",
    );
  }
}

previous.addEventListener("click", () => {
  if (currentInfo?.has_previous) {
    void changeEpisode(currentInfo.episode - 1, { autoplay: true });
  }
});

next.addEventListener("click", () => {
  if (currentInfo?.has_next) {
    void changeEpisode(currentInfo.episode + 1, { autoplay: true });
  }
});

castButton.addEventListener("click", async () => {
  try {
    if (googleCastReady) {
      if (castContext.getCurrentSession()) {
        await loadCurrentOnGoogleCast();
      } else {
        await castContext.requestSession();
      }
    } else if (typeof video.webkitShowPlaybackTargetPicker === "function") {
      video.webkitShowPlaybackTargetPicker();
    } else if (video.remote?.prompt) {
      await video.remote.prompt();
    } else {
      setCastStatus("Cast is not supported in this Telegram browser.");
    }
  } catch {
    setCastStatus("No compatible TV was selected.");
  }
});

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

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "hidden" || completed) return;
  if (googleCastConnected() && remotePlayer?.isMediaLoaded) {
    void saveProgress(
      true,
      false,
      remotePlayer.currentTime,
      remotePlayer.duration,
    );
  } else {
    void saveProgress(true, false);
  }
});

window.addEventListener("pagehide", () => {
  if (!completed) {
    if (googleCastConnected() && remotePlayer?.isMediaLoaded) {
      void saveProgress(
        true,
        false,
        remotePlayer.currentTime,
        remotePlayer.duration,
      );
    } else {
      void saveProgress(true, false);
    }
  }
  if (castProgressTimer) window.clearInterval(castProgressTimer);
  playerEvents?.abort();
  hls?.destroy();
});

void initialize();
