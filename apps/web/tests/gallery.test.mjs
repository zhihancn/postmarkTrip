/**
 * 展览集的纯函数测试：URL 筛选 ⇄ 状态、时间轴年月日分组。
 *
 * 方式与 `draft.test.mjs` 相同：零新依赖（仓库里已有的 tsc 编译 + node:test）。
 *
 * **测的是哪一类问题**：
 *
 * * URL 解析/序列化写错不会崩，只会"筛选看起来生效、刷新后没了"；
 * * 分组写错不会崩，只会"某个月不见了 / 同一个月出现两次" ——
 *   而这两种表现都很容易被当成"数据本来就少"。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));

const filters = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'features', 'gallery', 'galleryFilters.js'))
    .href
);
const timeline = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'features', 'gallery', 'timelineGroup.js'))
    .href
);
const wall = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'features', 'gallery', 'postmarkWall.js'))
    .href
);
const stampWall = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'features', 'gallery', 'stampWall.js'))
    .href
);

// --------------------------------------------------------------------------- //
//  夹具
// --------------------------------------------------------------------------- //

function item(cardId, visitDate) {
  return {
    card_id: cardId,
    visit_date: visitDate,
    title: null,
    thumb_asset_id: null,
    place_path: [],
    office_names: [],
    postmark_count: 0,
    inscriptions: [],
    postmark_types: [],
    tag_names: [],
    has_scene: false,
    source: 'field_trip',
  };
}

// --------------------------------------------------------------------------- //
//  筛选：解析
// --------------------------------------------------------------------------- //

test('parseGalleryFilters: 空串 → 默认值（只看真实跑戳、按戳型/系列分组）', () => {
  const state = filters.parseGalleryFilters('');
  assert.deepEqual(state, {
    year: '',
    place_id: '',
    tag_id: '',
    source: '',
    provenance: 'real_scan',
    postmark_type: '',
    is_color: '',
    group_by: 'type',
    sort: 'visit_desc',
    stamp_series: '',
    stamp_group_by: 'series',
  });
});

test('parseGalleryFilters: 读取全部筛选，并接受带 ? 的查询串', () => {
  const state = filters.parseGalleryFilters(
    '?year=2026&place_id=pl-1&tag_id=t-1&source=mail_request&provenance=all&type=daily&is_color=true&group_by=province&sort=visit_asc&stamp_series=pu&stamp_group_by=face',
  );
  assert.equal(state.year, '2026');
  assert.equal(state.place_id, 'pl-1');
  assert.equal(state.tag_id, 't-1');
  assert.equal(state.source, 'mail_request');
  assert.equal(state.provenance, 'all');
  assert.equal(state.postmark_type, 'daily');
  assert.equal(state.is_color, 'true');
  assert.equal(state.group_by, 'province');
  assert.equal(state.sort, 'visit_asc');
  assert.equal(state.stamp_series, 'pu');
  assert.equal(state.stamp_group_by, 'face');
});

test('parseGalleryFilters: 非法年份被丢弃（否则会变成后端 422）', () => {
  for (const bad of ['26', '20266', 'abcd', '  ', '999', '2201']) {
    assert.equal(filters.parseGalleryFilters(`?year=${bad}`).year, '', `year=${bad}`);
  }
  assert.equal(filters.parseGalleryFilters('?year=1800').year, '1800');
  assert.equal(filters.parseGalleryFilters('?year=2200').year, '2200');
});

test('parseGalleryFilters: 未知 provenance 回落到默认值，不把页面搞崩', () => {
  assert.equal(filters.parseGalleryFilters('?provenance=generated').provenance, 'real_scan');
});

test('parseGalleryFilters: 空白 id 视为未选', () => {
  const state = filters.parseGalleryFilters('?place_id=%20&tag_id=');
  assert.equal(state.place_id, '');
  assert.equal(state.tag_id, '');
});

// --------------------------------------------------------------------------- //
//  筛选：序列化
// --------------------------------------------------------------------------- //

test('buildGallerySearch: 默认值不写进 URL', () => {
  assert.equal(filters.buildGallerySearch(filters.GALLERY_FILTER_DEFAULTS), '');
});

test('buildGallerySearch: 只写非默认值，且关掉开关写 provenance=all', () => {
  const state = { ...filters.GALLERY_FILTER_DEFAULTS, year: '2026', provenance: 'all' };
  assert.equal(filters.buildGallerySearch(state), '?year=2026&provenance=all');
});

test('buildGallerySearch: 戳墙选项也写进 URL（刷新/后退不丢）', () => {
  const state = {
    ...filters.GALLERY_FILTER_DEFAULTS,
    postmark_type: 'daily',
    is_color: 'false',
    group_by: 'province',
    sort: 'visit_asc',
  };
  assert.equal(
    filters.buildGallerySearch(state),
    '?type=daily&is_color=false&group_by=province&sort=visit_asc',
  );
});

test('buildGallerySearch: 票墙选项也写进 URL', () => {
  const state = {
    ...filters.GALLERY_FILTER_DEFAULTS,
    stamp_series: 'pu',
    stamp_group_by: 'face',
  };
  assert.equal(filters.buildGallerySearch(state), '?stamp_series=pu&stamp_group_by=face');
});

test('URL 往返：解析 → 序列化 → 再解析，状态不变', () => {
  const state = {
    year: '2025',
    place_id: 'pl-9',
    tag_id: 't-9',
    source: 'exchange',
    provenance: 'all',
    postmark_type: 'scenic',
    is_color: 'true',
    group_by: 'tag',
    sort: 'visit_asc',
    stamp_series: 'yearly',
    stamp_group_by: 'year',
  };
  const search = filters.buildGallerySearch(state);
  assert.deepEqual(filters.parseGalleryFilters(search), state);
});

test('galleryPath: 切 Tab 时保留筛选条件', () => {
  const state = { ...filters.GALLERY_FILTER_DEFAULTS, year: '2026' };
  assert.equal(filters.galleryPath('timeline', state), '/gallery/timeline?year=2026');
  assert.equal(filters.galleryPath('stamps', state), '/gallery/stamps?year=2026');
  assert.equal(filters.galleryPath('map', filters.GALLERY_FILTER_DEFAULTS), '/gallery/map');
});

test('activeGalleryFilterCount: 开关关掉也算一个生效的筛选', () => {
  assert.equal(filters.activeGalleryFilterCount(filters.GALLERY_FILTER_DEFAULTS), 0);
  assert.equal(
    filters.activeGalleryFilterCount({ ...filters.GALLERY_FILTER_DEFAULTS, provenance: 'all' }),
    1,
  );
  assert.equal(
    filters.activeGalleryFilterCount({
      ...filters.GALLERY_FILTER_DEFAULTS,
      year: '2026',
      place_id: 'pl-1',
    }),
    2,
  );
  // 戳墙自己的筛选也算生效项 —— 否则「清除筛选」会显示成不可点
  assert.equal(
    filters.activeGalleryFilterCount({
      ...filters.GALLERY_FILTER_DEFAULTS,
      postmark_type: 'daily',
      is_color: 'true',
    }),
    2,
  );
});

test('toTimelineParams: 空串转 null，provenance 永远显式带上', () => {
  assert.deepEqual(filters.toTimelineParams(filters.GALLERY_FILTER_DEFAULTS), {
    year: null,
    place_id: null,
    tag_id: null,
    source: null,
    provenance: 'real_scan',
  });
  assert.deepEqual(
    filters.toTimelineParams({
      year: '2026',
      place_id: 'pl-1',
      tag_id: '',
      source: 'field_trip',
      provenance: 'all',
    }),
    { year: '2026', place_id: 'pl-1', tag_id: null, source: 'field_trip', provenance: 'all' },
  );
});

test('toPostmarkParams: 戳墙的筛选项与分组都显式带上', () => {
  assert.deepEqual(filters.toPostmarkParams(filters.GALLERY_FILTER_DEFAULTS), {
    year: null,
    place_id: null,
    tag_id: null,
    type: null,
    is_color: null,
    provenance: 'real_scan',
    group_by: 'type',
    sort: 'visit_desc',
  });
  assert.deepEqual(
    filters.toPostmarkParams({
      ...filters.GALLERY_FILTER_DEFAULTS,
      year: '2026',
      postmark_type: 'scenic',
      is_color: 'false',
      group_by: 'province',
      sort: 'visit_asc',
      provenance: 'all',
    }),
    {
      year: '2026',
      place_id: null,
      tag_id: null,
      type: 'scenic',
      is_color: 'false',
      provenance: 'all',
      group_by: 'province',
      sort: 'visit_asc',
    },
  );
});

test('toStampParams: 票墙的系列与分组都显式带上', () => {
  assert.deepEqual(filters.toStampParams(filters.GALLERY_FILTER_DEFAULTS), {
    year: null,
    place_id: null,
    tag_id: null,
    series: null,
    provenance: 'real_scan',
    group_by: 'series',
  });
  assert.deepEqual(
    filters.toStampParams({
      ...filters.GALLERY_FILTER_DEFAULTS,
      year: '2026',
      stamp_series: 'pu',
      stamp_group_by: 'face',
      provenance: 'all',
    }),
    {
      year: '2026',
      place_id: null,
      tag_id: null,
      series: 'pu',
      provenance: 'all',
      group_by: 'face',
    },
  );
});

// --------------------------------------------------------------------------- //
//  分组
// --------------------------------------------------------------------------- //

test('groupTimeline: 空输入给空结果', () => {
  assert.deepEqual(timeline.groupTimeline([]), { years: [], skipped: 0 });
});

test('groupTimeline: 按 年 → 月 → 日 倒序，跨年边界不漏', () => {
  const groups = timeline.groupTimeline([
    item('c-2026', '2026-01-01'),
    item('c-2025', '2025-12-31'),
    item('c-2024', '2024-02-29'),
  ]).years;

  assert.deepEqual(
    groups.map((y) => y.year),
    [2026, 2025, 2024],
  );
  assert.equal(groups[0].months[0].month, 1);
  assert.equal(groups[1].months[0].month, 12);
  assert.equal(groups[2].months[0].days[0].date, '2024-02-29');
});

test('groupTimeline: ★ 加载更多时同一个月跨页也能合并成一个月', () => {
  // 第 1 页最后一张是 09-30，第 2 页第一张是 09-29 —— 都属于 2026-09。
  const page1 = [item('a', '2026-10-01'), item('b', '2026-09-30')];
  const page2 = [item('c', '2026-09-29'), item('d', '2026-09-01')];
  const groups = timeline.groupTimeline([...page1, ...page2]).years;

  assert.equal(groups.length, 1);
  assert.equal(groups[0].year, 2026);
  assert.equal(groups[0].months.length, 2, '10 月 + 9 月');
  const september = groups[0].months.find((m) => m.month === 9);
  assert.equal(september.days.length, 3);
  assert.deepEqual(
    september.days.map((d) => d.date),
    ['2026-09-30', '2026-09-29', '2026-09-01'],
  );
});

test('groupTimeline: 同一天多张卡不合并，保留输入顺序', () => {
  const groups = timeline.groupTimeline([
    item('first', '2026-09-25'),
    item('second', '2026-09-25'),
  ]).years;
  const day = groups[0].months[0].days[0];
  assert.deepEqual(
    day.items.map((i) => i.card_id),
    ['first', 'second'],
  );
});

test('groupTimeline: 输入顺序被打乱也按日期倒序排', () => {
  const groups = timeline.groupTimeline([
    item('old', '2024-03-02'),
    item('new', '2026-09-25'),
    item('mid', '2025-06-01'),
  ]).years;
  assert.deepEqual(
    groups.map((y) => y.year),
    [2026, 2025, 2024],
  );
});

test('groupTimeline: 坏日期计入 skipped，不静默丢弃', () => {
  const result = timeline.groupTimeline([
    item('ok', '2026-09-25'),
    item('bad-1', 'not-a-date'),
    item('bad-2', '2026-13-01'),
  ]);
  assert.equal(result.skipped, 2);
  assert.equal(result.years.length, 1);
  assert.equal(result.years[0].months[0].days[0].items.length, 1);
});

test('groupTimeline: 日期相同时不因多页重复创建月份/日节点', () => {
  const result = timeline.groupTimeline([
    item('a', '2026-09-25'),
    item('b', '2026-09-25'),
    item('c', '2026-09-25'),
  ]);
  assert.equal(result.years[0].months.length, 1);
  assert.equal(result.years[0].months[0].days.length, 1);
});

test('summarizePostmarks: 没有戳时说人话，而不是留白', () => {
  assert.equal(timeline.summarizePostmarks({ inscriptions: [], postmark_count: 0 }), '未建戳');
  assert.equal(
    timeline.summarizePostmarks({ inscriptions: ['建溪 1', '三清山'], postmark_count: 2 }),
    '建溪 1 / 三清山',
  );
});

// --------------------------------------------------------------------------- //
//  戳墙：跨页合并 + 瓦片文案
// --------------------------------------------------------------------------- //

function wallItem(over = {}) {
  return {
    postmark_id: 'pm-1',
    card_id: 'c-1',
    asset_id: null,
    inscription: '建溪',
    visit_date: '2026-09-25',
    place_name: '福建省 / 南平市 / 延平区',
    office_name: '建溪邮政支局',
    is_color: false,
    color_layers: null,
    tag_names: [],
    postmark_type: 'daily',
    ...over,
  };
}

function wallGroup(key, label, items) {
  return { key, label, count: items.length, items };
}

test('mergeWallGroups: 两页里的同名组合并成一个组头（不是两个"日戳"）', () => {
  const page1 = {
    total: 3,
    with_cut: 3,
    without_cut: 0,
    groups: [wallGroup('daily', '日戳', [wallItem({ postmark_id: 'a' })])],
    page: 1,
    size: 2,
  };
  const page2 = {
    total: 3,
    with_cut: 3,
    without_cut: 0,
    groups: [
      wallGroup('daily', '日戳', [wallItem({ postmark_id: 'b' })]),
      wallGroup('scenic', '风景戳', [wallItem({ postmark_id: 'c' })]),
    ],
    page: 2,
    size: 2,
  };

  const merged = wall.mergeWallGroups([page1, page2]);

  assert.deepEqual(
    merged.map((g) => g.key),
    ['daily', 'scenic'],
  );
  assert.equal(merged[0].count, 2);
  assert.deepEqual(
    merged[0].items.map((i) => i.postmark_id),
    ['a', 'b'],
  );
  assert.equal(wall.wallLoadedCount(merged), 3);
});

test('mergeWallGroups: 空页给空结果', () => {
  assert.deepEqual(wall.mergeWallGroups([]), []);
  assert.equal(wall.wallLoadedCount([]), 0);
});

test('wallTileTitle: 原文 + 日期 + 地点/局名', () => {
  assert.equal(
    wall.wallTileTitle(wallItem()),
    '建溪 · 2026-09-25 · 福建省 / 南平市 / 延平区 · 建溪邮政支局',
  );
  assert.equal(
    wall.wallTileTitle(wallItem({ place_name: null, office_name: null })),
    '建溪 · 2026-09-25',
  );
});

test('★ W7：戳墙的文案里不出现"缺票/补票"（那是票墙的事）', () => {
  for (const item of [wallItem(), wallItem({ asset_id: null, place_name: null, office_name: null })]) {
    assert.doesNotMatch(wall.wallTileTitle(item), /缺票|补票/);
  }
});

// --------------------------------------------------------------------------- //
//  票墙：跨页合并、宽高比、文案
// --------------------------------------------------------------------------- //

function stampItem(over = {}) {
  return {
    stamp_id: 's-1',
    card_id: 'c-1',
    asset_id: null,
    catalog_ref: '普30',
    catalog_system: 'china',
    name: '保护森林',
    face_value: '0.40',
    currency: 'CNY',
    face_label: '40分',
    issue_date: '2002-02-01',
    visit_date: '2026-09-25',
    tag_names: [],
    asset_width: 986,
    asset_height: 692,
    ...over,
  };
}

function stampGroup(key, label, items) {
  return { key, label, count: items.length, items };
}

test('mergeStampGroups: 两页里的同名组合并成一个组头', () => {
  const page1 = {
    total: 3,
    with_cut: 3,
    stamps_without_cut: 0,
    cards_without_stamp: 0,
    first_card_without_stamp: null,
    groups: [stampGroup('pu', '普', [stampItem({ stamp_id: 'a' })])],
    page: 1,
    size: 2,
  };
  const page2 = {
    total: 3,
    with_cut: 3,
    stamps_without_cut: 0,
    cards_without_stamp: 0,
    first_card_without_stamp: null,
    groups: [
      stampGroup('pu', '普', [stampItem({ stamp_id: 'b' })]),
      stampGroup('yearly', '编年', [stampItem({ stamp_id: 'c' })]),
    ],
    page: 2,
    size: 2,
  };

  const merged = stampWall.mergeStampGroups([page1, page2]);
  assert.deepEqual(
    merged.map((g) => g.key),
    ['pu', 'yearly'],
  );
  assert.equal(merged[0].count, 2);
  assert.equal(stampWall.stampLoadedCount(merged), 3);
});

test('stampAspectRatio: 用贴片真实尺寸，缺尺寸时给保守比例（不跳版）', () => {
  assert.equal(stampWall.stampAspectRatio(stampItem()), '986 / 692');
  assert.equal(
    stampWall.stampAspectRatio(stampItem({ asset_width: null, asset_height: null })),
    '4 / 3',
  );
});

test('stampTileTitle: 志号/名称 + 面值 + 跑戳日；没有"缺戳"提示（只有票是合法的）', () => {
  const title = stampWall.stampTileTitle(stampItem());
  assert.equal(title, '普30 保护森林 · 40分 · 2026-09-25');
  assert.doesNotMatch(title, /缺戳|异常/);
  assert.match(
    stampWall.stampTileTitle(stampItem({ catalog_ref: null, name: null })),
    /^未编目 · 40分/,
  );
});
