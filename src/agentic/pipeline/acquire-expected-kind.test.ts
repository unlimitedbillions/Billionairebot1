import assert from 'node:assert/strict';
import test from 'node:test';
import { expectedVisualKind } from './acquire.js';

test('expectedVisualKind respects explicit local images even when narration prefers video', () => {
    for (const localAsset of ['portrait.jpg', 'portrait.webp', 'portrait.png', 'portrait.gif']) {
        assert.equal(expectedVisualKind({ localAsset, visualPreference: 'video' } as any), 'image');
    }
});

test('expectedVisualKind respects explicit local video extensions', () => {
    for (const localAsset of ['broll.mp4', 'legacy.avi', 'archive.mkv', 'clip.m2ts']) {
        assert.equal(expectedVisualKind({ localAsset, visualPreference: 'image' } as any), 'video');
    }
});

test('expectedVisualKind uses scene preference without an explicit local binding', () => {
    assert.equal(expectedVisualKind({ localAsset: undefined, visualPreference: 'video' } as any), 'video');
    assert.equal(expectedVisualKind({ localAsset: undefined, visualPreference: 'image' } as any), 'image');
});
