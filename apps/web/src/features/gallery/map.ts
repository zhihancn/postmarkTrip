/**
 * 地图（M3d，形态 B）的纯逻辑：分档、填充规则、统计文案、跳转链接。
 *
 * 这里**没有坐标**。地图是"省份清单 + 一张按卡数着色的 SVG"：
 * 着色键是后端返回的 `code2`（`admin_code` 前两位），与
 * `assets/china-provinces.svg` 的 `data-code` 一一对应。
 *
 * 为什么把分档单独写：同一批数字要同时喂给列表与 SVG（`04` §四 "同一查询"）。
 * 分档若写两遍，迟早出现"列表 3 张是深蓝、图上却是浅蓝"。
 */

import type { GalleryMap, GalleryMapProvince } from '../../shared/types';
// 带 `.js` 后缀：`test:pure` 把 TS 编到 `.test-build` 后由 node 直接跑 ESM，
// 没有打包器帮忙补扩展名（Vite 两种写法都认）。
import { galleryPath, type GalleryFilterState } from './galleryFilters.js';

/** 0 = 没去过（浅灰），1–4 = 由浅到深。 */
export const MAP_MAX_LEVEL = 4;

/**
 * 卡数 → 颜色档位（0–4）。
 *
 * 按**相对最高值**分四档，而不是固定阈值：只有 2 张卡时，"福建 2 张"
 * 应该是满色（人的直觉是"我全部的点亮在这"），不是最浅的一档。
 * 0 张恒为 0 档；最高值 ≤ 1 时，有卡就是满色。
 */
export function mapLevel(count: number, maxCount: number): number {
  if (count <= 0) return 0;
  if (maxCount <= 1) return MAP_MAX_LEVEL;

  const ratio = count / maxCount;
  if (ratio <= 0.25) return 1;
  if (ratio <= 0.5) return 2;
  if (ratio <= 0.75) return 3;
  return 4;
}

export function maxCardCount(provinces: readonly GalleryMapProvince[]): number {
  return provinces.reduce((max, province) => Math.max(max, province.card_count), 0);
}

/**
 * `code2 → 档位`。没有 `code2` 的省份不进表 —— SVG 里它保持默认浅灰，
 * 列表仍然显示它（不能因为它编码缺失就吞掉一张卡）。
 */
export function provinceLevels(
  provinces: readonly GalleryMapProvince[],
): Record<string, number> {
  const max = maxCardCount(provinces);
  const levels: Record<string, number> = {};
  for (const province of provinces) {
    if (province.code2) {
      levels[province.code2] = mapLevel(province.card_count, max);
    }
  }
  return levels;
}

/**
 * 生成注入 `<style>` 的规则。
 *
 * SVG 是静态资产（不手画、不带数据），颜色只能由这里按 `data-code` 覆盖；
 * 已知省份顺手加上 `cursor:pointer`（灰色省份点了没有目标，不该假装可点）。
 */
export function provinceFillRules(levels: Record<string, number>): string {
  return Object.entries(levels)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(
      ([code, level]) =>
        `.china-map [data-code="${code}"]{fill:var(--map-level-${level});cursor:pointer}`,
    )
    .join('\n');
}

export interface MapSummary {
  cards: number;
  provinces: number;
  cities: number;
  withoutPlace: number;
}

/**
 * 统计口径（与 `04` §四一致）：
 * * 卡数 = `total_cards`（与时间轴同一个筛选）；
 * * 省数 = 有卡的省级行数；
 * * 市数 = 各省 `city_count` 之和（不是县数）。
 */
export function mapSummary(
  map: Pick<GalleryMap, 'total_cards' | 'provinces' | 'without_place'>,
): MapSummary {
  return {
    cards: map.total_cards,
    provinces: map.provinces.length,
    cities: map.provinces.reduce((total, province) => total + province.city_count, 0),
    withoutPlace: map.without_place,
  };
}

/** "共 N 张卡 / M 个省 / K 个市"（底部统计行）。 */
export function mapSummaryText(
  map: Pick<GalleryMap, 'total_cards' | 'provinces' | 'without_place'>,
): string {
  const summary = mapSummary(map);
  return `共 ${summary.cards} 张卡 / ${summary.provinces} 个省 / ${summary.cities} 个市`;
}

/** 列表行的次要信息："2 张卡 · 2 个戳 · 1 个市"（市为 0 时不写）。 */
export function provinceDetail(province: GalleryMapProvince): string {
  const parts = [`${province.card_count} 张卡`, `${province.postmark_count} 个戳`];
  if (province.city_count > 0) parts.push(`${province.city_count} 个市`);
  return parts.join(' · ');
}

/** hover 提示：省名 / N 张卡 / N 个戳（`04` §六）。 */
export function provinceTooltip(province: GalleryMapProvince): string {
  return `${province.name} · ${province.card_count} 张卡 · ${province.postmark_count} 个戳`;
}

/** 没有卡的省份（SVG 里是浅灰）：只说名字，不编数字。 */
export function emptyProvinceTooltip(name: string): string {
  return `${name || '该省'} · 暂无卡片`;
}

/**
 * 点省份 → 时间轴，URL 带 `place_id`（M6）。
 *
 * 用当前筛选状态生成链接：地图上看到的是"筛选后的省"，
 * 跳过去必须还是同一批卡；否则用户会以为点错了。
 */
export function mapTimelineHref(
  placeId: string,
  state: GalleryFilterState,
): string {
  return galleryPath('timeline', { ...state, place_id: placeId });
}
