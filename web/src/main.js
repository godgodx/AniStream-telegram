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
const pictureInPictureButton = document.querySelector("#picture-in-picture");
const previous = document.querySelector("#previous");
const next = document.querySelector("#next");
const autoplayStatus = document.querySelector("#autoplay-status");
const playbackToolbar = document.querySelector("#playback-toolbar");
const episodePickerShell = document.querySelector("#episode-picker-shell");
const episodePicker = document.querySelector("#episode-picker");
const sourcePickerShell = document.querySelector("#source-picker-shell");
const sourcePicker = document.querySelector("#source-picker");
const castButton = document.querySelector("#cast");
const castStatus = document.querySelector("#cast-status");
const playbackOptionsButton = document.querySelector("#playback-options");
const streamControls = document.querySelector("#stream-controls");
const qualityPicker = document.querySelector("#quality-picker");
const audioPicker = document.querySelector("#audio-picker");
const subtitlePicker = document.querySelector("#subtitle-picker");
const NEXT_PREFETCH_WINDOW_SECONDS = 5 * 60;
const PREFETCH_EXPIRY_MARGIN_MS = 30_000;
const PREFETCH_RETRY_DELAY_MS = 60_000;

let csrfToken = "";
let playbackId = "";
let currentInfo = null;
let hls = null;
let playerEvents = null;
let lastSavedAt = 0;
let lastSavedPlaybackId = "";
let progressEventSequence = 0;
let lastProgressObservation = null;
let completed = false;
let changingEpisode = false;
let changingSource = false;
let preferredQualityHeight = 0;
let preferredAudio = "";
let preferredSubtitle = "";
let streamControlsExpanded = false;
let prefetchedNext = null;
let prefetchedNextExpiresAt = 0;
let nextPrefetchPromise = null;
let nextPrefetchKey = "";
let prefetchGeneration = 0;
let nextPrefetchRetryAt = 0;
let localResumePending = false;
let localResumeTarget = 0;

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

function standardPictureInPictureSupported() {
  return (
    document.pictureInPictureEnabled === true &&
    typeof video.requestPictureInPicture === "function"
  );
}

function webkitPictureInPictureSupported() {
  return (
    typeof video.webkitSupportsPresentationMode === "function" &&
    typeof video.webkitSetPresentationMode === "function" &&
    video.webkitSupportsPresentationMode("picture-in-picture")
  );
}

function pictureInPictureActive() {
  return (
    document.pictureInPictureElement === video ||
    video.webkitPresentationMode === "picture-in-picture"
  );
}

function remotePlaybackActive() {
  return (
    googleCastConnected() ||
    video.remote?.state === "connected" ||
    video.webkitCurrentPlaybackTargetIsWireless === true
  );
}

function updatePictureInPictureUi() {
  const supported =
    standardPictureInPictureSupported() ||
    webkitPictureInPictureSupported();
  const active = pictureInPictureActive();
  const unavailable =
    !active &&
    (video.readyState === HTMLMediaElement.HAVE_NOTHING ||
      remotePlaybackActive());
  pictureInPictureButton.hidden = !supported;
  pictureInPictureButton.disabled = !supported || unavailable;
  pictureInPictureButton.classList.toggle("is-active", active);
  pictureInPictureButton.setAttribute("aria-pressed", String(active));
  pictureInPictureButton.setAttribute(
    "aria-label",
    active ? "Exit picture-in-picture" : "Enter picture-in-picture",
  );
  pictureInPictureButton.title = active
    ? "Return video to AniStream"
    : remotePlaybackActive()
      ? "Picture-in-picture is unavailable while casting"
      : "Watch in a floating window";
}

