export const CLOSE_COMPLETION_WINDOW_SECONDS = 10 * 60;
export const DEFAULT_SLEEP_MODE_EPISODES = 3;

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function completionOnClose(info, positionValue, durationValue) {
  const episode = finiteNumber(info?.episode);
  const totalEpisodes = finiteNumber(info?.total_episodes);
  const position = finiteNumber(positionValue);
  const duration = finiteNumber(durationValue);
  if (
    episode === null ||
    totalEpisodes === null ||
    position === null ||
    duration === null ||
    episode !== totalEpisodes ||
    position <= 0 ||
    duration <= 0 ||
    position > duration
  ) {
    return false;
  }
  return duration - position <= CLOSE_COMPLETION_WINDOW_SECONDS;
}

export function sleepModeEpisodeLimit(value) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1 || number > 12) {
    return DEFAULT_SLEEP_MODE_EPISODES;
  }
  return number;
}

export function sleepModeConfigurationChanged(previousInfo, nextInfo) {
  if (!previousInfo || !nextInfo) return false;
  return (
    (previousInfo.sleep_mode_enabled === true) !==
      (nextInfo.sleep_mode_enabled === true) ||
    (previousInfo.autoplay_enabled !== false) !==
      (nextInfo.autoplay_enabled !== false) ||
    sleepModeEpisodeLimit(previousInfo.sleep_mode_episodes) !==
      sleepModeEpisodeLimit(nextInfo.sleep_mode_episodes)
  );
}

export function sleepModeStatusText(info, completedEpisodesValue) {
  if (info?.sleep_mode_enabled !== true) return "Sleep mode: off";

  const completedEpisodes = Math.max(
    0,
    Number.isInteger(Number(completedEpisodesValue))
      ? Number(completedEpisodesValue)
      : 0,
  );
  const remainingEpisodes = Math.max(
    1,
    sleepModeEpisodeLimit(info.sleep_mode_episodes) - completedEpisodes,
  );
  return remainingEpisodes === 1
    ? "Sleep mode: last"
    : `Sleep mode: ${remainingEpisodes}`;
}

export function progressPersistenceAccepted(response) {
  return response?.accepted === true;
}

export function playbackAttachment(
  info,
  { hlsSupported = false, nativeRemoteActive = false } = {},
) {
  const streamUrl = nativeRemoteActive
    ? info?.native_cast_url
    : info?.stream_url;
  if (typeof streamUrl !== "string" || !streamUrl) {
    throw new Error("The playback stream is not ready. Try again in a moment.");
  }
  return {
    streamUrl,
    useAdaptiveHls:
      info?.kind === "hls" && hlsSupported === true && !nativeRemoteActive,
  };
}

export function localPlaybackRecoveryAllowed(
  { nativeCastTransition = false, nativeRemoteActive = false } = {},
) {
  return nativeCastTransition !== true && nativeRemoteActive !== true;
}

export function beginNativeCast({
  video,
  castUrl,
  releaseAdaptivePlayer = () => {},
  beforeLoad = () => {},
  openPicker,
  positionOverride = null,
}) {
  if (
    !video ||
    typeof castUrl !== "string" ||
    !castUrl ||
    typeof openPicker !== "function"
  ) {
    throw new Error("The TV stream is not ready. Try again in a moment.");
  }
  const currentTime =
    positionOverride === null
      ? Number(video.currentTime)
      : Number(positionOverride);
  const resumePosition = Number.isFinite(currentTime)
    ? Math.max(0, currentTime)
    : 0;
  beforeLoad(resumePosition);
  releaseAdaptivePlayer();
  video.pause();
  video.src = castUrl;
  video.load();
  const pickerResult = openPicker();
  return { resumePosition, pickerResult };
}

export async function runNativeCastAttempt({ begin, rollback }) {
  try {
    const result = begin();
    await result?.pickerResult;
    return result;
  } catch (reason) {
    try {
      await rollback(reason);
    } catch {
      // Preserve the picker error; restoration is best-effort.
    }
    throw reason;
  }
}

function playbackCompletionIsCurrent(completion, current) {
  return (
    completion?.playbackId === current?.playbackId &&
    completion?.playbackGeneration === current?.playbackGeneration &&
    completion?.workflowEpoch === current?.workflowEpoch
  );
}

export async function runCurrentPlaybackCompletion({
  completion,
  persist,
  current,
  apply,
}) {
  const persisted = await persist();
  if (persisted !== true) return false;
  const latest = current();
  if (!playbackCompletionIsCurrent(completion, latest)) return false;
  await apply(latest.info || completion.info);
  return true;
}

export function nextSleepModeState(info, completedEpisodesValue) {
  const completedEpisodes = Math.max(
    0,
    Number.isInteger(Number(completedEpisodesValue))
      ? Number(completedEpisodesValue)
      : 0,
  );
  const applies =
    Number(info?.total_episodes) > 1 &&
    info?.has_next === true &&
    info?.autoplay_enabled !== false &&
    info?.sleep_mode_enabled === true;
  if (!applies) {
    return { completedEpisodes, shouldPause: false };
  }
  const nextCount = completedEpisodes + 1;
  return {
    completedEpisodes: nextCount,
    shouldPause: nextCount >= sleepModeEpisodeLimit(info.sleep_mode_episodes),
  };
}
