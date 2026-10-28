/**
 * 票墙的纯逻辑：跨页合并分组、宽高比、瓦片文案。
 *
 * 瀑布流用 CSS columns，图片没加载完时如果不知道宽高比，整墙会"跳版"。
 * 服务端返回贴片像素尺寸，这里换算成 `aspect-ratio` 先占位（`03` S6）。
 */

import type {
  GalleryStampGroup,
  GalleryStampItem,
  GalleryStampWall,
} from '../../shared/types';

export const MAX_STAMP_ITEMS = 600;

/** 把多页的 `groups` 合并：保留首次出现的组顺序，组内按页追加。 */
export function mergeStampGroups(
  pages: readonly GalleryStampWall[],
): GalleryStampGroup[] {
  const order: string[] = [];
  const byKey = new Map<string, GalleryStampGroup>();

  for (const page of pages) {
    for (const group of page.groups) {
      const existing = byKey.get(group.key);
      if (existing === undefined) {
        byKey.set(group.key, {
          key: group.key,
          label: group.label,
          count: group.items.length,
          items: [...group.items],
        });
        order.push(group.key);
      } else {
        existing.items.push(...group.items);
        existing.count = existing.items.length;
      }
    }
  }

  return order.map((key) => byKey.get(key) as GalleryStampGroup);
}

export function stampLoadedCount(groups: readonly GalleryStampGroup[]): number {
  return groups.reduce((total, group) => total + group.items.length, 0);
}

/** 没有尺寸时的保守比例；有尺寸就用真实比例，图片加载后不跳。 */
export function stampAspectRatio(item: GalleryStampItem): string {
  if (item.asset_width && item.asset_height && item.asset_height > 0) {
    return `${item.asset_width} / ${item.asset_height}`;
  }
  return '4 / 3';
}

/**
 * 悬浮提示：志号/名称 · 面值 · 跑戳日。
 * 只有票没有戳是**合法**的，所以这里不说任何"缺戳/异常"。
 */
export function stampTileTitle(item: GalleryStampItem): string {
  const title = [item.catalog_ref, item.name].filter(Boolean).join(' ');
  return [title || '未编目', item.face_label, item.visit_date]
    .filter(Boolean)
    .join(' · ');
}
