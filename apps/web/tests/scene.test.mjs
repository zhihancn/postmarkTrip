/**
 * `render/scene.ts`（布局计划）的测试 —— **零新依赖**，与 draft/recognize 同一个套路。
 *
 * 为什么这块值得测：mm→px 与 fit 的算术写错了**不会崩**，只会让图看起来
 * "差一点"（歪 1px、贴边 2px、相邻槽位一条缝）。而这正是"预览与导出不一致"
 * 那一类 bug 的原料。canvas 画出来什么样只能靠眼睛，但**布局可以钉死**。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const scene = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'render', 'scene.js')).href
);
const { mmToPx, slotBox, fitRect, planScene, assetRefs, missingSlots, assetUrlBySha } = scene;

const REF = (c) => 'sha256:' + c.repeat(64);

function sceneOf(slots, canvas = { width_mm: 54, height_mm: 90, dpi: 300 }) {
  return { schema_version: 1, canvas, slots };
}

test('mmToPx：54×90 mm @300 dpi = 638×1063', () => {
  assert.equal(mmToPx(54, 300), 638);
  assert.equal(mmToPx(90, 300), 1063);
  assert.equal(mmToPx(25.4, 300), 300);
  assert.equal(mmToPx(25.4, 600), 600);
});

test('相邻槽位不留缝：右边界与下一个左边界逐像素相同', () => {
  const a = { id: 'a', kind: 'image', x: 0, y: 0, w: 27, h: 90 };
  const b = { id: 'b', kind: 'image', x: 27, y: 0, w: 27, h: 90 };
  const ab = slotBox(a, 300);
  const bb = slotBox(b, 300);
  assert.equal(ab.left + ab.width, bb.left);
});

test('fitRect contain：等比放进框内并居中', () => {
  const r = fitRect({ width: 100, height: 100 }, { width: 200, height: 100 }, 'contain');
  assert.deepEqual(r, { width: 100, height: 100, dx: 50, dy: 0 });
});

test('fitRect cover：填满框（会溢出，由绘制层裁剪）', () => {
  const r = fitRect({ width: 100, height: 100 }, { width: 200, height: 100 }, 'cover');
  assert.deepEqual(r, { width: 200, height: 200, dx: 0, dy: -50 });
});

test('fitRect fill / none', () => {
  assert.deepEqual(
    fitRect({ width: 10, height: 20 }, { width: 100, height: 50 }, 'fill'),
    { width: 100, height: 50, dx: 0, dy: 0 },
  );
  assert.deepEqual(
    fitRect({ width: 10, height: 20 }, { width: 100, height: 50 }, 'none'),
    { width: 10, height: 20, dx: 45, dy: 15 },
  );
});

test('fitRect 遇到 0 尺寸不除零', () => {
  const r = fitRect({ width: 0, height: 0 }, { width: 100, height: 50 }, 'contain');
  assert.deepEqual(r, { width: 100, height: 50, dx: 0, dy: 0 });
});

test('planScene：画布尺寸、DPI、z 升序', () => {
  const plan = planScene(
    sceneOf([
      { id: 'top', kind: 'text', x: 0, y: 0, w: 10, h: 10, z: 20 },
      { id: 'bottom', kind: 'image', x: 0, y: 0, w: 10, h: 10, z: 5 },
    ]),
  );
  assert.equal(plan.width, 638);
  assert.equal(plan.height, 1063);
  assert.equal(plan.dpi, 300);
  assert.deepEqual(
    plan.ops.map((o) => o.slot.id),
    ['bottom', 'top'],
  );
});

test('planScene：显式 dpi 覆盖画布默认值', () => {
  const plan = planScene(sceneOf([], { width_mm: 54, height_mm: 90, dpi: 300 }), 600);
  assert.equal(plan.width, 1276);
  assert.equal(plan.height, 2126);
});

test('planScene：同 z 保持数组顺序（稳定）', () => {
  const plan = planScene(
    sceneOf([
      { id: 'a', kind: 'image', x: 0, y: 0, w: 1, h: 1 },
      { id: 'b', kind: 'image', x: 0, y: 0, w: 1, h: 1 },
      { id: 'c', kind: 'image', x: 0, y: 0, w: 1, h: 1 },
    ]),
  );
  assert.deepEqual(
    plan.ops.map((o) => o.slot.id),
    ['a', 'b', 'c'],
  );
});

test('assetRefs：背景 + 槽位去重', () => {
  const refs = assetRefs({
    schema_version: 1,
    canvas: { width_mm: 54, height_mm: 90 },
    background: { type: 'image', asset: REF('a') },
    slots: [
      { id: 'p', kind: 'postmark', x: 0, y: 0, w: 1, h: 1, asset: REF('b') },
      { id: 's', kind: 'stamp', x: 0, y: 0, w: 1, h: 1, asset: REF('b') },
      { id: 't', kind: 'text', x: 0, y: 0, w: 1, h: 1 },
    ],
  });
  assert.deepEqual(refs.sort(), [REF('a'), REF('b')].sort());
});

test('missingSlots：只数"该有图却没有"的槽位，文字槽不算', () => {
  const slots = [
    { id: 'p', kind: 'postmark', x: 0, y: 0, w: 1, h: 1 },
    { id: 's', kind: 'stamp', x: 0, y: 0, w: 1, h: 1, asset: REF('a') },
    { id: 't', kind: 'text', x: 0, y: 0, w: 1, h: 1 },
  ];
  assert.deepEqual(
    missingSlots(sceneOf(slots)).map((s) => s.id),
    ['p'],
  );
});

test('assetUrlBySha：接受带前缀与裸 sha', () => {
  const sha = 'c'.repeat(64);
  assert.equal(assetUrlBySha('sha256:' + sha), `/api/assets/by-sha256/${sha}/raw`);
  assert.equal(assetUrlBySha(sha), `/api/assets/by-sha256/${sha}/raw`);
});

test('exportFileName：非法字符与空白换成 -，空名回落到 card', async () => {
  const mod = await import(
    pathToFileURL(path.join(here, '..', '.test-build', 'render', 'export.js')).href
  );
  assert.equal(mod.exportFileName('2026-09-25-建溪1'), '2026-09-25-建溪1.png');
  assert.equal(mod.exportFileName('2026/09/25 建溪:1'), '2026-09-25-建溪-1.png');
  assert.equal(mod.exportFileName('  '), 'card.png');
  assert.equal(mod.exportFileName('a"b<c>d|e'), 'a-b-c-d-e.png');
});
