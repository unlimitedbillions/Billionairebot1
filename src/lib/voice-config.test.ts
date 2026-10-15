import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeVoiceConfig } from './voice-config.js';

test('voice config fills missing Edge-TTS prosody with valid neutral defaults', () => {
    const c = normalizeVoiceConfig({ voice: 'en-US-GuyNeural' });
    assert.equal(c.voice, 'en-US-GuyNeural');
    assert.equal(c.rate, '+0%');
    assert.equal(c.pitch, '+0Hz');
});

test('voice config rejects undefined, malformed and out-of-range prosody', () => {
    const c = normalizeVoiceConfig({ voice: ' en-US-AriaNeural ', rate: 'undefined', pitch: 'NaNHz' });
    assert.equal(c.voice, 'en-US-AriaNeural');
    assert.equal(c.rate, '+0%');
    assert.equal(c.pitch, '+0Hz');
    assert.equal(normalizeVoiceConfig({ rate: '+101%', pitch: '-1001Hz' }).rate, '+0%');
    assert.equal(normalizeVoiceConfig({ rate: '-15%', pitch: '+8Hz' }).rate, '-15%');
    assert.equal(normalizeVoiceConfig({ rate: '-15%', pitch: '+8Hz' }).pitch, '+8Hz');
});