async function togglePictureInPicture() {
  if (pictureInPictureActive()) {
    if (
      document.pictureInPictureElement === video &&
      typeof document.exitPictureInPicture === "function"
    ) {
      await document.exitPictureInPicture();
    } else if (typeof video.webkitSetPresentationMode === "function") {
      video.webkitSetPresentationMode("inline");
    }
    return;
  }
  if (remotePlaybackActive()) {
    setCastStatus("Stop casting before opening picture-in-picture.");
    return;
  }
  if (standardPictureInPictureSupported()) {
    await video.requestPictureInPicture();
    return;
  }
  if (webkitPictureInPictureSupported()) {
    video.webkitSetPresentationMode("picture-in-picture");
    return;
  }
  throw new Error("Picture-in-picture is not supported in this browser.");
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

function progressObservation(
  positionOverride = null,
  durationOverride = null,
  targetPlaybackId = playbackId,
  preferCached = false,
) {
  if (
    preferCached &&
    lastProgressObservation?.playback_id === targetPlaybackId
  ) {
    return { ...lastProgressObservation };
  }
  const positionValue =
    positionOverride === null ? video.currentTime : Number(positionOverride);
  const durationValue =
    durationOverride === null ? video.duration : Number(durationOverride);
  if (
    !targetPlaybackId ||
    !Number.isFinite(positionValue) ||
    (targetPlaybackId === playbackId &&
      positionOverride === null &&
      localResumePending)
  ) {
    return null;
  }
  const previous =
    lastProgressObservation?.playback_id === targetPlaybackId
      ? lastProgressObservation
      : null;
  // Telegram's iOS WebView can reset currentTime to exactly zero while the
  // player is being detached. Preserve the last real observation in that
  // narrow case instead of converting a close event into a restart.
  if (
    positionValue <= 0.25 &&
    Number(previous?.position) >= 5 &&
    !video.ended
  ) {
    return { ...previous };
  }
  const observation = {
    playback_id: targetPlaybackId,
    position: Math.max(0, positionValue || 0),
    duration: Number.isFinite(durationValue) ? Math.max(0, durationValue) : 0,
    observed_at_ms: Date.now(),
  };
  if (targetPlaybackId === playbackId) {
    lastProgressObservation = observation;
  }
  return { ...observation };
}

function progressSnapshot(
  isComplete = false,
  positionOverride = null,
  durationOverride = null,
  targetPlaybackId = playbackId,
  preferCached = false,
) {
  const observation = progressObservation(
    positionOverride,
    durationOverride,
    targetPlaybackId,
    preferCached,
  );
  if (!observation) return null;
  progressEventSequence += 1;
  return {
    ...observation,
    event_sequence: progressEventSequence,
    completed: isComplete,
  };
}

async function saveProgress(
  force = false,
  isComplete = false,
  positionOverride = null,
  durationOverride = null,
  targetPlaybackId = playbackId,
  preferCached = false,
) {
  const snapshot = progressSnapshot(
    isComplete,
    positionOverride,
    durationOverride,
    targetPlaybackId,
    preferCached,
  );
  if (!snapshot) return false;
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
  try {
    await api("/api/progress", {
      method: "POST",
      body: JSON.stringify(snapshot),
      keepalive: force,
    });
    return true;
  } catch {
    // A transient progress failure must never interrupt playback.
    return false;
  }
}

function sendProgressBeacon(
  positionOverride = null,
  durationOverride = null,
  targetPlaybackId = playbackId,
) {
  if (!csrfToken || typeof navigator.sendBeacon !== "function") return false;
  const snapshot = progressSnapshot(
    false,
    positionOverride,
    durationOverride,
    targetPlaybackId,
    positionOverride === null,
  );
  if (!snapshot) return false;
  try {
    return navigator.sendBeacon(
      "/api/progress/beacon",
      new Blob(
        [JSON.stringify({ ...snapshot, csrf_token: csrfToken })],
        { type: "application/json" },
      ),
    );
  } catch {
    return false;
  }
}

function episodePrefetchKey(info) {
  if (!info?.has_next) return "";
  return [
    info.playback_id,
    Number(info.episode) + 1,
    Number(info.source_index) || 0,
  ].join(":");
}

function invalidateNextPrefetch() {
  prefetchGeneration += 1;
  prefetchedNext = null;
  prefetchedNextExpiresAt = 0;
  nextPrefetchPromise = null;
  nextPrefetchKey = "";
  nextPrefetchRetryAt = 0;
}

function connectionAllowsPrefetch() {
  const connection =
    navigator.connection ||
    navigator.mozConnection ||
    navigator.webkitConnection;
  if (!connection) return true;
  return (
    connection.saveData !== true &&
    !["slow-2g", "2g"].includes(connection.effectiveType)
  );
}

function prefetchedNextIsFresh() {
  return (
    prefetchedNext !== null &&
    prefetchedNextExpiresAt > Date.now() + PREFETCH_EXPIRY_MARGIN_MS
  );
}

async function startNextPrefetch(info = currentInfo) {
  if (
    !info?.has_next ||
    changingEpisode ||
    changingSource ||
    !connectionAllowsPrefetch()
  ) {
    return null;
  }
  const key = episodePrefetchKey(info);
  if (!key) return null;
  if (nextPrefetchKey === key) {
    if (prefetchedNextIsFresh()) return prefetchedNext;
    if (nextPrefetchPromise) return nextPrefetchPromise;
    if (Date.now() < nextPrefetchRetryAt) return null;
  }

  invalidateNextPrefetch();
  const generation = prefetchGeneration;
  nextPrefetchKey = key;
  const requestedAt = Date.now();
  const request = api("/api/playback/prefetch", {
    method: "POST",
    body: JSON.stringify({
      episode: Number(info.episode) + 1,
      source_index: Number(info.source_index) || 0,
    }),
  })
    .then((prepared) => {
      if (generation === prefetchGeneration && nextPrefetchKey === key) {
        const ttlSeconds = Math.max(
          0,
          Number(prepared.prepared_ttl_seconds) || 0,
        );
        prefetchedNext = prepared;
        // Measure from request start so network latency can only shorten,
        // never accidentally extend, the server-side expiration.
        prefetchedNextExpiresAt = requestedAt + ttlSeconds * 1000;
      }
      return prepared;
    })
    .catch(() => {
      if (generation === prefetchGeneration && nextPrefetchKey === key) {
        nextPrefetchRetryAt = Date.now() + PREFETCH_RETRY_DELAY_MS;
      }
      return null;
    })
    .finally(() => {
      if (generation === prefetchGeneration && nextPrefetchKey === key) {
        nextPrefetchPromise = null;
      }
    });
  nextPrefetchPromise = request;
  return request;
}

function maybePrefetchNext(
  info = currentInfo,
  position = video.currentTime,
  duration = video.duration,
) {
  const currentPosition = Number(position);
  const currentDuration = Number(duration);
  if (
    !info?.has_next ||
    !Number.isFinite(currentPosition) ||
    !Number.isFinite(currentDuration) ||
    currentDuration < 60 ||
    currentPosition < 30 ||
    currentDuration - currentPosition > NEXT_PREFETCH_WINDOW_SECONDS
  ) {
    return;
  }
  void startNextPrefetch(info);
}

async function preparedNextEpisode(targetEpisode) {
  if (!currentInfo || Number(targetEpisode) !== Number(currentInfo.episode) + 1) {
    return null;
  }
  const key = episodePrefetchKey(currentInfo);
  if (!key || nextPrefetchKey !== key) return null;
  if (prefetchedNextIsFresh()) return prefetchedNext;
  if (prefetchedNext) {
    invalidateNextPrefetch();
    return null;
  }
  if (nextPrefetchPromise) {
    await nextPrefetchPromise;
  }
  return nextPrefetchKey === key && prefetchedNextIsFresh()
    ? prefetchedNext
    : null;
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

function option(value, label) {
  const item = document.createElement("option");
  item.value = String(value);
  item.textContent = label;
  return item;
}

function trackLabel(track, fallback) {
  return (
    String(track?.name || track?.label || track?.lang || track?.language || "").trim() ||
    fallback
  );
}

function refreshStreamControls() {
  streamControls.hidden = !streamControlsExpanded;
  playbackOptionsButton.setAttribute(
    "aria-expanded",
    String(streamControlsExpanded),
  );
  playbackOptionsButton.classList.toggle("is-active", streamControlsExpanded);
}

function resetStreamControls() {
  qualityPicker.replaceChildren(option("", "Auto (source)"));
  audioPicker.replaceChildren(option("", "Source audio"));
  subtitlePicker.replaceChildren(option("-1", "Unavailable"));
  qualityPicker.disabled = true;
  audioPicker.disabled = true;
  subtitlePicker.disabled = true;
  refreshStreamControls();
}

function updateQualityOptions(levels = []) {
  const usable = levels
    .map((level, index) => ({ level, index }))
    .filter(({ level }) => Number(level?.height) > 0 || Number(level?.bitrate) > 0);
  if (usable.length <= 1 || !hls) {
    const only = usable[0]?.level;
    const height = Number(only?.height) || 0;
    const bitrate = Math.round((Number(only?.bitrate) || 0) / 1000);
    const label = height
      ? `${height}p (source)`
      : bitrate
        ? `${bitrate} kbps (source)`
        : "Auto (source)";
    qualityPicker.replaceChildren(option("", label));
    qualityPicker.disabled = true;
    refreshStreamControls();
    return;
  }
  const options = document.createDocumentFragment();
  options.append(option("-1", "Auto"));
  for (const { level, index } of usable) {
    const height = Number(level.height) || 0;
    const bitrate = Math.round((Number(level.bitrate) || 0) / 1000);
    const label = height ? `${height}p` : `${bitrate} kbps`;
    options.append(option(index, label));
  }
  qualityPicker.replaceChildren(options);
  const preferred = usable.find(
    ({ level }) => Number(level.height) === preferredQualityHeight,
  );
  qualityPicker.value = preferred ? String(preferred.index) : "-1";
  hls.currentLevel = preferred ? preferred.index : -1;
  qualityPicker.disabled = false;
  refreshStreamControls();
}

function updateAudioOptions(tracks = [], native = false) {
  if (tracks.length <= 1) {
    const label = tracks.length
      ? trackLabel(tracks[0], "Source audio")
      : "Source audio";
    audioPicker.replaceChildren(option("", label));
    audioPicker.disabled = true;
    refreshStreamControls();
    return;
  }
  const options = document.createDocumentFragment();
  let selectedIndex = 0;
  tracks.forEach((track, index) => {
    const label = trackLabel(track, `Audio ${index + 1}`);
    options.append(option(index, label));
    if (preferredAudio && label === preferredAudio) selectedIndex = index;
  });
  audioPicker.replaceChildren(options);
  audioPicker.value = String(selectedIndex);
  if (native) {
    tracks.forEach((track, index) => {
      track.enabled = index === selectedIndex;
    });
  } else if (hls) {
    hls.audioTrack = selectedIndex;
  }
  preferredAudio = trackLabel(tracks[selectedIndex], `Audio ${selectedIndex + 1}`);
  audioPicker.disabled = false;
  refreshStreamControls();
}

function updateSubtitleOptions(tracks = [], native = false) {
  if (!tracks.length) {
    subtitlePicker.replaceChildren(option("-1", "Unavailable"));
    subtitlePicker.disabled = true;
    refreshStreamControls();
    return;
  }
  const options = document.createDocumentFragment();
  options.append(option("-1", "Off"));
  let selectedIndex = -1;
  tracks.forEach((track, index) => {
    const label = trackLabel(track, `Subtitles ${index + 1}`);
    options.append(option(index, label));
    if (preferredSubtitle && label === preferredSubtitle) selectedIndex = index;
  });
  subtitlePicker.replaceChildren(options);
  subtitlePicker.value = String(selectedIndex);
  if (native) {
    tracks.forEach((track, index) => {
      track.mode = index === selectedIndex ? "showing" : "disabled";
    });
  } else if (hls) {
    hls.subtitleTrack = selectedIndex;
  }
  subtitlePicker.disabled = false;
  refreshStreamControls();
}

function updateNativeTrackOptions() {
  const audioTracks = video.audioTracks ? Array.from(video.audioTracks) : [];
  const textTracks = video.textTracks ? Array.from(video.textTracks) : [];
  updateAudioOptions(audioTracks, true);
  updateSubtitleOptions(textTracks, true);
}

function teardownPlayer() {
  playerEvents?.abort();
  playerEvents = null;
  hls?.destroy();
  hls = null;
  resetStreamControls();
  video.pause();
  localResumePending = false;
  localResumeTarget = 0;
  video.removeAttribute("src");
  video.load();
  updatePictureInPictureUi();
}

function updateEpisodePicker(info) {
  const totalEpisodes = Math.max(1, Number(info.total_episodes) || 1);
  if (totalEpisodes <= 1) {
    episodePickerShell.hidden = true;
    episodePicker.disabled = true;
    episodePicker.replaceChildren();
    updatePlaybackToolbar();
    return;
  }

  if (episodePicker.options.length !== totalEpisodes) {
    const options = document.createDocumentFragment();
    for (let number = 1; number <= totalEpisodes; number += 1) {
      const option = document.createElement("option");
      option.value = String(number);
      option.textContent = `Episode ${number}`;
      options.append(option);
    }
    episodePicker.replaceChildren(options);
  }

  episodePicker.value = String(info.episode);
  episodePicker.disabled = changingEpisode;
  episodePickerShell.hidden = false;
  updatePlaybackToolbar();
}

function updatePlaybackToolbar() {
  const visiblePickers =
    Number(!episodePickerShell.hidden) + Number(!sourcePickerShell.hidden);
  playbackToolbar.hidden = visiblePickers === 0;
  playbackToolbar.classList.toggle("is-single", visiblePickers === 1);
}

function updateSourceUi(info) {
  const sourceCount = Math.max(1, Number(info.source_count) || 1);
  const sourceIndex = Math.max(
    0,
    Math.min(sourceCount - 1, Number(info.source_index) || 0),
  );
  if (sourceCount <= 1) {
    sourcePickerShell.hidden = true;
    sourcePicker.disabled = true;
    sourcePicker.replaceChildren();
    updatePlaybackToolbar();
    return;
  }
  if (sourcePicker.options.length !== sourceCount) {
    const options = document.createDocumentFragment();
    for (let index = 0; index < sourceCount; index += 1) {
      options.append(option(index, `Source ${index + 1}`));
    }
    sourcePicker.replaceChildren(options);
  }
  sourcePicker.value = String(sourceIndex);
  sourcePicker.disabled = changingSource;
  sourcePickerShell.hidden = false;
  updatePlaybackToolbar();
}

function updateAutoplayUi(info) {
  const isSeries = Number(info.total_episodes) > 1;
  autoplayStatus.hidden = !isSeries;
  autoplayStatus.textContent =
    info.autoplay_enabled === false
      ? "Autoplay next is off"
      : "Autoplay next is on";
}

function updateEpisodeUi(info) {
  currentInfo = info;
  playbackId = info.playback_id;
  completed = false;
  lastSavedAt = 0;
  lastSavedPlaybackId = "";
  const initialPosition = Math.max(0, Number(info.start_position) || 0);
  lastProgressObservation = {
    playback_id: info.playback_id,
    position: initialPosition,
    duration: 0,
    observed_at_ms: Date.now(),
  };
  title.textContent = info.title;
  subtitle.textContent = [info.season, info.language].filter(Boolean).join(" · ");
  episode.textContent = `${info.episode} / ${info.total_episodes}`;
  source.textContent =
    Number(info.source_count) > 1
      ? `Source ${Number(info.source_index) + 1}`
      : info.source;
  progress.textContent = formatTime(initialPosition);
  previous.disabled = !info.has_previous;
  next.disabled = !info.has_next;
  updateEpisodePicker(info);
  updateSourceUi(info);
  updateAutoplayUi(info);
}

function attachPlayer(info, { autoplay = false } = {}) {
  invalidateNextPrefetch();
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
  const requestedPosition = Number(info.start_position);
  localResumeTarget =
    Number.isFinite(requestedPosition) && requestedPosition > 0
      ? requestedPosition
      : 0;
  localResumePending = localResumeTarget > 0;

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
    hls.on(Hls.Events.MANIFEST_PARSED, (_, data) => {
      updateQualityOptions(data.levels || hls.levels || []);
      updateAudioOptions(hls.audioTracks || []);
      updateSubtitleOptions(hls.subtitleTracks || []);
    });
    hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, (_, data) => {
      updateAudioOptions(data.audioTracks || hls.audioTracks || []);
    });
    hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, (_, data) => {
      updateSubtitleOptions(data.subtitleTracks || hls.subtitleTracks || []);
    });
    hls.on(Hls.Events.LEVEL_SWITCHED, (_, data) => {
      if (hls.autoLevelEnabled) {
        qualityPicker.value = "-1";
      } else if (Number.isInteger(data.level)) {
        qualityPicker.value = String(data.level);
      }
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal && !changingEpisode && !changingSource) {
        showError("The stream stopped. Try another episode or reopen it from the bot.");
      }
    });
  } else {
    video.src = streamUrl;
  }

  const resumeRangeEnd = () => {
    const duration = Number(video.duration);
    if (duration === Number.POSITIVE_INFINITY) return duration;
    let end = Number.isFinite(duration) && duration > 0 ? duration : 0;
    for (let index = 0; index < video.seekable.length; index += 1) {
      const candidate = Number(video.seekable.end(index));
      if (Number.isFinite(candidate)) end = Math.max(end, candidate);
    }
    return end;
  };

  const applyResumePosition = ({ restartIfOutOfRange = false } = {}) => {
    if (!localResumePending || playbackId !== info.playback_id) return true;
    const end = resumeRangeEnd();
    if (end !== Number.POSITIVE_INFINITY && end <= localResumeTarget + 5) {
      if (!restartIfOutOfRange || !Number.isFinite(end) || end <= 0) {
        return false;
      }
      localResumePending = false;
      localResumeTarget = 0;
      video.currentTime = 0;
      progress.textContent = formatTime(0);
      return true;
    }
    try {
      video.currentTime = localResumeTarget;
    } catch {
      return false;
    }
    progress.textContent = formatTime(localResumeTarget);
    localResumePending = false;
    return true;
  };

  let playerReadyHandled = false;
  const finishPlayerReady = () => {
    if (
      playerReadyHandled ||
      localResumePending ||
      video.readyState < HTMLMediaElement.HAVE_METADATA
    ) {
      return;
    }
    playerReadyHandled = true;
    loading.hidden = true;
    updatePictureInPictureUi();
    if (autoplay && !googleCastConnected()) {
      void video.play().catch(() => {
        setCastStatus("Tap play to start the next episode.");
      });
    }
  };

  video.addEventListener(
    "loadedmetadata",
    () => {
      applyResumePosition();
      if (!useHls || !Hls?.isSupported()) {
        updateNativeTrackOptions();
      }
      finishPlayerReady();
    },
    options,
  );
  video.addEventListener(
    "durationchange",
    () => {
      applyResumePosition();
      finishPlayerReady();
    },
    options,
  );
  video.addEventListener(
    "progress",
    () => {
      applyResumePosition();
      finishPlayerReady();
    },
    options,
  );
  video.addEventListener(
    "canplay",
    () => {
      applyResumePosition({ restartIfOutOfRange: true });
      finishPlayerReady();
    },
    options,
  );
  video.addEventListener(
    "timeupdate",
    () => {
      if (localResumePending) {
        applyResumePosition();
        if (localResumePending) return;
      }
      progress.textContent = formatTime(video.currentTime);
      void saveProgress(false, false);
      maybePrefetchNext(info);
    },
    options,
  );
  video.addEventListener(
    "pause",
    () => {
      if (
        !video.ended &&
        !changingEpisode &&
        !changingSource &&
        !googleCastConnected()
      ) {
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
      if (info.has_next && info.autoplay_enabled !== false) {
        await changeEpisode(info.episode + 1, { autoplay: true });
      } else if (info.has_next) {
        setCastStatus("Autoplay is off. Choose the next episode when ready.");
      } else {
        setCastStatus("Season completed.");
      }
    },
    options,
  );
  video.addEventListener(
    "error",
    () => {
      if (!changingEpisode && !changingSource) {
        showError("This source cannot be played right now. Try another episode.");
      }
    },
    options,
  );
  updatePictureInPictureUi();
}

