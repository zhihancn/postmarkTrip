/**
 * 地图（M3d）纯函数测试：分档、注入的填充规则、统计文案、跳转链接。
 *
 * 这些函数写错的后果都不是崩溃，而是"颜色看起来不对 / 文案数字对不上"——
 * 只能靠单测钉住。与 `gallery.test.mjs` 用同一套零依赖方式（tsc + node:test）。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const build = path.join(here, '..', '.test-build', 'features', 'gallery');

const map = await import(pathToFileURL(path.join(build, 'map.js')).href);
const filters = await import(pathToFileURL(path.join(build, 'galleryFilters.js')).href);

// --------------------------------------------------------------------------- //
//  夹具
// --------------------------------------------------------------------------- //

function province(over = {}) {
  return {
    place_id: 'place-1',
    name: '福建省',
    admin_code: '350000',
    code2: '35',
    card_count: 2,
    city_count: 1,
    postmark_count: 2,
    ...over,
  };
}

// --------------------------------------------------------------------------- //
//  分档
// --------------------------------------------------------------------------- //

test('mapLevel: 0 张永远是第 0 档（浅灰）', () => {
  assert.equal(map.mapLevel(0, 10), 0);
  assert.equal(map.mapLevel(0, 0), 0);
});

test('mapLevel: 最高值 ≤ 1 时，有卡就是满色', () => {
  assert.equal(map.mapLevel(1, 1), 4);
  assert.equal(map.mapLevel(1, 0), 4);
});

test('mapLevel: 按相对最高值分四档', () => {
  // 10 张为最高：2/10=0.2→1、5/10=0.5→2、7/10=0.7→3、8/10=0.8→4
  assert.equal(map.mapLevel(2, 10), 1);
  assert.equal(map.mapLevel(5, 10), 2);
  assert.equal(map.mapLevel(7, 10), 3);
  assert.equal(map.mapLevel(8, 10), 4);
  assert.equal(map.mapLevel(10, 10), 4);
});

test('mapLevel: 只有 2 张卡时，2 张是满色、1 张是中间档', () => {
  // 这是当前真库的形态（福建 2 张）——不能因为"最大就是 2"而画成最浅
  assert.equal(map.mapLevel(2, 2), 4);
  assert.equal(map.mapLevel(1, 2), 2);
});

test('provinceLevels: 用最高卡数归一，跳过没有 code2 的省', () => {
  const levels = map.provinceLevels([
    province({ place_id: 'a', name: '福建省', code2: '35', card_count: 2 }),
    province({ place_id: 'b', name: '江西省', code2: '36', card_count: 1 }),
    province({ place_id: 'c', name: '无名省', code2: null, card_count: 1 }),
  ]);
  assert.deepEqual(levels, { 35: 4, 36: 2 });
});

test('provinceLevels: 空列表 → 空对象', () => {
  assert.deepEqual(map.provinceLevels([]), {});
});

// --------------------------------------------------------------------------- //
//  填充规则
// --------------------------------------------------------------------------- //

test('provinceFillRules: 每个 code2 一条规则，按 code 排序、已知省份可点', () => {
  const rules = map.provinceFillRules({ 36: 2, 35: 4 });
  assert.equal(
    rules,
    [
      '.china-map [data-code="35"]{fill:var(--map-level-4);cursor:pointer}',
      '.china-map [data-code="36"]{fill:var(--map-level-2);cursor:pointer}',
    ].join('\n'),
  );
});

test('provinceFillRules: 没有已知省份时不生成任何规则（图全灰）', () => {
  assert.equal(map.provinceFillRules({}), '');
});

// --------------------------------------------------------------------------- //
//  统计与文案
// --------------------------------------------------------------------------- //

test('mapSummaryText: 卡数 / 省数 / 市数来自同一份响应', () => {
  const data = {
    total_cards: 3,
    without_place: 1,
    provinces: [
      province({ place_id: 'a', name: '福建省', card_count: 2, city_count: 1 }),
      province({ place_id: 'b', name: '江西省', card_count: 2, city_count: 2 }),
    ],
  };
  assert.equal(map.mapSummaryText(data), '共 3 张卡 / 2 个省 / 3 个市');
});

test('mapSummary: 市数是各省 city_count 之和，不含县', () => {
  const summary = map.mapSummary({
    total_cards: 1,
    without_place: 0,
    provinces: [
      province({ place_id: 'a', city_count: 0 }),
      province({ place_id: 'b', city_count: 2 }),
    ],
  });
  assert.deepEqual(summary, { cards: 1, provinces: 2, cities: 2, withoutPlace: 0 });
});

test('provinceDetail: 有市才写市；没有市不写"0 个市"', () => {
  assert.equal(
    map.provinceDetail(province({ card_count: 2, postmark_count: 3, city_count: 1 })),
    '2 张卡 · 3 个戳 · 1 个市',
  );
  assert.equal(
    map.provinceDetail(province({ card_count: 1, postmark_count: 1, city_count: 0 })),
    '1 张卡 · 1 个戳',
  );
});

test('provinceTooltip: 省名 / N 张卡 / N 个戳', () => {
  assert.equal(
    map.provinceTooltip(province({ name: '福建省', card_count: 2, postmark_count: 2 })),
    '福建省 · 2 张卡 · 2 个戳',
  );
});

test('emptyProvinceTooltip: 没卡的省只说名字，不编 0 张', () => {
  assert.equal(map.emptyProvinceTooltip('西藏自治区'), '西藏自治区 · 暂无卡片');
  assert.equal(map.emptyProvinceTooltip(''), '该省 · 暂无卡片');
});

// --------------------------------------------------------------------------- //
//  链接
// --------------------------------------------------------------------------- //

test('mapTimelineHref: 点省份跳时间轴，URL 带 place_id 且保留当前筛选', () => {
  const state = filters.parseGalleryFilters('?year=2026&provenance=all');
  assert.equal(
    map.mapTimelineHref('province-uuid', state),
    '/gallery/timeline?year=2026&place_id=province-uuid&provenance=all',
  );
});

test('mapTimelineHref: 已选地点时用点击的省份覆盖它', () => {
  const state = filters.parseGalleryFilters('?place_id=old');
  assert.equal(
    map.mapTimelineHref('new', state),
    '/gallery/timeline?place_id=new',
  );
});

// --------------------------------------------------------------------------- //
//  请求参数
// --------------------------------------------------------------------------- //

test('toMapParams: 与时间轴同一组条件，地址栏里残留的 source 也继续生效', () => {
  const state = filters.parseGalleryFilters(
    '?year=2026&place_id=place-1&tag_id=tag-1&provenance=all&source=field_trip',
  );
  assert.deepEqual(filters.toMapParams(state), {
    year: '2026',
    place_id: 'place-1',
    tag_id: 'tag-1',
    source: 'field_trip',
    provenance: 'all',
  });
});

test('toMapParams: 默认条件全部是 null + real_scan', () => {
  assert.deepEqual(filters.toMapParams(filters.parseGalleryFilters('')), {
    year: null,
    place_id: null,
    tag_id: null,
    source: null,
    provenance: 'real_scan',
  });
});
