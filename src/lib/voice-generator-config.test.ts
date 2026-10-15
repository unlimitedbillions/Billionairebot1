import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeVoiceConfig } from './voice-generator.js';

describe('normalizeVoiceConfig', () => {
    it('defaults missing rate and pitch instead of forwarding undefined to Edge-TTS', () => {
        assert.deepEqual(normalizeVoiceConfig({ voice: 'en-US-DavisNeural' }), {
            voice: 'en-US-DavisNeural',
            rate: '+0%',
            pitch: '+0Hz',
        });
    });

    it('replaces malformed rate and pitch values with safe defaults', () => {
        assert.deepEqual(normalizeVoiceConfig({
            voice: 'en-US-DavisNeural',
            rate: 'undefined',
            pitch: 'undefined',
        }), {
            voice: 'en-US-DavisNeural',
            rate: '+0%',
            pitch: '+0Hz',
        });
    });

    it('preserves valid voice settings', () => {
        assert.deepEqual(normalizeVoiceConfig({
            voice: 'en-US-GuyNeural',
            rate: '-10%',
            pitch: '+5Hz',
        }), {
            voice: 'en-US-GuyNeural',
            rate: '-10%',
            pitch: '+5Hz',
        });
    });
});
