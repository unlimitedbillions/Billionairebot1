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
  const outputId = (job.id || "job").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64);
  const dir = path.join(ROOT, "output", outputId);
  // The composer writes the canonical artifact to _compose/final.mp4.
  // Keep the gate aligned with that producer path, while still accepting a
  // legacy top-level MP4 if one exists.
  const candidates = [
    path.join(dir, "_compose", "final.mp4"),
    path.join(dir, "final.mp4"),
  ];
  let videoPath = candidates.find((p) => fs.existsSync(p) && fs.statSync(p).size > 0);
  if (!videoPath && fs.existsSync(dir)) {
    const stack = [dir];
    while (stack.length && !videoPath) {
      const current = stack.pop();
      for (const name of fs.readdirSync(current)) {
        const p = path.join(current, name);
        const st = fs.statSync(p);
        if (st.isDirectory()) stack.push(p);
        else if (name.endsWith(".mp4") && st.size > 0) { videoPath = p; break; }
      }
    }
  }
  if (!videoPath) { console.log(`[duration] ${job.id} | MISSING VIDEO`); outOfSpec.push(job.id); continue; }
  const d = parseFloat(execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", videoPath]).toString());
  const s = SPEC[kind];
  const ok = d >= s.min && d <= s.max;
  console.log(`[duration] ${job.id} | ${d.toFixed(1)}s | ${ok ? "OK" : `OUT OF SPEC (${s.min}-${s.max === Infinity ? "inf" : s.max}s)`}`);
  if (!ok) outOfSpec.push(job.id);
}

fs.mkdirSync(path.join(ROOT, "output"), { recursive: true });
fs.writeFileSync(path.join(ROOT, "output", "out-of-spec.json"), JSON.stringify(outOfSpec, null, 2));
console.log(outOfSpec.length ? `[duration] ${outOfSpec.length} job(s) out of spec -> skipped at publish.` : "[duration] All jobs within spec.");
process.exit(outOfSpec.length ? 1 : 0);
