import * as fs from 'fs';
import * as path from 'path';
import { parseScript } from '../../lib/script-parser.js';
import { fetchVisualsForScene, searchImages } from '../../lib/visual-fetcher.js';
import { downloadMedia } from '../../lib/visual-fetcher.js';
import { verifyMedia } from '../../lib/media-verifier.js';
import { resolveFreeBackgroundMusic } from '../../lib/free-music.js';
import { inputAssetPath, inputBgmPath, inputVoiceoverPath } from '../../lib/path-safety.js';
import { LANGUAGE_DEFAULTS } from '../../lib/voice-data.js';
import { buildPlan, applyProEdits } from '../pipeline/plan.js';
import { acquireAssets, AcquireDeps, FetchedVisual } from '../pipeline/acquire.js';
import { verifyAll, VerifyDeps } from '../pipeline/verify.js';
import { runGateway, GatewayDeps } from '../pipeline/gateway.js';
import { runFinalGate } from '../pipeline/gate.js';
import { AgenticWorkspace, getAgenticWorkspace, readJson, writeJson, pruneWorkspaces } from '../management/workspace.js';
import { archiveJob } from '../delivery/archive.js';
import { openReview } from '../delivery/revision.js';
import { createPluginRegistry, registerAllPlugins, getPluginRegistry } from '../plugins/index.js';
import { PluginContext } from '../plugins/core/types.js';
import { generateAgenticVoiceovers } from '../media/tts.js';
import { createJob, updateJob, persistJob } from '../management/job.js';
import { AssetCandidate, AssetDecision, Plan, RenderManifest, assetId } from '../types.js';
import { AgentBackendConfig, AgenticBackend, expandKeywordsHeuristic, writeScriptHeuristic } from '../ai/agent.js';
import { AgentBrain, hasModel, envOpts } from '../ai/brain.js';
import { resolveBridge, type LlmBridge, type DriverLlmCallback } from '../ai/bridge.js';
import { sourceFromUrl } from './source.js';
import { withTimeout, estimateAudioDurationSafe, makePlaceholder, normalizeAudio, runFfmpeg } from './ffmpeg.js';
import { makeContactSheet, writeDecisionsReport } from './artifacts.js';
import type { PipelineRequest, PipelineResult, PipelineProgress } from './types.js';
import { logInfo, logWarn, logError } from '../../shared/logging/runtime-logging.js';

export type { PipelineRequest, PipelineResult, PipelineProgress };

/**
 * Voice is the narration source of truth. Once TTS has produced real audio,
 * picture timing follows that audio exactly. If a platform has a minimum
 * runtime and natural narration is slightly short, add quiet breathing room
 * after each scene rather than slowing the speaker or inventing script text.
 * The audio is physically padded so the renderer, captions and final mux all
 * see the same timeline.
 */
async function alignVoiceoverTimeline(
    plan: Plan,
    voiceovers: import('../media/tts.js').VoiceoverResult,
    platform: PipelineRequest['platform'],
    maxRuntimeSec: number | undefined,
    workspace: AgenticWorkspace,
): Promise<void> {
    // TTS is the timing source of truth. Correction happens before acquisition;
    // this function only synchronizes scene metadata with natural measured audio.
    // It NEVER pads or time-stretches speech to hit a target.
    void platform;
    void maxRuntimeSec;
    void workspace;
    const usable = voiceovers.scenes.filter((v) =>
        Number.isFinite(v.durationSec) && v.durationSec > 0 && fs.existsSync(v.audioPath),
    );
    if (usable.length !== plan.scenes.length) {
        logWarn('⚠ narration timeline sync skipped: only ' + usable.length + '/' + plan.scenes.length + ' scene audio tracks are available');
        return;
    }
    for (const v of usable) {
        const scene = plan.scenes.find((s) => s.sceneNumber === v.sceneIndex + 1);
        if (scene) scene.durationSec = v.durationSec;
    }
    plan.totalDurationSec = usable.reduce((sum, v) => sum + v.durationSec, 0);
    logInfo('🎬 narration-driven timeline synchronized to measured TTS: ' + plan.totalDurationSec.toFixed(1) + 's');
}

