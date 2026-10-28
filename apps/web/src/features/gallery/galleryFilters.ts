/**
 * 展览集的筛选状态 ⇄ URL 查询串。**纯函数，不依赖 React**。
 *
 * 为什么单独一层：URL 是四个视图共用的"唯一状态源"（刷新/后退不丢、可分享）。
 * 解析与序列化写错的后果不是崩溃，而是"筛选看起来生效了、刷新后没了" ——
 * 这类问题只能靠单测钉住（`apps/web/tests/gallery.test.mjs`）。
 *
 * 默认值**不写进 URL**：地址栏只出现用户真正改过的项。
 * 其中 `provenance` 的默认值是 `real_scan`（只看真实跑戳，开关默认开），
 * 关掉时写 `provenance=all` —— 它不是后端的一个 provenance 取值，
 * 而是"不加 provenance 条件"。
 */

import type {
  GalleryMapParams,
  GalleryPostmarkGroupBy,
  GalleryPostmarkParams,
  GalleryPostmarkSort,
  GalleryStampGroupBy,
  GalleryStampParams,
  GalleryStampSeries,
  GalleryTimelineParams,
} from '../../shared/types';

export const GALLERY_VIEWS = ['timeline', 'postmarks', 'stamps', 'map'] as const;
export type GalleryView = (typeof GALLERY_VIEWS)[number];

export type GalleryProvenance = 'real_scan' | 'all';

export const GALLERY_GROUP_BYS: readonly GalleryPostmarkGroupBy[] = [
  'type',
  'color',
  'tag',
  'province',
  'none',
];

export const GALLERY_SORTS: readonly GalleryPostmarkSort[] = ['visit_desc', 'visit_asc'];

export const GALLERY_STAMP_GROUP_BYS: readonly GalleryStampGroupBy[] = [
  'series',
  'face',
  'year',
  'tag',
  'none',
];

export const GALLERY_STAMP_SERIES: readonly GalleryStampSeries[] = [
  'pu',
  'ji',
  'te',
  'yearly',
  'other',
  'uncatalogued',
];

export interface GalleryFilterState {
  /** '' = 全部年份。**用字符串而不是 number**，URL 里不必区分 0 与空。 */
  year: string;
  place_id: string;
  tag_id: string;
  source: string;
  /** `real_scan` = 只看真实跑戳（默认）；`all` = 不限 */
  provenance: GalleryProvenance;
  // ---- 戳墙选项（时间轴/票墙忽略；写进 URL 以便刷新/后退不丢）----
  /** 戳种（`postmark_type`）；'' = 全部 */
  postmark_type: string;
  /** 三态：'' = 不限、'true' = 只看彩色、'false' = 只看单色 */
  is_color: '' | 'true' | 'false';
  group_by: GalleryPostmarkGroupBy;
  sort: GalleryPostmarkSort;
  // ---- 票墙选项 ----
  stamp_series: GalleryStampSeries | '';
  stamp_group_by: GalleryStampGroupBy;
}

export const GALLERY_FILTER_DEFAULTS: GalleryFilterState = {
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
};

/** 与后端 `Query(ge=1800, le=2200)` 对齐；非法年份直接丢弃，不让它变成 422。 */
const YEAR_MIN = 1800;
const YEAR_MAX = 2200;

function cleanId(raw: string | null): string {
  const value = (raw ?? '').trim();
  return value === '' ? '' : value;
}

/**
 * 从查询串（可以是 `?a=1`、`a=1`，也可以是 URLSearchParams）解析筛选状态。
 *
 * **未知/非法取值一律回落到默认值**：用户手改 URL 不该让页面报错，
 * 而筛选本来就是"可丢弃"的输入。
 */
export function parseGalleryFilters(input: string | URLSearchParams): GalleryFilterState {
  const search = typeof input === 'string' ? new URLSearchParams(input) : input;

  const yearRaw = (search.get('year') ?? '').trim();
  const yearNumber = Number(yearRaw);
  const year =
    /^\d{4}$/.test(yearRaw) && yearNumber >= YEAR_MIN && yearNumber <= YEAR_MAX ? yearRaw : '';

  const provenance: GalleryProvenance =
    search.get('provenance') === 'all' ? 'all' : GALLERY_FILTER_DEFAULTS.provenance;

  const groupByRaw = search.get('group_by') ?? '';
  const group_by = (GALLERY_GROUP_BYS as readonly string[]).includes(groupByRaw)
    ? (groupByRaw as GalleryPostmarkGroupBy)
    : GALLERY_FILTER_DEFAULTS.group_by;

  const sortRaw = search.get('sort') ?? '';
  const sort = (GALLERY_SORTS as readonly string[]).includes(sortRaw)
    ? (sortRaw as GalleryPostmarkSort)
    : GALLERY_FILTER_DEFAULTS.sort;

  const isColorRaw = search.get('is_color');

  const stampGroupRaw = search.get('stamp_group_by') ?? '';
  const stamp_group_by = (GALLERY_STAMP_GROUP_BYS as readonly string[]).includes(stampGroupRaw)
    ? (stampGroupRaw as GalleryStampGroupBy)
    : GALLERY_FILTER_DEFAULTS.stamp_group_by;

  const stampSeriesRaw = search.get('stamp_series') ?? '';
  const stamp_series = (GALLERY_STAMP_SERIES as readonly string[]).includes(stampSeriesRaw)
    ? (stampSeriesRaw as GalleryStampSeries)
    : GALLERY_FILTER_DEFAULTS.stamp_series;

  return {
    year,
    place_id: cleanId(search.get('place_id')),
    tag_id: cleanId(search.get('tag_id')),
    source: cleanId(search.get('source')),
    provenance,
    postmark_type: cleanId(search.get('type')),
    is_color: isColorRaw === 'true' ? 'true' : isColorRaw === 'false' ? 'false' : '',
    group_by,
    sort,
    stamp_series,
    stamp_group_by,
  };
}

