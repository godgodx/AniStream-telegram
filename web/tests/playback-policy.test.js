import assert from "node:assert/strict";
import test from "node:test";

import {
  completionOnClose,
  nextSleepModeState,
  progressPersistenceAccepted,
  runCurrentPlaybackCompletion,
  sleepModeConfigurationChanged,
  sleepModeStatusText,
} from "../src/playback-policy.js";

const finalEpisode = {
  episode: 12,
  total_episodes: 12,
  has_next: false,
  autoplay_enabled: true,
  sleep_mode_enabled: true,
  sleep_mode_episodes: 3,
};

test("closing a movie or final episode with at most ten minutes left completes it", () => {
  assert.equal(completionOnClose(finalEpisode, 6_600, 7_200), true);
  assert.equal(completionOnClose({ ...finalEpisode, total_episodes: 1, episode: 1 }, 3_000, 3_600), true);
  assert.equal(completionOnClose(finalEpisode, 6_599, 7_200), false);
});

test("near-end close completion never applies to an intermediate episode", () => {
  assert.equal(
    completionOnClose(
      { ...finalEpisode, episode: 11, has_next: true },
      1_000,
      1_440,
    ),
    false,
  );
});

test("near-end close completion requires valid progress", () => {
  assert.equal(completionOnClose(finalEpisode, 0, 500), false);
  assert.equal(completionOnClose(finalEpisode, Number.NaN, 7_200), false);
  assert.equal(completionOnClose(finalEpisode, 7_300, 7_200), false);
});

test("near-end close completion also supports short final content once playback started", () => {
  assert.equal(completionOnClose(finalEpisode, 540, 600), true);
  assert.equal(
    completionOnClose(
      { ...finalEpisode, episode: 1, total_episodes: 1 },
      240,
      300,
    ),
    true,
  );
});

test("sleep mode pauses autoplay after the configured consecutive episode count", () => {
  const episodeWithNext = { ...finalEpisode, episode: 9, has_next: true };
  assert.deepEqual(nextSleepModeState(episodeWithNext, 0), {
    completedEpisodes: 1,
    shouldPause: false,
  });
  assert.deepEqual(nextSleepModeState(episodeWithNext, 1), {
    completedEpisodes: 2,
    shouldPause: false,
  });
  assert.deepEqual(nextSleepModeState(episodeWithNext, 2), {
    completedEpisodes: 3,
    shouldPause: true,
  });
});

test("sleep mode status counts down and marks the final episode before pausing", () => {
  const active = { ...finalEpisode, episode: 9, has_next: true };
  assert.equal(sleepModeStatusText(active, 0), "Sleep mode: 3");
  assert.equal(sleepModeStatusText(active, 1), "Sleep mode: 2");
  assert.equal(sleepModeStatusText(active, 2), "Sleep mode: last");
  assert.equal(
    sleepModeStatusText({ ...active, sleep_mode_enabled: false }, 0),
    "Sleep mode: off",
  );
});

test("sleep mode status safely handles a one-episode limit and invalid counters", () => {
  const active = {
    ...finalEpisode,
    episode: 9,
    has_next: true,
    sleep_mode_episodes: 1,
  };
  assert.equal(sleepModeStatusText(active, 0), "Sleep mode: last");
  assert.equal(sleepModeStatusText(active, -4), "Sleep mode: last");
  assert.equal(sleepModeStatusText(active, "invalid"), "Sleep mode: last");
});

test("sleep mode is inert for movies, final episodes, disabled autoplay, or disabled mode", () => {
  const scenarios = [
    { ...finalEpisode, total_episodes: 1, episode: 1 },
    { ...finalEpisode, has_next: false },
    { ...finalEpisode, has_next: true, autoplay_enabled: false },
    { ...finalEpisode, has_next: true, sleep_mode_enabled: false },
  ];
  for (const info of scenarios) {
    assert.deepEqual(nextSleepModeState(info, 2), {
      completedEpisodes: 2,
      shouldPause: false,
    });
  }
});

test("sleep mode restarts its consecutive counter when its active configuration changes", () => {
  const active = { ...finalEpisode, has_next: true };
  assert.equal(sleepModeConfigurationChanged(active, { ...active }), false);
  assert.equal(
    sleepModeConfigurationChanged(active, {
      ...active,
      sleep_mode_enabled: false,
    }),
    true,
  );
  assert.equal(
    sleepModeConfigurationChanged(active, {
      ...active,
      autoplay_enabled: false,
    }),
    true,
  );
  assert.equal(
    sleepModeConfigurationChanged(active, {
      ...active,
      sleep_mode_episodes: 4,
    }),
    true,
  );
});

test("a delayed completion cannot affect a playback selected while it was saving", async () => {
  let releaseProgress;
  const progressSaved = new Promise((resolve) => {
    releaseProgress = resolve;
  });
  const firstPlayback = {
    playbackId: "playback-1",
    playbackGeneration: 1,
    workflowEpoch: 7,
    info: { episode: 2 },
  };
  let currentPlayback = { ...firstPlayback };
  const applied = [];
  const transition = runCurrentPlaybackCompletion({
    completion: firstPlayback,
    persist: () => progressSaved,
    current: () => currentPlayback,
    apply: (info) => applied.push(info.episode),
  });

  currentPlayback = {
    playbackId: "playback-2",
    playbackGeneration: 2,
    workflowEpoch: 8,
  };
  releaseProgress();

  assert.equal(await transition, false);
  assert.deepEqual(applied, []);
});

test("a current completion applies exactly once after progress is persisted", async () => {
  const completion = {
    playbackId: "playback-1",
    playbackGeneration: 1,
    workflowEpoch: 4,
    info: { episode: 3 },
  };
  const applied = [];

  assert.equal(
    await runCurrentPlaybackCompletion({
      completion,
      persist: async () => true,
      current: () => ({ ...completion }),
      apply: async (info) => applied.push(info.episode),
    }),
    true,
  );
  assert.deepEqual(applied, [3]);
});

test("a completion never advances when persistence fails", async () => {
  const completion = {
    playbackId: "playback-1",
    playbackGeneration: 1,
    workflowEpoch: 4,
    info: { episode: 3 },
  };
  const applied = [];

  assert.equal(
    await runCurrentPlaybackCompletion({
      completion,
      persist: async () => false,
      current: () => ({ ...completion }),
      apply: async (info) => applied.push(info.episode),
    }),
    false,
  );
  assert.deepEqual(applied, []);
});

test("progress persistence requires the server to accept the write", () => {
  assert.equal(progressPersistenceAccepted({ ok: true, accepted: true }), true);
  assert.equal(progressPersistenceAccepted({ ok: true, accepted: false }), false);
  assert.equal(progressPersistenceAccepted({ ok: true }), false);
  assert.equal(progressPersistenceAccepted(null), false);
});

test("a delayed completion uses the latest autoplay preferences", async () => {
  const completion = {
    playbackId: "playback-1",
    playbackGeneration: 1,
    workflowEpoch: 4,
    info: { episode: 3, autoplay_enabled: true },
  };
  const currentPlayback = {
    playbackId: "playback-1",
    playbackGeneration: 1,
    workflowEpoch: 4,
    info: { episode: 3, autoplay_enabled: false },
  };
  let appliedAutoplayPreference = null;

  assert.equal(
    await runCurrentPlaybackCompletion({
      completion,
      persist: async () => true,
      current: () => currentPlayback,
      apply: async (info) => {
        appliedAutoplayPreference = info.autoplay_enabled;
      },
    }),
    true,
  );
  assert.equal(appliedAutoplayPreference, false);
});
