#!/usr/bin/env node
/**
 * Verify production visual tags with the SAME parser used by the agentic
 * pipeline. Python preflight validates the asset-manager manifest, but that
 * alone cannot prove that the TypeScript parser binds each final tag to a
 * local file. This closes that gap before CI_ZERO_NETWORK rendering starts.
 */
import * as fs from 'node:fs';
import path from 'node:path';
import { parseScript } from '../src/lib/script-parser.js';
import { inputAssetPath } from '../src/lib/path-safety.js';

interface ProductionJob {
  id?: string;
  title?: string;
  script?: string;
}

const VISUAL_RE = /\[Visual:\s*([^\]]+?)\s*\]/gi;

export async function validateProductionBindings(jobs: ProductionJob[]): Promise<string[]> {
  const errors: string[] = [];

  if (!Array.isArray(jobs) || jobs.length === 0) {
    return ['render-jobs file is empty or is not a JSON array'];
  }

  for (const job of jobs) {
    const id = String(job.id || job.title || 'unknown-job');
    const script = String(job.script || '');
    const tags = [...script.matchAll(VISUAL_RE)].map((m) => (m[1] || '').trim());

    if (tags.length === 0) {
      errors.push(`${id}: no [Visual: ...] tags found in the prepared script`);
      continue;
    }

    let parsed;
    try {
      parsed = await parseScript(script);
    } catch (error) {
      errors.push(`${id}: production parser threw: ${String(error)}`);
      continue;
    }

    if (parsed.scenes.length !== tags.length) {
      errors.push(`${id}: parser produced ${parsed.scenes.length} scenes for ${tags.length} visual tags; every production scene must have exactly one prepared visual`);
      continue;
    }

    for (let index = 0; index < parsed.scenes.length; index++) {
      const tag = tags[index];
      const scene = parsed.scenes[index];
      if (!scene) {
        errors.push(`${id} scene ${index + 1}: parser dropped visual tag "${tag}"`);
        continue;
      }
      if (!scene.localAsset) {
        errors.push(`${id} scene ${index + 1}: visual tag "${tag}" did not bind to a prepared local file`);
        continue;
      }

      if (path.basename(scene.localAsset) !== scene.localAsset) {
        errors.push(`${id} scene ${index + 1}: unsafe non-filename local asset binding "${scene.localAsset}"`);
        continue;
      }
      const resolved = inputAssetPath(scene.localAsset);
      if (!fs.existsSync(resolved)) {
        errors.push(`${id} scene ${index + 1}: parser bound "${scene.localAsset}" but file is missing at ${resolved}`);
      }
    }
  }

  return errors;
}

async function selfTest(): Promise<void> {
  const fixtureName = '.production-binding-self-test.mp4';
  const fixturePath = inputAssetPath(fixtureName);
  fs.mkdirSync(path.dirname(fixturePath), { recursive: true });
  try {
    fs.writeFileSync(fixturePath, Buffer.from('parser-binding-fixture'));
    const good = await validateProductionBindings([
      { id: 'binding-self-test', script: `A narrated test scene. [Visual: ${fixtureName}]` },
    ]);
    if (good.length > 0) throw new Error(`valid local binding was rejected: ${good.join('; ')}`);

    fs.rmSync(fixturePath, { force: true });
    const bad = await validateProductionBindings([
      { id: 'binding-self-test', script: `A narrated test scene. [Visual: ${fixtureName}]` },
    ]);
    if (!bad.some((message) => message.includes('did not bind'))) {
      throw new Error('missing local asset was not detected');
    }
    console.log('[production-bindings] self-test passed (valid binding accepted; missing binding rejected).');
  } finally {
    fs.rmSync(fixturePath, { force: true });
  }
}

async function main(): Promise<void> {
  if (process.argv.includes('--self-test')) {
    await selfTest();
    return;
  }

  const fileIndex = process.argv.indexOf('--file');
  const file = fileIndex >= 0 && process.argv[fileIndex + 1]
    ? path.resolve(process.argv[fileIndex + 1])
    : path.resolve('input/scripts/render-jobs.json');

  if (!fs.existsSync(file)) throw new Error(`production jobs file not found: ${file}`);
  const jobs = JSON.parse(fs.readFileSync(file, 'utf8')) as ProductionJob[];
  const errors = await validateProductionBindings(jobs);

  if (errors.length > 0) {
    for (const error of errors) console.error(`[production-bindings] FAIL: ${error}`);
    throw new Error(`${errors.length} production visual binding issue(s); render stopped before acquiring assets`);
  }

  console.log(`[production-bindings] PASS: all final visual tags bind to local files across ${jobs.length} selected job(s).`);
}

main().catch((error) => {
  console.error(`[production-bindings] ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
});
