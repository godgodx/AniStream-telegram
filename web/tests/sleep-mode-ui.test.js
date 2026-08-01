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