export async function runAgenticPipeline(
    req: PipelineRequest,
    onProgress?: (p: PipelineProgress) => void,
): Promise<PipelineResult> {
    const emit = (p: PipelineProgress) => onProgress?.(p);
    const backend: AgenticBackend = req.backend ?? 'agent';
    const cfg: AgentBackendConfig = {
        backend,
        writeScript: req.agent?.writeScript,
        expandKeywords: req.agent?.expandKeywords,
        visionVerify: req.agent?.visionVerify,
        // Control-surface extension — reached from the script JSON
        aiVerify: req.aiVerify,
        brain: req.brain,
    };
    const jobId = req.jobId ?? `job_${Date.now()}`;
    try {
        const pluginCfgPath = path.join(process.cwd(), 'agentic-plugins.config.json');
        let pluginCfg: any = { plugins: [] };
        try {
            if (fs.existsSync(pluginCfgPath)) pluginCfg = JSON.parse(fs.readFileSync(pluginCfgPath, 'utf-8'));
        } catch { /* defaults */ }
        const pctx = new PluginContext({
            jobId,
            workspaceRoot: `./workspace/jobs/${jobId}`,
            config: pluginCfg,
        });
        const preg = await createPluginRegistry(pctx);
        registerAllPlugins(preg, pluginCfg);
    } catch (e) {
        console.warn(`⚠ plugin registry init skipped: ${(e as Error)?.message ?? e}`);
    }
    const sharedImagePool: { url: string }[] = [];
    sharedImagePool.length = 0;

    pruneWorkspaces(req.pruneWorkspaces ?? Number(process.env.AGENTIC_KEEP_WORKSPACES ?? 2));

    // Merge the job-level brain budget ONTO the env-discovered model config.
    // Previously this was a bare override: any job setting brain:{maxCalls}
    // wiped openRouterKey/ollamaUrl/providers, silently degrading every LLM
    // decision (hook, keywords, pacing, music) to heuristics for that job.
    const brainOpts: import('../ai/brain.js').BrainOptions = {
        ...envOpts(),
        ...(cfg.brain ?? {}),
    };
    const driverLLM: DriverLlmCallback | undefined = req.driverLLM;
    const bridge: LlmBridge = resolveBridge({
        hasModelKeys: hasModel(brainOpts ?? envOpts()),
        driverLLM,
        modelOpts: brainOpts,
    });
    const brain = new AgentBrain(brainOpts);

    const script =
        req.script ??                                          // ← custom script with [Visual: ...] tags
        (cfg.writeScript ? await cfg.writeScript(req.topic, req.title) : null) ??
        (
            await bridge.completeJSON<{ script: string }>(
                'You are a short-form video scriptwriter. Write a tight, natural, engaging script with a hook, build, and payoff. 3-5 short sentences. No hashtags, no markup.',
                `Topic: ${req.topic}\nTitle: ${req.title}`,
                '{"script":"..."}',
            )
        )?.script ??
        writeScriptHeuristic(req.topic, req.title);

    // Feature 3 — retention hook: rewrite the opening line when requested.
    // Heuristic always runs; if cfg.optimizeHook AND a brain model is available,
    // the brain writes a stronger hook (falls back to heuristic on any failure).
    const { optimizeHook } = await import('../operations/hook.js');
    const firstLine = script.split(/\n|\. |\.|\? |\?|! |!/)[0] ?? script;
    const hooked = await optimizeHook(firstLine, { useLlm: Boolean(req.optimizeHook), brain: brain.modelEnabled ? brain : undefined });
    const finalScript = hooked.hook !== firstLine.trim()
        ? script.replace(firstLine, hooked.hook)
        : script;
    if (hooked.method === 'llm') logInfo(`🪝 retention hook (LLM): "${hooked.hook}"`);
    else logInfo(`🪝 retention hook (heuristic)`);

    // Language → voice resolution (same as legacy pipeline)
    const resolvedVoice =
        req.language && !req.voice
            ? LANGUAGE_DEFAULTS[req.language.toLowerCase().trim()]
            : req.voice;

    // Production duration policy is format-driven and TTS-measured.
    // Shorts: 50-60s. Long-form: 120-180s.
    // IMPORTANT: do not pre-pad or pre-expand to a fixed word count. The first
    // real TTS pass establishes the speaker's actual words-per-second, then the
    // correction loop calculates the required word count from that measurement.
    const isLongForm = req.platform === 'youtube' && (req.maxRuntimeSec ?? 0) >= 120;
    const durationSpec = isLongForm
        ? { minSec: 120, targetSec: 120, maxSec: Number.POSITIVE_INFINITY, label: 'long', minWords: 780, maxWords: 800 }
        : req.platform === 'shorts'
          ? { minSec: 50, targetSec: 55, maxSec: 60, label: 'short' }
          : null;
    const countWords = (text: string) => text.trim().split(/\s+/).filter(Boolean).length;
    const countSpokenWords = (text: string) => countWords(text.replace(/\[[^\]]*\]/g, ' '));

    const plan = await buildPlan(
        finalScript,
        {
            jobId,
            title: req.title,
            orientation: req.orientation ?? 'portrait',
            voice: resolvedVoice ?? 'en-US-JennyNeural',
            platform: req.platform,
            targetRuntimeSec: req.platform === 'shorts' ? (req.maxRuntimeSec ?? 57) : undefined,
            musicQuery: req.musicQuery,
            // Wave N/O — multi-persona cast: forward the declared persona block
            // so scenes get voicePersona/dialogue assignments. Previously these
            // were only wired in the modular CLI path; the main pipeline
            // silently rendered every persona job with the single default voice.
            ...(req.personas ? { personas: req.personas } : {}),
            ...(req.defaultPersona ? { defaultPersona: req.defaultPersona } : {}),
            ...(req.scenePersonas ? { scenePersonas: req.scenePersonas } : {}),
            ...(req.dialogueVoices ? { dialogueVoices: req.dialogueVoices } : {}),
            ...(req.sceneDialogue ? { sceneDialogue: req.sceneDialogue } : {}),
        },
        parseScript,
    );

    // Apply musicVolume to env for render step (osom will pick it up)
    if (req.musicVolume != null) {
        process.env.AUDIO_FULL_LEVEL = String(req.musicVolume);
    }

    await applyProEdits(plan, {
        hookFirst: req.hookFirst ?? true,
        variablePacing: req.variablePacing ?? true,
        brain,
        platform: req.platform,
        targetRuntimeSec: isLongForm ? undefined : req.maxRuntimeSec,
    });

    // Do not reject narration by a fixed word-count ceiling here.
    // Actual TTS duration is the source of truth. The calibration loop below
    // measures the first real speech pass, calculates the required word count
    // from that measurement, rewrites within a controlled tolerance, and
    // measures again until the format's runtime range is satisfied.
    if (durationSpec) {
        const initialWords = countSpokenWords(plan.scenes.map((s) => s.voiceoverText).join(' '));
        logInfo(
            'TTS_INITIAL_WORDCOUNT format=' + durationSpec.label +
            ' spoken=' + initialWords +
            ' (measured TTS will determine the correction target)',
        );
    }

    // Runtime alignment is applied after real TTS below. Do not alter speech rate here.\n    // Runtime alignment is applied after real TTS below; no pre-TTS rate forcing.\n\n    // Localize burned captions to match a non-English voiceover. When the
    // target language isn't English, translate each scene's voiceoverText and
    // stash it as captionText; the renderer prefers captionText over
    // voiceoverText so on-screen captions match the spoken language. English
    // (or a failed/again-skipped translation) keeps the original text.
    const targetLang = (req.language || 'english').toLowerCase();
    if (targetLang && targetLang !== 'english' && brain) {
        try {
            const { translateScenes } = await import('../media/translate.js');
            const translated = await translateScenes(plan.scenes.map((s) => s.voiceoverText), targetLang, brain);
            if (translated && translated.length === plan.scenes.length) {
                plan.scenes.forEach((s, i) => { if (translated[i] && translated[i] !== s.voiceoverText) s.captionText = translated[i]; });
                logInfo(`🌐 localized ${translated.filter((t, i) => t && t !== plan.scenes[i].voiceoverText).length} caption(s) to ${targetLang}`);
            }
        } catch (e: any) {
            console.warn(`⚠ caption localization skipped: ${e?.message ?? e}`);
        }
    }

    for (const s of plan.scenes) {
        const base = s.voiceoverText || s.searchKeywords.join(' ');
        s.searchKeywords = cfg.expandKeywords
            ? await cfg.expandKeywords(s, req.title)
            : ((await brain.expandKeywords(base, req.title)) ?? expandKeywordsHeuristic(s, req.title));
    }
    if (req.preferVisual) {
        for (const s of plan.scenes) s.visualPreference = req.preferVisual;
    }

    const LOCAL_MEDIA_RE = /\.(jpg|jpeg|png|webp|gif|mp4|mov|webm|m4v)$/i;
    if (req.localAssets && req.localAssets.length > 0) {
        // Only bind to scenes WITHOUT an existing localAsset (set by parseScript from [Visual: ...] tags)
        let li = 0;
        for (const s of plan.scenes) {
            if (!s.localAsset) {
                s.localAsset = req.localAssets[li % req.localAssets.length];
                li++;
            }
        }
        emit({ stage: 'plan', percent: 100, message: `Bound ${req.localAssets.length} local asset(s) to ${plan.scenes.length} scenes` });
    } else if (req.autoLocalAssets) {
        // Opt-in ONLY. Without this flag we do NOT scan input/visuals/, because
        // doing so unconditionally hijacked every stock-based job with whatever
        // file happened to be there (e.g. one brand_cover.jpg bound to all
        // scenes → every video was a flat swatch). Stock acquisition is default.
        try {
            const assetsDir = inputAssetPath();
            if (fs.existsSync(assetsDir)) {
                const files = fs.readdirSync(assetsDir).filter((f) => LOCAL_MEDIA_RE.test(f));
                if (files.length > 0) {
                    req.localAssets = files.sort();
                    // Only bind to scenes WITHOUT an existing localAsset
                    let li = 0;
                    for (const s of plan.scenes) {
                        if (!s.localAsset) {
                            s.localAsset = req.localAssets[li % req.localAssets.length];
                            li++;
                        }
                    }
                    emit({ stage: 'plan', percent: 100, message: `Auto-detected ${files.length} local asset(s) from input/visuals/ → bound to ${plan.scenes.length} scenes` });
                }
            }
        } catch { /* input/visuals/ may not exist or be inaccessible; skip silently */ }
    }
    if (req.videoClips && req.videoClips.length > 0) {
        plan.scenes.forEach((s, i) => {
            const clip = req.videoClips![i % req.videoClips!.length];
            if (clip) {
                s.localAsset = path.basename(clip);
                s.visualPreference = 'video';
            }
        });
        emit({ stage: 'plan', percent: 100, message: `Bound ${req.videoClips.length} video clip(s) to ${plan.scenes.length} scenes` });
    }
    if (req.personalAudio && req.personalAudio.length > 0) {
        plan.scenes.forEach((s, i) => {
            const a = req.personalAudio![i % req.personalAudio!.length];
            if (a) s.personalAudio = path.basename(a);
        });
        emit({ stage: 'plan', percent: 100, message: `Bound ${req.personalAudio.length} personal audio track(s)` });
    }

    if (req.dryRun) {
        emit({ stage: 'plan', percent: 100, message: `DRY RUN — ${plan.scenes.length} scenes, no assets fetched` });
        return {
            backend,
            plan,
            workspace: {
                jobId: 'dry-run',
                root: '', assetsDir: '', imagesDir: '', videosDir: '', musicDir: '', verificationDir: '',
            } as AgenticWorkspace,
            candidates: [],
            decisions: [],
            gate: { pass: false, checks: [] },
            manifest: null as any,
            voiceovers: null,
            fullyAgentDriven: backend === 'agent' && !cfg.visionVerify,
            dryRunInfo: {
                voice: resolvedVoice ?? 'en-US-JennyNeural',
                musicQuery: req.musicQuery ?? plan.musicQuery,
                musicEnabled: req.music !== false,
                searchQueries: plan.scenes.map(s => s.searchKeywords),
                orientation: req.orientation ?? 'portrait',
                estimatedDurationSec: plan.totalDurationSec,
            },
        };
    }

    emit({ stage: 'plan', percent: 100, message: `Plan ready (${plan.scenes.length} scenes)` });

    const STOP = new Set([
        'a', 'an', 'the', 'of', 'for', 'to', 'and', 'or', 'in', 'on', 'with', 'about',
        'facts', 'fact', 'benefits', 'benefit', 'how', 'what', 'why', 'tips', 'ways', 'things',
        '5', '3', '10', 'top', 'best', 'amazing', 'fascinating', 'interesting',
        'daily', 'changed', 'change', 'vs',
    ]);
    const topicNoun =
        ((req.topic || plan.title || 'video') as string)
            .toLowerCase()
            .split(/\s+/)
            .filter((w) => w && !STOP.has(w.replace(/[^a-z]/g, '')))
            .join(' ') || 'video';
    const preferVideo = plan.scenes.some((s) => s.visualPreference === 'video');
    const getImagePool = async () => {
        if ((req.localAssets && req.localAssets.length > 0) || (req.videoClips && req.videoClips.length > 0)) {
            return sharedImagePool;
        }
        if (sharedImagePool.length > 0) return sharedImagePool;
        const DEAD_HOSTS = /flickr\.com|staticflickr\.com|live\.staticflickr/i;
        const seen = new Set<string>();
        const add = (url?: string) => {
            if (url && !DEAD_HOSTS.test(url) && !seen.has(url)) {
                seen.add(url);
                sharedImagePool.push({ url });
            }
        };
        const variants = [topicNoun, `${topicNoun} photo`, (req.title || '').trim(), `person ${topicNoun}`]
            .map((s) => s.trim())
            .filter(Boolean);
        // Every fallback fetch below MUST be time-bounded. These calls used
        // to run bare (no withTimeout) — one wedged provider request hung the
        // entire pipeline indefinitely (observed twice in matrix QA).
        // Bounded to 12s (down from 20s) — on RAM-tight dev rigs, 4 parallel × 20s
        // was 80s worst-case tail. 12s still catches slow providers without the drag.
        const POOL_FETCH_TIMEOUT_MS = 12000;
        for (const q of variants) {
            if (preferVideo) {
                try {
                    const r = await withTimeout(fetchVisualsForScene([q], true, plan.orientation), POOL_FETCH_TIMEOUT_MS, `pool[video:${q}]`);
                    if (r) add(Array.isArray(r) ? r[0]?.url : r.url);
                } catch { /* next */ }
                try {
                    (await withTimeout(searchImages(q, 12, 2, plan.orientation, 1), POOL_FETCH_TIMEOUT_MS, `pool[img:${q}]`)).forEach((p) => add(p.url));
                } catch { /* next */ }
                try {
                    const r = await withTimeout(fetchVisualsForScene([q], false, plan.orientation), POOL_FETCH_TIMEOUT_MS, `pool[image:${q}]`);
                    if (r) add(Array.isArray(r) ? r[0]?.url : r.url);
                } catch { /* next */ }
            } else {
                try {
                    (await withTimeout(searchImages(q, 12, 2, plan.orientation, 1), POOL_FETCH_TIMEOUT_MS, `pool[img:${q}]`)).forEach((p) => add(p.url));
                } catch { /* next */ }
                try {
                    const r = await withTimeout(fetchVisualsForScene([q], false, plan.orientation), POOL_FETCH_TIMEOUT_MS, `pool[image:${q}]`);
                    if (r) add(Array.isArray(r) ? r[0]?.url : r.url);
                } catch { /* next */ }
            }
            if (sharedImagePool.length >= 12) break;
        }
        if (sharedImagePool.length === 0) {
            // P2#1: race multiple loupe candidates in parallel; first hit wins.
            // Previously a single 12s sequential fallback — multiplied worst-case
            // cold-start when that one slow provider also timed out.
            const candidates = [topicNoun, 'nature', 'city', 'technology']
                .map((s) => (s || '').trim())
                .filter(Boolean)
                .slice(0, 3);
            try {
                const res = await Promise.any(
                    candidates.map((cand) =>
                        withTimeout(
                            fetchVisualsForScene([cand], preferVideo, plan.orientation),
                            10000,
                            `fetchVisual[lastditch:${cand}]`,
                        ).then((r) => {
                            // Reject empty results so Promise.any keeps waiting
                            const hit = r && (Array.isArray(r) ? r[0]?.url : r.url);
                            if (!hit) throw new Error('empty');
                            return r;
                        }),
                    ),
                );
                add(Array.isArray(res) ? res[0]?.url : res.url);
            } catch { /* all last-ditch candidates failed — pool stays empty */ }
        }
        return sharedImagePool;
    };

    const acquireDeps: AcquireDeps = {
        fetchVisual: async (keywords, kind, orientation, sceneIndex = 0) => {
            // Placeholder orientation must match the job so fallback cards are
            // never pillarboxed (makePlaceholder reads this env once per call).
            process.env.AGENTIC_PLACEHOLDER_ORIENTATION =
                plan.orientation === 'landscape' ? 'landscape' : plan.orientation === 'square' ? 'square' : 'portrait';
            // Phase 1: targeted search with per-scene resultIndex
            const bySpecificity = [...keywords].sort((a, b) => b.length - a.length);
            const ladder = [bySpecificity, keywords];
            if (keywords.length > 1) ladder.push([keywords[0]]);
            ladder.push([topicNoun || 'nature', 'city', 'technology'].slice(0, 1));
            const seen = new Set<string>();
            // Provider-level retry: one 12s attempt per hop burned scenes on
            // flaky networks (observed: 3-5 hops × timeout = scene starved).
            // Two attempts with short backoff + jitter doubles the success
            // window without meaningfully extending the worst case.
            const FETCH_ATTEMPTS = Number(process.env.ACQUIRE_FETCH_ATTEMPTS ?? 2);
            for (const raw of ladder) {
                const q = raw.filter(Boolean);
                const key = q.join(' ');
                if (seen.has(key)) continue;
                seen.add(key);
                let usable: FetchedVisual[] | null = null;
                for (let attempt = 1; attempt <= Math.max(1, FETCH_ATTEMPTS); attempt++) {
                    try {
                        const resultIndex = sceneIndex;
                        const res = await withTimeout(
                            fetchVisualsForScene(q, kind === 'video', orientation, undefined, resultIndex),
                            12000,
                            `fetchVisual[${q.join(' ')}]`,
                        );
                        const arr = !res ? [] : Array.isArray(res) ? res : [res];
                        const DEAD_HOSTS = /flickr\.com|staticflickr\.com|live\.staticflickr/i;
                        const found = arr.filter(
                            (a) => a && typeof a.url === 'string' && a.url.length > 0 && !DEAD_HOSTS.test(a.url),
                        );
                        if (found.length > 0) {
                            // Honest source: derive from the URL host when the
                            // fetcher doesn't carry an explicit source. Never
                            // hardcode a provider name — matrix QA found a
                            // solid-color gradient mislabeled 'openverse/pexels'.
                            usable = found.map(
                                (a) =>
                                    ({
                                        url: a.url,
                                        localPath: '',
                                        source: a.url?.startsWith('http') ? sourceFromUrl(a.url) : (a.photographer || 'unknown'),
                                        license: a.license,
                                        licenseUrl: a.licenseUrl,
                                        // Source title (Wikimedia file title etc.) — feeds
                                        // the acquire-stage key-free relevance gate.
                                        title: (a as any).title,
                                    }) as FetchedVisual,
                            );
                            break;
                        }
                    } catch (e) {
                        console.warn(`⚠ fetchVisual failed for "${q.join(' ')}" (attempt ${attempt}/${FETCH_ATTEMPTS}): ${(e as Error).message}`);
                    }
                    if (attempt < FETCH_ATTEMPTS) {
                        // Backoff + jitter: 600-1100ms after attempt 1. Short —
                        // the ladder itself is the bigger retry mechanism.
                        await new Promise((r) => setTimeout(r, 600 + Math.floor(Math.random() * 500)));
                    }
                }
                if (usable && usable.length > 0) {
                    return usable;
                }
            }

            // Phase 2: image pooling is valid only for image scenes.
            // A video scene must never silently downgrade to a still image.
            if (kind !== 'video') {
                const pool = await getImagePool();
                if (pool.length > 0) {
                const pick = pool[sceneIndex % pool.length];
                const DEAD_HOSTS = /flickr\.com|staticflickr\.com|live\.staticflickr/i;
                if (pick && pick.url && !DEAD_HOSTS.test(pick.url)) {
                    const source = sourceFromUrl(pick.url);
                    return [
                        {
                            url: pick.url,
                            localPath: '',
                            source,
                            license: undefined,
                            licenseUrl: undefined,
                            width: 0,
                            height: 0,
                        },
                    ];
                }
                }
            }
            if (kind === 'video') return [];
            const ph = makePlaceholder(keywords, kind);
            return [
                {
                    url: '',
                    localPath: ph,
                    source: 'placeholder',
                    license: 'CC0 (generated placeholder)',
                    licenseUrl: '',
                } as FetchedVisual,
            ];
        },
        download: async (url, dir, filename) => {
            const useDefaultVisual = (): string => {
                const local = require('path').join(
                    dir,
                    filename.replace(/(\.[^.]+)?$/, path.extname(req.defaultVisual || '.png')),
                );
                try {
                    const src = inputAssetPath(req.defaultVisual!);
                    if (fs.existsSync(src)) {
                        fs.mkdirSync(dir, { recursive: true });
                        fs.copyFileSync(src, local);
                        return local;
                    }
                } catch { /* ignore */ }
                return '';
            };
            for (let attempt = 0; attempt < 3; attempt++) {
                try {
                    const r = await downloadMedia(url, dir, filename);
                    return r.path;
                } catch (e) {
                    const isLast = attempt === 2;
                    if (isLast) {
                        console.warn(`⚠ download failed for "${url}": ${(e as Error).message}. Using placeholder card.`);
                        const local = require('path').join(dir, filename.replace(/(\.[^.]+)?$/, '.png'));
                        const def = req.defaultVisual ? useDefaultVisual() : '';
                        if (!def) {
                            // Pass a human label, NOT the downloaded filename
                            // (e.g. 'candidate_1.png') — burning a filename into the
                            // frame is a defect. The download dep only gets url/dir/
                            // filename, so use the job title as the fallback label.
                            const ph = makePlaceholder([req.title || topicNoun || "scene"], "image");
                            try {
                                require('fs').copyFileSync(ph, local);
                            } catch (e) {
                                console.warn(`⚠ placeholder copy failed for ${filename}: ${(e as Error)?.message}`);
                            }
                        }
                        return def || local;
                    }
                    await new Promise((res) => setTimeout(res, 600 * (attempt + 1)));
                }
            }
            const local = require('path').join(dir, filename.replace(/(\.[^.]+)?$/, '.png'));
            const def = req.defaultVisual ? useDefaultVisual() : '';
            if (!def) {
                // Pass a human label, NOT the downloaded filename (e.g.
                // 'candidate_1.png') — burning a filename into the frame is a
                // defect. Use the job title as the fallback label.
                const ph = makePlaceholder([req.title || topicNoun || "scene"], "image");
                try {
                    require('fs').copyFileSync(ph, local);
                } catch (e) {
                    console.warn(`⚠ placeholder copy failed for ${filename}: ${(e as Error)?.message}`);
                }
            }
            return def || local;
        },
        fetchMusic: async (query, count) => {
            // Opt-out: music: false → no background music (voice-only final).
            // This is the no-music side of the 9:16/1:1/16:9 × music/no-music
            // variety matrix. Acquire then produces zero music candidates and
            // compose skips the music mix stage entirely.
            if (req.music === false) {
                logInfo('  🎵 Background music disabled (music: false) — voice-only final');
                return [];
            }
            // backgroundMusic override: use local file instead of searching
            if (req.backgroundMusic) {
                const bgmPath = inputAssetPath(req.backgroundMusic);
                if (fs.existsSync(bgmPath)) {
                    logInfo(`  🎵 Using custom background music: ${req.backgroundMusic}`);
                    const normalized = normalizeAudio(bgmPath);
                    const finalPath = normalized && fs.existsSync(normalized) ? normalized : bgmPath;
                    if (finalPath) {
                        return [{
                            url: '',
                            localPath: finalPath,
                            source: 'local',
                            license: 'CC-BY (user provided)',
                            licenseUrl: '',
                        }];
                    }
                } else {
                    console.warn(`  ⚠ backgroundMusic file not found: ${req.backgroundMusic} (in input/visuals/) — falling back to stock music`);
                }
            }
            const zeroNetwork = process.env.CI_ZERO_NETWORK === '1';
            if (zeroNetwork) {
                const fallback = [inputBgmPath('twenty_minutes.mp3'), inputBgmPath('two_minutes.mp3')].find((p) =>
                    fs.existsSync(p),
                );
                if (!fallback) {
                    logWarn('  ⚠ zero-network music: no bundled local track found; continuing voice-only');
                    return [];
                }
                const normalized = normalizeAudio(fallback);
                const finalPath = normalized && fs.existsSync(normalized) ? normalized : fallback;
                logInfo('  🎵 Zero-network render: using bundled local background music');
                return [{
                    url: '',
                    localPath: finalPath,
                    source: 'local-bundled',
                    license: 'Bundled project asset',
                    licenseUrl: '',
                }];
            }
            const tracks = [];
            for (let i = 0; i < count; i++) {
                const m = await resolveFreeBackgroundMusic({ query, enabled: true });
                let localPath = m?.localPath && fs.existsSync(m.localPath) ? m.localPath : '';
                if (!localPath) {
                    const fallback = [inputBgmPath('twenty_minutes.mp3'), inputBgmPath('two_minutes.mp3')].find((p) =>
                        fs.existsSync(p),
                    );
                    localPath = fallback ?? makePlaceholder([query], 'music');
                }
                const normalized = normalizeAudio(localPath);
                const finalPath = normalized && fs.existsSync(normalized) ? normalized : localPath;
                if (finalPath)
                    tracks.push({
                        url: '',
                        localPath: finalPath,
                        source: m?.track.provider ?? 'local',
                        license: m?.track.license ?? 'CC-BY (assumed royalty-free)',
                        licenseUrl: m?.track.licenseUrl ?? '',
                    } as FetchedVisual);
            }
            return tracks;
        },
    };
    if (cfg.aiVerify?.enabled) {
        acquireDeps.cfg = cfg as any;
        acquireDeps.bridge = bridge;
    }
    // Local material pool (off by default): bind scenes to input/visuals files.
    if (req.localPool) acquireDeps.localPool = true;
    // Per-stage timebox: don't let acquisition wedge the whole pipeline.
    // This must stay LONGER than the sum of the in-acquire soft deadlines
    // (ACQUIRE_FETCH_DEADLINE_MS + ACQUIRE_SOFT_DEADLINE_MS ≈ 185s) — those
    // flush partial results gracefully; when THIS fired first (old default
    // 120s) it abandoned the whole acquireAssets promise and discarded every
    // candidate, forcing the offline fallback even though assets were arriving.
    // Default 300s = last-resort safety net only; override with ACQUIRE_TIMEBOX_MS.
    // ── NARRATION-FIRST PIPELINE ─────────────────────────────────────────────
    // Generate real scene audio BEFORE acquiring visuals. The narration is the
    // semantic source for visual queries, and measured audio is the duration
    // source for every scene.
    let voiceovers: import('../media/tts.js').VoiceoverResult | null = null;
    const voiceWorkspace = getAgenticWorkspace(jobId);

    try {
        const { runVoiceStage } = await import('../media/voice-controller.js');
        const res = await runVoiceStage(plan, voiceWorkspace, req.voice, (percent, message) => {
            emit({ stage: 'voiceover', percent, message });
        }, req.useClonedVoiceId, req.personas);
        voiceovers = {
            scenes: res.voices.map((v) => ({
                sceneIndex: v.sceneIndex,
                audioPath: v.audioPath,
                durationSec: v.durationSec,
                captionSegments: [],
            })),
            voiceoverDriven: res.voiceoverDriven,
            sidecars: [],
            fallbackUsed: res.fallbackUsed,
        };
        emit({
            stage: 'voiceover',
            percent: 100,
            message: `Voiceover ${res.voiceoverDriven ? 'generated (speech backend)' : 'partial via speech backend'} BEFORE visual acquisition`,
        });
    } catch (e: any) {
        console.warn(`⚠ speech backend voice stage failed ("${e?.message}"); falling back to Edge-TTS BEFORE visual acquisition`);
        voiceovers = await generateAgenticVoiceovers(plan, voiceWorkspace, req.voice, undefined, req.personalAudio?.[0]);
        emit({
            stage: 'voiceover',
            percent: 100,
            message: `Voiceover ${voiceovers.voiceoverDriven ? 'generated (Edge-TTS fallback)' : 'fallback tones'} BEFORE visual acquisition`,
        });
    }

    // MEASURE -> CALCULATE WORD COUNT -> REGENERATE -> MEASURE AGAIN.
    // The first TTS pass is the calibration pass. This prevents the old fixed
    // 780-800 word assumption from turning a ~102s narration into ~180s.
    const durationReport = {
        format: durationSpec?.label ?? 'unbounded',
        minimumSec: durationSpec?.minSec ?? 0,
        targetSec: durationSpec?.targetSec ?? 0,
        maximumSec: durationSpec?.maxSec ?? 0,
        attempts: [] as { attempt: number; measuredSec: number; currentWords: number; requiredWords: number }[],
        passedBeforeVisualAcquisition: !durationSpec,
        finalMeasuredSec: 0,
    };

    const hasRealTts = () =>
        Boolean(voiceovers?.voiceoverDriven) &&
        (voiceovers?.scenes ?? []).length === plan.scenes.length &&
        (voiceovers?.scenes ?? []).every((v) =>
            fs.existsSync(v.audioPath) &&
            Number(v.durationSec) > 0 &&
            !/_silent\.(wav|mp3|m4a|ogg)$/i.test(v.audioPath),
        );

    const measureNarration = () =>
        (voiceovers?.scenes ?? []).reduce((sum, v) => sum + (Number(v.durationSec) || 0), 0);

    if (durationSpec) {
        if (!hasRealTts()) {
            logWarn('⚠ initial TTS was incomplete/non-speech; retrying through the Edge-TTS fallback before measuring duration');
            voiceovers = await generateAgenticVoiceovers(
                plan, voiceWorkspace, req.voice, undefined, req.personalAudio?.[0],
            );
        }
        if (!hasRealTts()) {
            throw new Error('REAL_TTS_DURATION_FAIL: complete real speech is required before duration validation');
        }

        const MIN_LONG_WORDS = durationSpec.minWords ?? 1;
        const MAX_LONG_WORDS = durationSpec.maxWords ?? Number.POSITIVE_INFINITY;
        let narration = plan.scenes.map((s) => s.voiceoverText).join(' ').replace(/\s+/g, ' ').trim();
        let currentWords = countSpokenWords(narration);

        // Long-form content contract: keep the generated narration at 780-800
        // spoken words. Runtime is validated separately from actual TTS.
        if (durationSpec.label === 'long' && (currentWords < MIN_LONG_WORDS || currentWords > MAX_LONG_WORDS)) {
            const targetWords = Math.min(
                MAX_LONG_WORDS,
                Math.max(MIN_LONG_WORDS, Math.round((MIN_LONG_WORDS + MAX_LONG_WORDS) / 2)),
            );
            const rewrite = await bridge.completeJSON<{ script: string }>(
                'Rewrite this billionaire story as natural spoken narration. Preserve names, chronology and existing factual claims. Do not invent facts. ' +
                'Return one continuous narration with no headings, visual tags, hashtags or meta commentary. ' +
                'Target 780-800 spoken words, and keep the result inside that range.',
                JSON.stringify({
                    title: req.title,
                    topic: req.topic,
                    currentNarration: narration,
                    currentWords,
                    targetWords,
                    acceptedWordRange: [MIN_LONG_WORDS, MAX_LONG_WORDS],
                }),
                '{"script":"..."}',
            );
            const candidate = typeof rewrite?.script === 'string'
                ? rewrite.script.replace(/\s+/g, ' ').trim()
                : '';
            const candidateWords = candidate ? countSpokenWords(candidate) : 0;
            if (!candidate || candidateWords < MIN_LONG_WORDS || candidateWords > MAX_LONG_WORDS) {
                throw new Error(
                    'LONG_FORM_WORDCOUNT_FAIL: generated narration must contain ' +
                    MIN_LONG_WORDS + '-' + MAX_LONG_WORDS + ' spoken words; received ' + candidateWords,
                );
            }

            const correctedPlan = await buildPlan(
                candidate,
                {
                    jobId,
                    title: req.title,
                    orientation: req.orientation ?? 'portrait',
                    voice: resolvedVoice ?? 'en-US-JennyNeural',
                    platform: req.platform,
                    musicQuery: req.musicQuery,
                    ...(req.personas ? { personas: req.personas } : {}),
                    ...(req.defaultPersona ? { defaultPersona: req.defaultPersona } : {}),
                    ...(req.scenePersonas ? { scenePersonas: req.scenePersonas } : {}),
                    ...(req.dialogueVoices ? { dialogueVoices: req.dialogueVoices } : {}),
                    ...(req.sceneDialogue ? { sceneDialogue: req.sceneDialogue } : {}),
                },
                parseScript,
            );
            await applyProEdits(correctedPlan, {
                hookFirst: req.hookFirst ?? true,
                variablePacing: req.variablePacing ?? true,
                brain,
                platform: req.platform,
                targetRuntimeSec: req.platform === 'shorts' ? 55 : undefined,
            });

            const parsedWords = countSpokenWords(correctedPlan.scenes.map((s) => s.voiceoverText).join(' '));
            if (parsedWords < MIN_LONG_WORDS || parsedWords > MAX_LONG_WORDS) {
                throw new Error(
                    'LONG_FORM_WORDCOUNT_FAIL: parsed narration must contain ' +
                    MIN_LONG_WORDS + '-' + MAX_LONG_WORDS + ' spoken words; received ' + parsedWords,
                );
            }
            plan.scenes = correctedPlan.scenes;
            plan.totalDurationSec = correctedPlan.totalDurationSec;
            plan.musicQuery = correctedPlan.musicQuery;

            try {
                const { runVoiceStage } = await import('../media/voice-controller.js');
                const retryVoices = await runVoiceStage(
                    plan, voiceWorkspace, req.voice,
                    (percent, message) => emit({ stage: 'voiceover', percent, message: 'wordcount-correction: ' + message }),
                    req.useClonedVoiceId, req.personas,
                );
                voiceovers = {
                    scenes: retryVoices.voices.map((v: any) => ({
                        sceneIndex: v.sceneIndex,
                        audioPath: v.audioPath,
                        durationSec: v.durationSec,
                        captionSegments: [],
                    })),
                    voiceoverDriven: retryVoices.voiceoverDriven,
                    sidecars: [],
                    fallbackUsed: retryVoices.fallbackUsed,
                };
            } catch {
                voiceovers = await generateAgenticVoiceovers(
                    plan, voiceWorkspace, req.voice, undefined, req.personalAudio?.[0],
                );
            }
            if (!hasRealTts()) {
                voiceovers = await generateAgenticVoiceovers(
                    plan, voiceWorkspace, req.voice, undefined, req.personalAudio?.[0],
                );
            }
            if (!hasRealTts()) {
                throw new Error('REAL_TTS_DURATION_FAIL: corrected narration did not produce complete real speech');
            }

            narration = plan.scenes.map((s) => s.voiceoverText).join(' ').replace(/\s+/g, ' ').trim();
            currentWords = countSpokenWords(narration);
        }

        const measuredSec = measureNarration();
        const finalWords = countSpokenWords(plan.scenes.map((s) => s.voiceoverText).join(' '));
        if (durationSpec.label === 'long' && (finalWords < MIN_LONG_WORDS || finalWords > MAX_LONG_WORDS)) {
            throw new Error(
                'LONG_FORM_WORDCOUNT_FAIL: final narration contains ' + finalWords +
                ' spoken words; required ' + MIN_LONG_WORDS + '-' + MAX_LONG_WORDS,
            );
        }

        durationReport.attempts.push({
            attempt: 1,
            measuredSec,
            currentWords: finalWords,
            requiredWords: finalWords,
        });
        durationReport.passedBeforeVisualAcquisition = measuredSec >= durationSpec.minSec;
        durationReport.finalMeasuredSec = measuredSec;

        writeJson(voiceWorkspace, 'duration-calibration.json', durationReport);

        if (measuredSec < durationSpec.minSec) {
            throw new Error(
                'TTS_DURATION_MIN_FAIL: ' + durationSpec.label +
                ' measured ' + measuredSec.toFixed(1) + 's; minimum required is ' +
                durationSpec.minSec + 's. No maximum duration is enforced for long-form.',
            );
        }

        logInfo(
            'TTS_DURATION_PASS format=' + durationSpec.label +
            ' measured=' + measuredSec.toFixed(1) + 's; minimum=' +
            durationSpec.minSec + 's; maximum=unbounded; words=' + finalWords,
        );
    }

    // Recompute the visual semantics only after the final narration exists and
    // immediately before search. This prevents a stale pre-TTS query from
    // deciding the picture content.
    for (const s of plan.scenes) {
        const narration = (s.voiceoverText || '').trim();
        if (!narration) continue;
        s.searchKeywords = cfg.expandKeywords
            ? await cfg.expandKeywords(s, req.title)
            : ((await brain.expandKeywords(narration, req.title)) ?? expandKeywordsHeuristic(s, req.title));
    }
    writeJson(voiceWorkspace, 'narration-first.json', {
        jobId,
        voiceoverDriven: voiceovers.voiceoverDriven,
        scenes: plan.scenes.map((s) => ({
            sceneNumber: s.sceneNumber,
            narration: s.voiceoverText,
            visualQuery: s.searchKeywords,
            durationSec: voiceovers?.scenes?.find((v) => v.sceneIndex === s.sceneNumber - 1)?.durationSec ?? 0,
        })),
        generatedAt: new Date().toISOString(),
    });

    const acquireTimeboxMs = Number(process.env.ACQUIRE_TIMEBOX_MS ?? 300000);
    // Placeholder/fallback orientation follows the job (read by makePlaceholder
    // and generateFallbackVisual so cards match the frame, never pillarboxed).
    process.env.AGENTIC_JOB_ORIENTATION = plan.orientation === 'landscape' ? 'landscape' : plan.orientation === 'square' ? 'square' : 'portrait';
    process.env.AGENTIC_PLACEHOLDER_ORIENTATION = process.env.AGENTIC_JOB_ORIENTATION;
    const acquirePromise = acquireAssets(plan, acquireDeps, req.candidatesPerAsset ?? 2);
    const { workspace, candidates } = await withTimeout(acquirePromise, acquireTimeboxMs, 'acquireAssets')
        .catch((e) => {
            logWarn(`⚠ acquire timed out after ${acquireTimeboxMs}ms — proceeding with ${0} candidates`);
            return { workspace: { jobId, root: '', assetsDir: '', imagesDir: '', videosDir: '', musicDir: '', verificationDir: '' } as any, candidates: [] as any[] };
        });
    const zeroNetworkRender = process.env.CI_ZERO_NETWORK === '1';
    if (zeroNetworkRender) {
        const missingScenes = plan.scenes.filter((scene, index) => {
            const expectedKind: 'image' | 'video' =
                scene.visualPreference === 'video' || scene.visualPreference === 'video-gen' || scene.visualPreference === 'video-gen-local'
                    ? 'video'
                    : 'image';
            return !candidates.some(
                (candidate) =>
                    candidate.sceneIndex === index &&
                    candidate.kind === expectedKind &&
                    fs.existsSync(candidate.localPath),
            );
        });
        if (missingScenes.length > 0) {
            throw new Error(
                'ZERO_NETWORK_ASSET_MISS: ' +
                missingScenes.length +
                ' scene(s) have no prepared local visual after PREFLIGHT: ' +
                missingScenes.map((s) => s.sceneNumber).join(', '),
            );
        }
        logInfo('ZERO_NETWORK_RENDER_CHECK local visual coverage=' + plan.scenes.length + '/' + plan.scenes.length);
    }
    emit({ stage: 'acquire', percent: 100, message: `Acquired ${candidates.length} candidates` });
    writeJson(workspace, 'plan.json', plan);
    // ADVANCED (timeline IR): persist timeline.json + director.json best-effort.
    // Additive observability — never blocks pipeline on failure.
    try {
        const { buildTimelineArtifacts, persistTimelineArtifacts } = await import('../timeline/integrate.js');
        const artifacts = buildTimelineArtifacts(plan as never, null, {
            jCutSec: (req as { jCutSec?: number }).jCutSec ?? 0.4,
            crossfadeSec: (req as { crossfadeSec?: number }).crossfadeSec ?? 0.5,
        });
        if (artifacts && (workspace as { root?: string }).root) {
            persistTimelineArtifacts((workspace as { root: string }).root, artifacts);
        }
    } catch {
        /* timeline persistence is observability-only */
    }
    const jobRec = createJob(jobId, workspace, { topic: req.topic, title: req.title, backend, state: 'processing' });

    const vision = cfg.visionVerify
        ? cfg.visionVerify
        : backend === 'vision'
          ? (p: string, kw: string[]) => verifyMedia(p, kw)
          : async () => ({
                passes: true,
                confidence: 6,
                reason: 'agent backend: signal-only; visual relevance not AI-scored',
            });
    const verifyDeps: VerifyDeps = {
        verifyImage: (p, kw) => vision(p, kw),
        verifyVideo: (p, kw) => vision(p, kw),
    };

    const gatewayDeps: GatewayDeps = {
        ...acquireDeps,
        ...verifyDeps,
        decide: async (c, v) => {
            const decisions = readJson<AssetDecision[]>(workspace, 'approval-manifest.json') ?? [];
            const approvedInScene = decisions.filter(
                (d) => d.sceneIndex === c.sceneIndex && d.decision === 'approved',
            ).length;
            // Diversity context: color hashes of already-approved assets so the
            // agent's scoring can penalize near-duplicate visuals per scene.
            const { agentDecide, computeApprovedHashes } = await import('../ai/agent.js');
            const approvedHashes = computeApprovedHashes(candidates, decisions);
            const result = agentDecide({ candidate: c, verification: v as any, approvedInScene, approvedHashes });
            emit({
                stage: 'decide',
                percent: 50,
                message: `[s${c.sceneIndex} c${c.candidateIndex}] ${result.decision}`,
                sceneIndex: c.sceneIndex,
                candidateIndex: c.candidateIndex,
            });
            return result;
        },
    };

    emit({ stage: 'verify', percent: 100, message: 'Verification complete' });
    const { decisions } = await runGateway(plan, candidates, gatewayDeps);
    emit({
        stage: 'decide',
        percent: 100,
        message: `${decisions.filter((d) => d.decision === 'approved').length} assets approved`,
    });
    const manifest = readJson<RenderManifest>(workspace, 'render-manifest.json');
    // Platform-aware runtime cap (X5): map the job's declared platform to the
    // gate's platform table instead of defaulting every job to Shorts' 60s.
    const PLATFORM_TO_GATE = { tiktok: 'tiktok', youtube: 'youtube', instagram: 'reels', reels: 'reels', shorts: 'shorts' } as const;
    const gatePlatform =
        req.platform && PLATFORM_TO_GATE[req.platform]
            ? (PLATFORM_TO_GATE[req.platform] as 'shorts' | 'tiktok' | 'reels' | 'youtube')
            : undefined;
    const gate = runFinalGate(plan, candidates, decisions, manifest, {
        ...(gatePlatform ? { platform: gatePlatform } : {}),
        ...(durationSpec ? {
            minRuntimeSec: durationSpec.minSec,
            maxRuntimeSec: durationSpec.maxSec,
        } : (req.maxRuntimeSec ? { maxRuntimeSec: req.maxRuntimeSec } : {})),
    });
    emit({ stage: 'gate', percent: 100, message: gate.pass ? 'GATE PASS' : 'GATE FAIL' });

    // ── OFFLINE FALLBACK: if gate failed due to missing visuals, retry with bundled assets ──
    let offlineFallback = false;
    if (!gate.pass && !zeroNetworkRender) {
        const hasVisuals = candidates.some((c) => c.kind !== 'music' && c.url);
        const requiresRealVideo = plan.scenes.some((s) => s.visualPreference === 'video');
        if (!requiresRealVideo && !hasVisuals) {
            try {
                const bundled = await import('../media/bundled-media.js');
                const check = bundled.isOfflineModeAvailable ?? bundled.default?.isOfflineModeAvailable;
                if (check && check()) {
                    logInfo('⚠ No network visuals available — falling back to bundled offline assets');
                    const { createOfflinePlan } = await import('../media/offline-mode.js');
                    const offlinePlan = createOfflinePlan(req, finalScript);
                    offlineFallback = true;
                    logInfo(`📦 Offline plan ready: ${offlinePlan.scenes.length} scenes from bundled assets`);
                    
                    // Generate voiceovers for offline plan
                    let offlineVoiceovers: any = null;
                    try {
                        const { runVoiceStage } = await import('../media/voice-controller.js');
                        offlineVoiceovers = await runVoiceStage(offlinePlan, workspace, req.voice, () => {});
                    } catch {
                        offlineVoiceovers = await generateAgenticVoiceovers(offlinePlan, workspace, req.voice, undefined, req.personalAudio?.[0]);
                    }

                    // Sync scene durations to the REAL voiceover lengths. The offline
                    // plan hardcodes 5s/scene, but actual TTS narration usually runs
                    // longer; without this sync the render bakes ~N*5s of picture
                    // under a much longer audio track (observed: 20s of video under
                    // 75s of audio — container claimed 75s, stream ended at 20s).
                    try {
                        const vos: any[] = offlineVoiceovers?.voices ?? offlineVoiceovers?.scenes ?? [];
                        for (const v of vos) {
                            const scene: any = (offlinePlan.scenes as any[]).find((s) => s.sceneNumber === v.sceneIndex + 1);
                            if (scene && v.durationSec > 0) {
                                scene.durationSec = Math.ceil(v.durationSec);
                            }
                        }
                        (offlinePlan as any).totalDurationSec = (offlinePlan.scenes as any[]).reduce(
                            (acc, s) => acc + (s.durationSec || 0),
                            0,
                        );
                    } catch { /* keep static durations on any shape mismatch */ }
                    
                    // Build manifest for offline plan
                    const offlineManifest = {
                        assets: offlinePlan.scenes.map((s: any, i: number) => ({
                            sceneNumber: s.sceneNumber,
                            kind: s.visualPreference === 'video' ? 'video' : 'image',
                            // createOfflinePlan binds bundled media via `localAsset`,
                            // NOT `localPath` — reading only `localPath` handed the
                            // renderer undefined for every scene, which substituted
                            // navy placeholder clips (rendered near-black after
                            // grade+vignette → X10 black-frame failure).
                            localPath: s.localAsset ?? s.localPath,
                            durationSec: s.durationSec || 5,
                        })),
                        voiceoverDriven: offlineVoiceovers?.voiceoverDriven ?? false,
                    };
                    
                    // Write offline scene data
                    writeJson(workspace, 'scene-data.json', {
                        jobId: `offline_${Date.now()}`,
                        title: req.title,
                        backend,
                        voiceoverDriven: offlineVoiceovers?.voiceoverDriven ?? false,
                        scenes: offlinePlan.scenes.map((s: any) => ({
                            sceneNumber: s.sceneNumber,
                            voiceoverText: s.voiceoverText,
                            searchKeywords: s.searchKeywords,
                            visualPreference: s.visualPreference,
                            durationSec: s.durationSec || 5,
                            voiceover: offlineVoiceovers?.scenes?.[s.sceneNumber - 1]?.audioPath ?? null,
                            captionSegments: offlineVoiceovers?.scenes?.[s.sceneNumber - 1]?.captionSegments ?? [],
                        })),
                        decisions: [],
                        gate: [],
                        generatedAt: new Date().toString(),
                        offline: true,
                    });
                    
                    // Render with bundled assets
                    const { renderAgenticSlideshow } = await import('./render.js');
                    const renderOpts = {
                        preset: req.preset ?? 'cinematic',
                        sfx: req.sfx ?? false,
                        kinetic: req.kineticText !== false,
                        kenBurns: req.kenBurns !== false,
                        crossfadeSec: 0.5,
                        captions: req.captions ?? 'burned',
                        dimensions: req.orientation === 'landscape' ? { w: 1280, h: 720 } : req.orientation === 'square' ? { w: 1080, h: 1080 } : { w: 720, h: 1280 },
                        intro: req.intro,
                        outro: req.outro,
                    };
                    
                    const offlineResult = await renderAgenticSlideshow({
                        backend,
                        plan: offlinePlan,
                        workspace,
                        candidates: [],
                        decisions: [],
                        gate: { pass: true, checks: [] },
                        manifest: offlineManifest as any,
                        voiceovers: offlineVoiceovers,
                        fullyAgentDriven: false,
                    } as any, renderOpts);
                    
                    emit({
                        stage: 'gate',
                        percent: 100,
                        message: `OFFLINE RENDER → ${offlineResult}`,
                    });
                    
                    // Return offline result
                    const res: PipelineResult = {
                        backend,
                        plan: offlinePlan,
                        workspace,
                        candidates: [],
                        decisions: [],
                        gate: { pass: true, checks: [], offlineFallback: true } as any,
                        manifest: offlineManifest as any,
                        voiceovers: offlineVoiceovers,
                        fullyAgentDriven: false,
                    };
                    return res;
                }
            } catch {
                // offline module not available, skip fallback
            }
        }
    }

    if (gate.pass && manifest && voiceovers) {
        // TTS already ran before visual acquisition. This stage only binds the
        // measured narration tracks to the approved visuals and manifest.
        await alignVoiceoverTimeline(plan, voiceovers, req.platform, req.maxRuntimeSec, voiceWorkspace);

        const voByScene = new Map(voiceovers.scenes.map((s) => [s.sceneIndex, s]));
        for (const a of manifest.assets) {
            if (a.kind === 'music') continue;
            const scene = plan.scenes[a.sceneIndex];
            if (a.kind === 'video' && a.localPath && fs.existsSync(a.localPath)) {
                const vd = await estimateAudioDurationSafe(a.localPath);
                if (vd > 0) {
                    a.durationSec = vd;
                    scene.durationSec = vd;
                }
            }
            const pa = scene?.personalAudio ? inputVoiceoverPath(scene.personalAudio) : undefined;
            if (pa && fs.existsSync(pa)) {
                const dur = await estimateAudioDurationSafe(pa);
                a.audioPath = pa;
                a.durationSec = dur;
                scene.durationSec = dur;
                a.captionSegments = [{ text: scene.voiceoverText, startMs: 0, endMs: Math.round(dur * 1000) }];
                continue;
            }
            const v = voByScene.get(a.sceneIndex);
            if (v) {
                a.audioPath = v.audioPath;
                a.durationSec = v.durationSec;
                a.captionSegments = v.captionSegments;
                scene.durationSec = v.durationSec;
            }
        }
        // Recalculate total plan duration from updated per-scene durations
        plan.totalDurationSec = plan.scenes.reduce((acc, s) => acc + s.durationSec, 0);
        manifest.voiceoverDriven = voiceovers.voiceoverDriven;
        writeJson(workspace, 'render-manifest.json', manifest);
        writeJson(workspace, 'scene-data.json', {
            jobId,
            title: req.title,
            backend,
            voiceoverDriven: voiceovers.voiceoverDriven,
            scenes: plan.scenes.map((s) => ({
                sceneNumber: s.sceneNumber,
                voiceoverText: s.voiceoverText,
                searchKeywords: s.searchKeywords,
                visualPreference: s.visualPreference,
                durationSec: s.durationSec,
                voiceover: voByScene.get(s.sceneNumber - 1)?.audioPath ?? null,
                captionSegments: voByScene.get(s.sceneNumber - 1)?.captionSegments ?? [],
            })),
            decisions,
            gate: gate.checks,
            generatedAt: new Date().toISOString(),
        });
        updateJob(jobId, {
            gatePass: gate.pass,
            voiceoverDriven: voiceovers.voiceoverDriven,
            state: gate.pass ? 'awaiting_review' : 'failed',
        });
        persistJob(jobRec);
    }

    if (gate.pass && !voiceovers) {
        throw new Error('Narration-first pipeline produced no voiceover timeline');
    }

    const res: PipelineResult = {
        backend,
        plan,
        workspace,
        candidates,
        decisions,
        gate: { pass: gate.pass, checks: gate.checks },
        manifest: manifest!,
        voiceovers,
        fullyAgentDriven: backend === 'agent' && !cfg.visionVerify,
    };
    const contactSheet = await makeContactSheet(res);
    const decisionsReport = writeDecisionsReport(res);

    return res;
}