/**
 * 序列化成查询串（`''` 表示没有非默认值）。
 *
 * 顺序固定为 年 → 地点 → 标签 → 来源 → 来源标记，方便人读与测试比较。
 */
export function buildGallerySearch(state: GalleryFilterState): string {
  const params = new URLSearchParams();
  if (state.year !== GALLERY_FILTER_DEFAULTS.year) params.set('year', state.year);
  if (state.place_id !== GALLERY_FILTER_DEFAULTS.place_id) params.set('place_id', state.place_id);
  if (state.tag_id !== GALLERY_FILTER_DEFAULTS.tag_id) params.set('tag_id', state.tag_id);
  if (state.source !== GALLERY_FILTER_DEFAULTS.source) params.set('source', state.source);
  if (state.provenance !== GALLERY_FILTER_DEFAULTS.provenance) {
    params.set('provenance', state.provenance);
  }
  if (state.postmark_type !== GALLERY_FILTER_DEFAULTS.postmark_type) {
    params.set('type', state.postmark_type);
  }
  if (state.is_color !== GALLERY_FILTER_DEFAULTS.is_color) {
    params.set('is_color', state.is_color);
  }
  if (state.group_by !== GALLERY_FILTER_DEFAULTS.group_by) {
    params.set('group_by', state.group_by);
  }
  if (state.sort !== GALLERY_FILTER_DEFAULTS.sort) params.set('sort', state.sort);
  if (state.stamp_series !== GALLERY_FILTER_DEFAULTS.stamp_series) {
    params.set('stamp_series', state.stamp_series);
  }
  if (state.stamp_group_by !== GALLERY_FILTER_DEFAULTS.stamp_group_by) {
    params.set('stamp_group_by', state.stamp_group_by);
  }
  const query = params.toString();
  return query === '' ? '' : `?${query}`;
}

/** 某个 Tab 的地址：切 Tab 时**保留筛选条件**。 */
export function galleryPath(view: GalleryView, state: GalleryFilterState): string {
  return `/gallery/${view}${buildGallerySearch(state)}`;
}

/** 有几个筛选项不是默认值（用于"清除筛选（N）"）。 */
export function activeGalleryFilterCount(state: GalleryFilterState): number {
  let count = 0;
  if (state.year !== GALLERY_FILTER_DEFAULTS.year) count += 1;
  if (state.place_id !== GALLERY_FILTER_DEFAULTS.place_id) count += 1;
  if (state.tag_id !== GALLERY_FILTER_DEFAULTS.tag_id) count += 1;
  if (state.source !== GALLERY_FILTER_DEFAULTS.source) count += 1;
  if (state.provenance !== GALLERY_FILTER_DEFAULTS.provenance) count += 1;
  if (state.postmark_type !== GALLERY_FILTER_DEFAULTS.postmark_type) count += 1;
  if (state.is_color !== GALLERY_FILTER_DEFAULTS.is_color) count += 1;
  if (state.stamp_series !== GALLERY_FILTER_DEFAULTS.stamp_series) count += 1;
  return count;
}

/** 转成 `GET /api/gallery/timeline` 的查询参数（空串 → null，由 api 层丢掉）。 */
export function toTimelineParams(state: GalleryFilterState): GalleryTimelineParams {
  return {
    year: state.year === '' ? null : state.year,
    place_id: state.place_id === '' ? null : state.place_id,
    tag_id: state.tag_id === '' ? null : state.tag_id,
    source: state.source === '' ? null : state.source,
    provenance: state.provenance,
  };
}

/** 转成 `GET /api/gallery/postmarks` 的查询参数。 */
export function toPostmarkParams(state: GalleryFilterState): GalleryPostmarkParams {
  return {
    year: state.year === '' ? null : state.year,
    place_id: state.place_id === '' ? null : state.place_id,
    tag_id: state.tag_id === '' ? null : state.tag_id,
    type: state.postmark_type === '' ? null : state.postmark_type,
    is_color: state.is_color === '' ? null : state.is_color,
    provenance: state.provenance,
    group_by: state.group_by,
    sort: state.sort,
  };
}

/** 转成 `GET /api/gallery/stamps` 的查询参数。 */
export function toStampParams(state: GalleryFilterState): GalleryStampParams {
  return {
    year: state.year === '' ? null : state.year,
    place_id: state.place_id === '' ? null : state.place_id,
    tag_id: state.tag_id === '' ? null : state.tag_id,
    series: state.stamp_series === '' ? null : state.stamp_series,
    provenance: state.provenance,
    group_by: state.stamp_group_by,
  };
}

/**
 * 转成 `GET /api/gallery/map` 的查询参数。
 *
 * 与时间轴**同一组条件**（年份 / 地点 / 标签 / 来源）——地图是列表的
 * 可视化，不能自己另立口径。来源筛选条在地图视图不显示，但如果地址栏里
 * 还带着它（从时间轴切过来），就必须继续生效；否则图与列表会对不上。
 */
export function toMapParams(state: GalleryFilterState): GalleryMapParams {
  return {
    year: state.year === '' ? null : state.year,
    place_id: state.place_id === '' ? null : state.place_id,
    tag_id: state.tag_id === '' ? null : state.tag_id,
    source: state.source === '' ? null : state.source,
    provenance: state.provenance,
  };
}
