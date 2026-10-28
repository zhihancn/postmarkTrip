/**
 * 戳墙的纯逻辑：**跨页合并分组**、**瓦片提示文案**。
 *
 * 为什么单独一层：分组在服务端按页做，前端「加载更多」后必须把两页里
 * 同一个组（例如两页都有"日戳"）合并成一个组头；如果按页各渲染一次，
 * 墙上会出现两个"日戳"。这类问题不会报错，只会看起来像重复数据。
 */

import type {
  GalleryPostmarkGroup,
  GalleryPostmarkItem,
  GalleryPostmarkWall,
} from '../../shared/types';

/** 墙不平铺无限加载：到 600 条提示用筛选缩小范围（`02` §五）。 */
export const MAX_WALL_ITEMS = 600;

/**
 * 把多页的 `groups` 合并成一组：**保留首次出现的组顺序**，组内按页追加。
 *
 * `count` 重算为合并后的条数 —— 服务端给的是"本页这一组的数量"，
 * 直接相加会得到正确的总数，但用 `items.length` 更不容易错。
 */
export function mergeWallGroups(
  pages: readonly GalleryPostmarkWall[],
): GalleryPostmarkGroup[] {
  const order: string[] = [];
  const byKey = new Map<string, GalleryPostmarkGroup>();

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

  return order.map((key) => byKey.get(key) as GalleryPostmarkGroup);
}

/** 已渲染的瓦片数（用于 600 条上限提示）。 */
export function wallLoadedCount(groups: readonly GalleryPostmarkGroup[]): number {
  return groups.reduce((total, group) => total + group.items.length, 0);
}

/**
 * 悬浮提示的文案：戳面原文 · 日期 · 地点/局名。
 *
 * 文案里**只有"无戳图"**（技术性），绝不出现"缺票/补票" —— 戳墙不判断票
 * （`02` §一、验收 W7）。
 */
export function wallTileTitle(item: GalleryPostmarkItem): string {
  const place = [item.place_name, item.office_name].filter(Boolean).join(' · ');
  return [item.inscription, item.visit_date, place].filter(Boolean).join(' · ');
}
