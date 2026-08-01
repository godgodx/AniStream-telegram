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

export function progressPersistenceAccepted(response) {
  return response?.accepted === true;
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
