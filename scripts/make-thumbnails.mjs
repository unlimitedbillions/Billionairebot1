#!/usr/bin/env node
/**
 * make-thumbnails.mjs — cinematic, high-CTR thumbnails & posters.
 * For each job in input/thumbnails.json: grabs a dramatic frame (~45% mark),
 * grades it, adds bottom gradient + accent bar + huge 2-line headline + channel tag.
 * Outputs per job: thumb_9x16.jpg, thumb_16x9.jpg, poster.jpg (4:5)
 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const ROOT = process.cwd();
const MAP = JSON.parse(fs.readFileSync(path.join(ROOT, "input", "thumbnails.json"), "utf8"));

const FONT_CANDIDATES = [
  process.env.THUMB_FONT,
  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
  "C:\\Windows\\Fonts\\arialbd.ttf",
].filter(Boolean);
const FONT = FONT_CANDIDATES.find((f) => fs.existsSync(f)) || "";
const fontOpt = FONT ? `fontfile='${FONT}'` : "font='Sans'";

const esc = (s) => s.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/:/g, "\\:");

function findVideo(jobId) {
  const dir = path.join(ROOT, "output", jobId);
  if (!fs.existsSync(dir)) return null;
  const mp4 = fs.readdirSync(dir).find((f) => f.endsWith(".mp4"));
  return mp4 ? path.join(dir, mp4) : null;
}
function duration(video) {
  return parseFloat(execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video]).toString());
}
function frame(video, t, out) {
  execFileSync("ffmpeg", ["-y", "-ss", String(t), "-i", video, "-frames:v", "1", out], { stdio: "ignore" });
}
function design(src, out, w, h, cfg, sizes) {
  const vf = [
    `scale=${w}:${h}:force_original_aspect_ratio=increase`,
    `crop=${w}:${h}`,
    "eq=contrast=1.18:saturation=1.25:brightness=0.02",
    "vignette=PI/5",
    `drawbox=x=0:y=ih*0.58:w=iw:h=ih*0.42:color=black@0.62:t=fill`,
    `drawbox=x=iw*0.08:y=ih*0.60:w=iw*0.34:h=${sizes.bar}:color=0x${cfg.accent}:t=fill`,
    `drawtext=${fontOpt}:text='${esc(cfg.tag || "")}':fontsize=${sizes.tag}:fontcolor=white@0.92:box=1:boxcolor=black@0.45:boxborderw=${sizes.tagPad}:x=(w-text_w)/2:y=ih*0.05`,
    `drawtext=${fontOpt}:text='${esc(cfg.line1)}':fontsize=${sizes.head}:fontcolor=white:borderw=${sizes.stroke}:bordercolor=black:x=(w-text_w)/2:y=ih*0.655`,
    `drawtext=${fontOpt}:text='${esc(cfg.line2)}':fontsize=${sizes.head}:fontcolor=0x${cfg.accent}:borderw=${sizes.stroke}:bordercolor=black:x=(w-text_w)/2:y=ih*0.655+${sizes.lineGap}`,
  ].join(",");
  execFileSync("ffmpeg", ["-y", "-i", src, "-vf", vf, "-q:v", "2", out], { stdio: "ignore" });
  console.log(`[thumb] ${out}`);
}

for (const [jobId, cfg] of Object.entries(MAP)) {
  const video = findVideo(jobId);
  if (!video) { console.log(`[thumb] ${jobId}: no rendered video yet — skipped`); continue; }
  const dir = path.dirname(video);
  const t = duration(video) * 0.45;
  const raw = path.join(dir, "_thumbframe.png");
  frame(video, t, raw);
  design(raw, path.join(dir, "thumb_9x16.jpg"), 1080, 1920, cfg, { head: 118, stroke: 10, lineGap: 140, bar: 16, tag: 44, tagPad: 18 });
  design(raw, path.join(dir, "thumb_16x9.jpg"), 1280, 720, cfg, { head: 86, stroke: 8, lineGap: 100, bar: 12, tag: 34, tagPad: 14 });
  design(raw, path.join(dir, "poster.jpg"), 1080, 1350, cfg, { head: 104, stroke: 9, lineGap: 124, bar: 14, tag: 40, tagPad: 16 });
  fs.rmSync(raw, { force: true });
}
console.log("[thumb] Done.");
