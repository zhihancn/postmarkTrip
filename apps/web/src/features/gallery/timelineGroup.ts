/**
 * 时间轴的"年 → 月 → 日"分组。**纯函数，不依赖 React / dayjs**。
 *
 * 为什么单独一层：
 *
 * 1. 分组规则有边界（跨年、跨月、同一天多张卡、加载更多把同一个月切成两页），
 *    写在组件里就只能靠肉眼点界面验证 —— 而"少了一个月"看起来很像"数据本来就少"。
 * 2. "加载更多"是**追加**下一页。如果每页各自分组，同一个月的两段会被画成
 *    两个月份标题；所以必须先**摊平所有页、再一次性分组**（组件里照做）。
 *
 * 输入顺序不参与正确性：内部按 年/月/日 倒序排好，日期相同则保留服务端顺序
 * （服务端是 `visit_date DESC, created_at DESC`）。
 */

import type { GalleryTimelineItem } from '../../shared/types';

export interface TimelineDated {
  card_id: string;
  /** "YYYY-MM-DD" */
  visit_date: string;
}

export interface TimelineDay<T extends TimelineDated> {
  /** "2026-09-25" */
  date: string;
  /** "09-25" */
  label: string;
  items: T[];
}

export interface TimelineMonth<T extends TimelineDated> {
  /** 1–12 */
  month: number;
  /** "09 月" */
  label: string;
  days: TimelineDay<T>[];
}

export interface TimelineYear<T extends TimelineDated> {
  year: number;
  months: TimelineMonth<T>[];
}

export interface TimelineGroups<T extends TimelineDated> {
  years: TimelineYear<T>[];
  /** 日期无法解析、因而不在 `years` 里的记录数。**不静默丢弃**。 */
  skipped: number;
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})/;

interface DateParts {
  year: number;
  month: number;
  day: number;
}

function splitDate(value: string): DateParts | null {
  const match = DATE_RE.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return { year, month, day };
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

/**
 * 把（可能来自多页的）卡片流分组。
 *
 * 返回 `skipped` 而不是把坏日期塞进某一年：日期列在后端是 NOT NULL 的 `date`，
 * 真出现解析不了的值说明上游出了问题 —— 界面应该把它**报出来**，而不是画成
 * "1970 年"或者悄悄少一行。
 */
export function groupTimeline<T extends TimelineDated>(
  items: readonly T[],
): TimelineGroups<T> {
  const byYear = new Map<number, Map<number, Map<string, T[]>>>();
  let skipped = 0;

  for (const item of items) {
    const parts = splitDate(item.visit_date);
    if (parts === null) {
      skipped += 1;
      continue;
    }

    let months = byYear.get(parts.year);
    if (months === undefined) {
      months = new Map<number, Map<string, T[]>>();
      byYear.set(parts.year, months);
    }
    let days = months.get(parts.month);
    if (days === undefined) {
      days = new Map<string, T[]>();
      months.set(parts.month, days);
    }
    const date = `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}`;
    const bucket = days.get(date);
    if (bucket === undefined) {
      days.set(date, [item]);
    } else {
      bucket.push(item);
    }
  }

  const years: TimelineYear<T>[] = [...byYear.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([year, months]) => ({
      year,
      months: [...months.entries()]
        .sort((a, b) => b[0] - a[0])
        .map(([month, days]) => ({
          month,
          label: `${pad2(month)} 月`,
          days: [...days.entries()]
            .sort((a, b) => (a[0] < b[0] ? 1 : a[0] > b[0] ? -1 : 0))
            .map(([date, dayItems]) => ({
              date,
              label: date.slice(5),
              items: dayItems,
            })),
        })),
    }));

  return { years, skipped };
}

/** 行内要显示的"戳面原文 · 戳型"。空列表给一句人话，而不是留白。 */
export function summarizePostmarks(
  item: Pick<GalleryTimelineItem, 'inscriptions' | 'postmark_count'>,
): string {
  if (item.postmark_count === 0 || item.inscriptions.length === 0) return '未建戳';
  return item.inscriptions.join(' / ');
}
