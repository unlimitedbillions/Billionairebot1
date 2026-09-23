    _voice?: string,
    onProgress?: (percent: number, message: string) => void,
    useClonedVoiceId?: string,
    /** Wave N/O — declared persona cast (from Plan.personas). When present,
     *  scenes may reference a persona by id for per-scene multi-voice. */
    personas?: import('../types.js').PersonaSpec[],
): Promise<VoiceRunResult> {
    const report = (p: number, m: string) => {
        onProgress?.(p, m);
        console.log(`[${p}%] ${m}`);
    };

    report(5, 'waking speech backend');

    // P1 — explicit fallback path. When the autopilot diagnoses a dead speech
    // backend it sets AGENTIC_VOICE_FALLBACK=1 so we skip the Python backend
    // entirely and drive all scenes through the engine-agnostic generator
    // (Edge-TTS / Kokoro / tones) — no external service required. This is what
    // lets an autonomous run still produce a real voiceover instead of retrying
    // blindly against an unavailable backend.
    if (process.env.AGENTIC_VOICE_FALLBACK === '1') {
        report(10, 'voice fallback mode (AGENTIC_VOICE_FALLBACK=1) — using built-in TTS engine');
        const audioDir = ws.audioDir;
        fs.mkdirSync(audioDir, { recursive: true });
        // The fallback path previously passed an empty config object. That
        // bypassed DEFAULT_VOICE_CONFIG and made the log show "Voice:
        // undefined", so the requested job voice was silently ignored.
        // Carry the resolved plan/global voice into the fallback and preserve
        // any per-scene voiceConfig overrides.
        const fallbackVoice = _voice || plan.voice || process.env.VIDEO_VOICE || 'en-US-GuyNeural';
        const scenes = plan.scenes.map((s, i) => ({
            sceneNumber: i + 1,
            voiceoverText: s.voiceoverText,
            voiceConfig: (s as any).voiceConfig,
        }));
        try {
            const map = await generateVoiceovers(
                scenes as any,
                audioDir,
                { voice: fallbackVoice, rate: '+0%', pitch: '+0Hz' } as any,
            );
            const voices: GeneratedVoice[] = [];
            let ok = 0;
            for (const [n, r] of map.entries()) {
                if (r.path && fs.existsSync(r.path)) {
                    voices.push({ sceneIndex: n - 1, audioPath: r.path, durationSec: (r as any).duration ?? 0 });
                    ok++;
                }
            }
            report(100, `voiceover generated via fallback engine (${ok}/${scenes.length})`);
            return { voices, voiceoverDriven: ok === scenes.length, profileId: 'fallback', fallbackUsed: true };
        } catch (e: any) {
            throw new Error(`voice fallback failed: ${e?.message ?? e}`);
        }
    }