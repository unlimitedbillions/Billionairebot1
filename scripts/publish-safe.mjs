#!/usr/bin/env node
/**
 * publish-safe.mjs v2
 *  - NEVER crashes: missing TikTok login, publish errors, or empty queues all exit 0.
 *  - TikTok attempted ONLY if credentials exist; otherwise skipped with a warning.
 *  - Skips jobs flagged out-of-spec by verify-durations.mjs.
 *  - Skips jobs already published (data/published.json) to prevent duplicate uploads.
 */
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const ROOT = process.cwd();
const log = (m) => console.log(`[publish-safe] ${m}`);

function parseEnv(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (const raw of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq === -1) continue;
    let val = line.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) val = val.slice(1, -1);
    out[line.slice(0, eq).trim()] = val;
  }
  return out;
}

const env = { ...parseEnv(path.join(ROOT, ".env")), ...process.env };
const i = process.argv.indexOf("--file");
const jobsFile = i > -1 && process.argv[i + 1] ? process.argv[i + 1] : "input/scripts/billionaire-stories.json";

const enabled = String(env.UPLOAD_POST_ENABLED || "").toLowerCase() === "true";
const apiKey = (env.UPLOAD_POST_API_KEY || "").trim();
const username = (env.UPLOAD_POST_USERNAME || "").trim();
const tiktokSession = (env.TIKTOK_SESSION_ID || "").trim();

let platforms = (env.UPLOAD_POST_PLATFORMS || "youtube,youtube_shorts").split(",").map((s) => s.trim()).filter(Boolean);

if (!enabled || !apiKey) { log("Publishing disabled or no UPLOAD_POST_API_KEY -> skipping ALL uploads. Pipeline continues."); process.exit(0); }

// --- TIKTOK GUARD ---
const tiktokAuthed = Boolean((apiKey && username) || tiktokSession);
if (platforms.includes("tiktok") && !tiktokAuthed) {
  log("WARNING: No TikTok login/session found -> skipping TikTok. YouTube continues.");
  platforms = platforms.filter((p) => p !== "tiktok");
}
if (!platforms.length) { log("No platforms left -> nothing to publish. Pipeline continues."); process.exit(0); }

// --- LOAD + FILTER JOBS ---
let jobs = [];
try { jobs = JSON.parse(fs.readFileSync(path.join(ROOT, jobsFile), "utf8")); } catch { log("Could not read jobs file -> continuing."); process.exit(0); }

const specPath = path.join(ROOT, "output", "out-of-spec.json");
let banned = [];
if (fs.existsSync(specPath)) { try { banned = JSON.parse(fs.readFileSync(specPath, "utf8")); } catch {} }
if (banned.length) log(`Duration gate: excluding ${banned.join(", ")}`);

const histPath = path.join(ROOT, "data", "published.json");
let published = [];
if (fs.existsSync(histPath)) { try { published = JSON.parse(fs.readFileSync(histPath, "utf8")); } catch {} }

const before = jobs.length;
jobs = jobs.filter((j) => !banned.includes(j.id) && !published.includes(j.id));
log(`Queue: ${jobs.length}/${before} job(s) new + in-spec.`);
if (!jobs.length) { log("Nothing new to publish. Pipeline continues."); process.exit(0); }

const queuePath = path.join(ROOT, "output", "publish-queue.json");
fs.mkdirSync(path.dirname(queuePath), { recursive: true });
fs.writeFileSync(queuePath, JSON.stringify(jobs, null, 2));

// --- PUBLISH (failures swallowed) ---
log(`Publishing to: ${platforms.join(", ")}`);
const res = spawnSync("npm", ["run", "agentic:post", "--", "--file", "output/publish-queue.json"], {
  cwd: ROOT, env: { ...process.env, UPLOAD_POST_PLATFORMS: platforms.join(",") }, stdio: "inherit", shell: process.platform === "win32",
});
if (res.status !== 0) log(`WARNING: publish exited ${res.status}. Ignoring failure so pipeline continues.`);

// --- RECORD HISTORY ---
try {
  fs.mkdirSync(path.dirname(histPath), { recursive: true });
  const all = Array.from(new Set([...published, ...jobs.map((j) => j.id)]));
  fs.writeFileSync(histPath, JSON.stringify(all, null, 2));
  log("Publish history updated.");
} catch (e) { log(`History write failed (${e.message}) — non-fatal.`); }

log("Publish step finished. Pipeline continues regardless of social auth state.");
process.exit(0);