async function loadCurrentOnGoogleCast(
  info = currentInfo,
  positionOverride = null,
) {
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
    const requestedPosition =
      positionOverride === null ? video.currentTime : Number(positionOverride);
    request.currentTime = Math.max(
      0,
      Number.isFinite(requestedPosition)
        ? requestedPosition
        : Number(info.start_position) || 0,
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
    changingSource ||
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
  if (currentInfo.has_next && currentInfo.autoplay_enabled !== false) {
    await changeEpisode(currentInfo.episode + 1, { autoplay: true });
  } else if (currentInfo.has_next) {
    setCastStatus("Autoplay is off. Choose the next episode when ready.");
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
        maybePrefetchNext(
          currentInfo,
          remotePlayer.currentTime,
          remotePlayer.duration,
        );
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
      !changingSource &&
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
        updatePictureInPictureUi();
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
        updatePictureInPictureUi();
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
    video.remote.onconnect = () => {
      setCastStatus("Playing on the TV.");
      updatePictureInPictureUi();
    };
    video.remote.ondisconnect = () => {
      setCastStatus("Remote playback disconnected.");
      updatePictureInPictureUi();
    };
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
  if (changingEpisode || changingSource || !currentInfo) return;
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
  episodePicker.disabled = true;
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
    let info = null;
    const prepared = await preparedNextEpisode(target);
    if (prepared) {
      try {
        info = await api("/api/playback/activate", {
          method: "POST",
          body: JSON.stringify({ playback_id: prepared.playback_id }),
        });
      } catch {
        info = null;
      }
    }
    if (!info) {
      info = await api("/api/playback/episode", {
        method: "POST",
        body: JSON.stringify({
          episode: target,
          source_index: Number(currentInfo.source_index) || 0,
        }),
      });
    }
    attachPlayer(info, { autoplay: autoplay && !googleCastConnected() });
    if (googleCastConnected()) {
      await loadCurrentOnGoogleCast(info, info.start_position);
    }
  } catch (reason) {
    showError(
      reason instanceof Error ? reason.message : "The episode could not be loaded.",
    );
    previous.disabled = !currentInfo.has_previous;
    next.disabled = !currentInfo.has_next;
  } finally {
    changingEpisode = false;
    updateEpisodePicker(currentInfo);
  }
}

async function changeSource(targetSourceIndex) {
  if (
    changingSource ||
    changingEpisode ||
    !currentInfo ||
    Number(currentInfo.source_count) <= 1
  ) {
    return;
  }
  const sourceCount = Number(currentInfo.source_count);
  const targetSource = Number(targetSourceIndex);
  if (
    !Number.isInteger(targetSource) ||
    targetSource < 0 ||
    targetSource >= sourceCount ||
    targetSource === Number(currentInfo.source_index)
  ) {
    sourcePicker.value = String(currentInfo.source_index);
    return;
  }
  const wasPlaying = googleCastConnected()
    ? Boolean(remotePlayer?.isMediaLoaded && !remotePlayer.isPaused)
    : !video.paused;
  changingSource = true;
  sourcePicker.disabled = true;
  clearError();
  loading.hidden = false;
  status.textContent = `Preparing source ${targetSource + 1}...`;
  try {
    if (googleCastConnected() && remotePlayer?.isMediaLoaded) {
      await saveProgress(
        true,
        false,
        remotePlayer.currentTime,
        remotePlayer.duration,
      );
      if (!remotePlayer.isPaused) {
        remoteController?.playOrPause();
      }
    } else {
      await saveProgress(true, false);
      video.pause();
    }
    const info = await api("/api/playback/source", {
      method: "POST",
      body: JSON.stringify({
        playback_id: currentInfo.playback_id,
        source_index: targetSource,
      }),
    });
    attachPlayer(info, { autoplay: wasPlaying && !googleCastConnected() });
    if (googleCastConnected()) {
      await loadCurrentOnGoogleCast(info, info.start_position);
    }
  } catch (reason) {
    loading.hidden = true;
    if (wasPlaying) {
      if (googleCastConnected() && remotePlayer?.isPaused) {
        remoteController?.playOrPause();
      } else if (!googleCastConnected()) {
        void video.play().catch(() => {});
      }
    }
    showError(
      reason instanceof Error
        ? `${reason.message} Choose another source or try again.`
        : "This source is unavailable. Choose another source or try again.",
    );
  } finally {
    changingSource = false;
    updateSourceUi(currentInfo);
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
    const info = await api("/api/playback", { method: "POST" });
    attachPlayer(info);
    setupCast();
  } catch (reason) {
    showError(
      reason instanceof Error ? reason.message : "Playback could not be started.",
    );
  }
}

async function refreshAutoplayPreference() {
  if (!currentInfo) return;
  try {
    const session = await api("/api/session");
    if (typeof session.autoplay_enabled === "boolean") {
      currentInfo.autoplay_enabled = session.autoplay_enabled;
      updateAutoplayUi(currentInfo);
    }
  } catch {
    // Keep the last known preference if Telegram briefly loses connectivity.
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

episodePicker.addEventListener("change", () => {
  const target = Number(episodePicker.value);
  if (currentInfo && target !== currentInfo.episode) {
    void changeEpisode(target, { autoplay: true });
  }
});

sourcePicker.addEventListener("change", () => {
  void changeSource(Number(sourcePicker.value));
});

playbackOptionsButton.addEventListener("click", () => {
  streamControlsExpanded = !streamControlsExpanded;
  refreshStreamControls();
});

qualityPicker.addEventListener("change", () => {
  if (!hls) return;
  const level = Number(qualityPicker.value);
  if (!Number.isInteger(level) || level < -1 || level >= hls.levels.length) return;
  hls.currentLevel = level;
  preferredQualityHeight =
    level >= 0 ? Number(hls.levels[level]?.height) || 0 : 0;
});

audioPicker.addEventListener("change", () => {
  const index = Number(audioPicker.value);
  if (!Number.isInteger(index) || index < 0) return;
  if (hls && index < hls.audioTracks.length) {
    hls.audioTrack = index;
    preferredAudio = trackLabel(hls.audioTracks[index], `Audio ${index + 1}`);
    return;
  }
  const tracks = video.audioTracks ? Array.from(video.audioTracks) : [];
  if (index >= tracks.length) return;
  tracks.forEach((track, trackIndex) => {
    track.enabled = trackIndex === index;
  });
  preferredAudio = trackLabel(tracks[index], `Audio ${index + 1}`);
});

subtitlePicker.addEventListener("change", () => {
  const index = Number(subtitlePicker.value);
  if (!Number.isInteger(index) || index < -1) return;
  if (hls) {
    if (index >= hls.subtitleTracks.length) return;
    hls.subtitleTrack = index;
    preferredSubtitle =
      index >= 0
        ? trackLabel(hls.subtitleTracks[index], `Subtitles ${index + 1}`)
        : "";
    return;
  }
  const tracks = video.textTracks ? Array.from(video.textTracks) : [];
  if (index >= tracks.length) return;
  tracks.forEach((track, trackIndex) => {
    track.mode = trackIndex === index ? "showing" : "disabled";
  });
  preferredSubtitle =
    index >= 0 ? trackLabel(tracks[index], `Subtitles ${index + 1}`) : "";
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

pictureInPictureButton.addEventListener("click", async () => {
  try {
    clearError();
    await togglePictureInPicture();
  } catch (reason) {
    showError(
      reason instanceof Error
        ? reason.message
        : "Picture-in-picture could not be opened.",
    );
  } finally {
    updatePictureInPictureUi();
  }
});

video.addEventListener("enterpictureinpicture", updatePictureInPictureUi);
video.addEventListener("leavepictureinpicture", updatePictureInPictureUi);
video.addEventListener(
  "webkitpresentationmodechanged",
  updatePictureInPictureUi,
);
video.addEventListener(
  "webkitcurrentplaybacktargetiswirelesschanged",
  updatePictureInPictureUi,
);

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
  if (document.visibilityState === "visible") {
    void refreshAutoplayPreference();
    return;
  }
  if (completed) return;
  if (googleCastConnected() && remotePlayer?.isMediaLoaded) {
    if (
      !sendProgressBeacon(
        remotePlayer.currentTime,
        remotePlayer.duration,
      )
    ) {
      void saveProgress(
        true,
        false,
        remotePlayer.currentTime,
        remotePlayer.duration,
      );
    }
  } else if (!sendProgressBeacon()) {
    void saveProgress(true, false, null, null, playbackId, true);
  }
});

window.addEventListener("pagehide", () => {
  if (!completed) {
    if (googleCastConnected() && remotePlayer?.isMediaLoaded) {
      if (
        !sendProgressBeacon(
          remotePlayer.currentTime,
          remotePlayer.duration,
        )
      ) {
        void saveProgress(
          true,
          false,
          remotePlayer.currentTime,
          remotePlayer.duration,
        );
      }
    } else if (!sendProgressBeacon()) {
      void saveProgress(true, false, null, null, playbackId, true);
    }
  }
  if (castProgressTimer) window.clearInterval(castProgressTimer);
  playerEvents?.abort();
  hls?.destroy();
});

resetStreamControls();
updatePictureInPictureUi();
void initialize();
