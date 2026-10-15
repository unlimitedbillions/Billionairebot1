import type { VoiceConfig } from './voice-types';

const DEFAULT_VOICE = 'en-US-GuyNeural';
const DEFAULT_RATE = '+0%';
const DEFAULT_PITCH = '+0Hz';

function normalizeRate(value: unknown): string {
    if (typeof value !== 'string') return DEFAULT_RATE;
    const match = /^([+-]?\\d{1,3})%$/.exec(value.trim());
    if (!match) return DEFAULT_RATE;
    const n = Number(match[1]);
    if (!Number.isInteger(n) || n < -100 || n > 100) return DEFAULT_RATE;
    return `${n >= 0 ? '+' : ''}${n}%`;
}

function normalizePitch(value: unknown): string {
    if (typeof value !== 'string') return DEFAULT_PITCH;
    const match = /^([+-]?\\d{1,4})Hz$/i.exec(value.trim());
    if (!match) return DEFAULT_PITCH;
    const n = Number(match[1]);
    if (!Number.isInteger(n) || n < -1000 || n > 1000) return DEFAULT_PITCH;
    return `${n >= 0 ? '+' : ''}${n}Hz`;
}

/** Reject malformed runtime prosody before it reaches the Edge-TTS CLI. */
export function normalizeVoiceConfig(config?: Partial<VoiceConfig> | null): VoiceConfig {
    const voice = typeof config?.voice === 'string' && config.voice.trim()
        ? config.voice.trim()
        : (process.env.VIDEO_VOICE?.trim() || DEFAULT_VOICE);
    return {
        voice,
        rate: normalizeRate(config?.rate),
        pitch: normalizePitch(config?.pitch),
        language: config?.language,
        requireRealSpeech: config?.requireRealSpeech,
    };
}
