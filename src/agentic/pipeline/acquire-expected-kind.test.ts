import assert from 'node:assert/strict';
import test from 'node:test';
import { expectedVisualKind } from './acquire.js';

test('expectedVisualKind respects an explicit local image even when narration prefers video', () => {
    assert.equal(expectedVisualKind({ localAsset: 'portrait.jpg', visualPreference: 'video' } as any), 'image');
    assert.equal(expectedVisualKind({ localAsset: 'portrait.webp', visualPreference: 'video' } as any), 'image');
});

test('expectedVisualKind respects an explicit local video', () => {
    assert.equal(expectedVisualKind({ localAsset: 'broll.mp4', visualPreference: 'image' } as any), 'video');
});

test('expectedVisualKind falls back to the scene visual preference without a local binding', () => {
    assert.equal(expectedVisualKind({ localAsset: undefined, visualPreference: 'video' } as any), 'video');
    assert.equal(expectedVisualKind({ localAsset: undefined, visualPreference: 'image' } as any), 'image');
});
