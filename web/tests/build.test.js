import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import test from "node:test";

const root = new URL("..", import.meta.url);

test("the production build fingerprints and ships the playback policy module", () => {
  execFileSync(process.execPath, ["build.mjs"], {
    cwd: root,
    stdio: "pipe",
  });
  const html = readFileSync(new URL("dist/index.html", root), "utf8");
  const mainName = html.match(/assets\/(main\.[0-9a-f]{12}\.js)/)?.[1];
  assert.ok(mainName, "fingerprinted main module is referenced");
  const main = readFileSync(new URL(`dist/assets/${mainName}`, root), "utf8");
  const policyName = main.match(/\.\/playback-policy\.([0-9a-f]{12})\.js/)?.[0];
  assert.ok(policyName, "main imports a fingerprinted playback policy module");
  assert.equal(existsSync(new URL(`dist/assets/${policyName.slice(2)}`, root)), true);
});
