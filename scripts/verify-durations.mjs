#!/usr/bin/env node
/** Duration gate: shorts must be 45-59s, long-form >= 120s. Writes output/out-of-spec.json */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const ROOT = process.cwd();
const i = process.argv.indexOf("--file");
const fileArg = i > -1 && process.argv[i + 1] ? process.argv[i + 1] : "input/scripts/billionaire-stories.json";
const jobs = JSON.parse(fs.readFileSync(path.join(ROOT, fileArg), "utf8"));

const SPEC = { short: { min: 45, max: 59 }, long: { min: 120, max: Infinity } };
const outOfSpec = [];

for (const job of jobs) {
  const kind = job.id.endsWith("-short") ? "short" : job.id.endsWith("-long") ? "long" : null;
  if (!kind) continue;
  const dir = path.join(ROOT, "output", job.id);
  const mp4 = fs.existsSync(dir) ? fs.readdirSync(dir).find((f) => f.endsWith(".mp4")) : null;
  if (!mp4) { console.log(`[duration] ${job.id} | MISSING VIDEO`); outOfSpec.push(job.id); continue; }
  const d = parseFloat(execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path.join(dir, mp4)]).toString());
  const s = SPEC[kind];
  const ok = d >= s.min && d <= s.max;
  console.log(`[duration] ${job.id} | ${d.toFixed(1)}s | ${ok ? "OK" : `OUT OF SPEC (${s.min}-${s.max === Infinity ? "inf" : s.max}s)`}`);
  if (!ok) outOfSpec.push(job.id);
}

fs.mkdirSync(path.join(ROOT, "output"), { recursive: true });
fs.writeFileSync(path.join(ROOT, "output", "out-of-spec.json"), JSON.stringify(outOfSpec, null, 2));
console.log(outOfSpec.length ? `[duration] ${outOfSpec.length} job(s) out of spec -> skipped at publish.` : "[duration] All jobs within spec.");
process.exit(0);
