import {
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";

const root = import.meta.dirname;
const dist = join(root, "dist");
const assets = join(dist, "assets");

rmSync(dist, { recursive: true, force: true });
mkdirSync(assets, { recursive: true });

function fingerprint(source, stem, extension) {
  const body = readFileSync(source);
  const digest = createHash("sha256").update(body).digest("hex").slice(0, 12);
  const name = `${stem}.${digest}${extension}`;
  writeFileSync(join(assets, name), body);
  return name;
}

const mainName = fingerprint(
  join(root, "src", "main.js"),
  "main",
  ".js",
);
const stylesName = fingerprint(
  join(root, "src", "styles.css"),
  "styles",
  ".css",
);
const hlsName = fingerprint(
  join(root, "node_modules", "hls.js", "dist", "hls.min.js"),
  "hls",
  ".min.js",
);

const html = readFileSync(join(root, "index.html"), "utf8")
  .replace("/app/assets/main.js", `/app/assets/${mainName}`)
  .replace("/app/assets/styles.css", `/app/assets/${stylesName}`)
  .replace("/app/assets/hls.min.js", `/app/assets/${hlsName}`);
writeFileSync(join(dist, "index.html"), html);

console.log("Mini App assets written to web/dist");
