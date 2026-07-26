import { cpSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";

const root = import.meta.dirname;
const dist = join(root, "dist");
const assets = join(dist, "assets");

rmSync(dist, { recursive: true, force: true });
mkdirSync(assets, { recursive: true });

cpSync(join(root, "index.html"), join(dist, "index.html"));
cpSync(join(root, "src", "main.js"), join(assets, "main.js"));
cpSync(join(root, "src", "styles.css"), join(assets, "styles.css"));
cpSync(
  join(root, "node_modules", "hls.js", "dist", "hls.min.js"),
  join(assets, "hls.min.js"),
);

console.log("Mini App assets written to web/dist");
