import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const root = new URL("..", import.meta.url);

function source(path) {
  return readFileSync(new URL(path, root), "utf8");
}

test("the player ships an accessible Sleep Mode confirmation overlay", () => {
  const html = source("index.html");
  assert.match(html, /<dialog[\s\S]*id="sleep-prompt"/);
  assert.match(html, /role="dialog"/);
  assert.match(html, /id="sleep-continue"/);
  assert.match(html, /id="sleep-stop"/);
  const script = source("src/main.js");
  assert.match(script, /sleepPrompt\.showModal\(\)/);
  assert.match(script, /sleepPrompt\.addEventListener\("cancel"/);
});

test("close completion and Sleep Mode policy are wired into player lifecycle", () => {
  const script = source("src/main.js");
  assert.match(script, /completionOnClose/);
  assert.match(script, /nextSleepModeState/);
  assert.match(script, /showSleepPrompt/);
  assert.match(script, /event\.persisted/);
  assert.equal(
    (script.match(/runCurrentPlaybackCompletion\(/g) || []).length,
    2,
    "local and Cast completion paths must both revalidate playback identity",
  );
});

test("the episode navigation shows the live Sleep Mode countdown", () => {
  const html = source("index.html");
  const script = source("src/main.js");
  assert.match(html, /id="autoplay-status">Sleep mode: off</);
  assert.match(script, /sleepModeStatusText\([\s\S]*sleepModeCompletedEpisodes/);
});

test("the fullscreen button toggles both Telegram and browser fullscreen", () => {
  const html = source("index.html");
  const script = source("src/main.js");
  assert.match(html, /id="fullscreen"[\s\S]*aria-pressed="false"/);
  assert.match(script, /telegram\.isFullscreen === true/);
  assert.match(script, /telegram\.exitFullscreen\(\)/);
  assert.match(script, /telegram\.requestFullscreen\(\)/);
  assert.match(script, /document\.exitFullscreen\(\)/);
  assert.match(script, /"fullscreenChanged", updateFullscreenUi/);
});

test("a failed completion is retried and confirmed before Next navigates", () => {
  const script = source("src/main.js");
  assert.match(script, /pendingCompletionRetry/);
  assert.match(script, /retryPendingCompletion/);
  assert.match(script, /Completion still could not be saved/);
});
