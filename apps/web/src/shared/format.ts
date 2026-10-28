/** 显示层的格式化工具。纯函数，不依赖 React。 */

import dayjs from 'dayjs';

export function todayISO(): string {
  return dayjs().format('YYYY-MM-DD');
}

export function yesterdayISO(): string {
  return dayjs().subtract(1, 'day').format('YYYY-MM-DD');
}

/** 后端 date 已经是 "YYYY-MM-DD"；这里主要处理 null 与非法值。 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD') : value;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : value;
}

/** "3 天前" 这类相对描述，用于列表的次要信息。 */
export function formatFromNow(value: string | null | undefined): string {
  if (!value) return '';
  const parsed = dayjs(value);
  if (!parsed.isValid()) return value;
  const days = dayjs().startOf('day').diff(parsed.startOf('day'), 'day');
  if (days === 0) return '今天';
  if (days === 1) return '昨天';
  if (days === -1) return '明天';
  if (days > 1 && days <= 30) return `${days} 天前`;
  if (days < -1 && days >= -30) return `${-days} 天后`;
  return formatDate(value);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  EUR: '€',
  GBP: '£',
  JPY: '¥',
  HKD: 'HK$',
  MOP: 'MOP$',
  TWD: 'NT$',
  KRW: '₩',
  RUB: '₽',
};

/**
 * 面值。**后端用 Decimal → JSON 里是字符串，这里全程当字符串处理**，
 * 绝不做算术（浮点会把 0.10 变成 0.1，丢失录入精度）。
 */
export function formatFaceValue(
  faceValue: string | null | undefined,
  currency: string | null | undefined,
): string {
  if (!faceValue) return '';
  const symbol = currency ? CURRENCY_SYMBOLS[currency.toUpperCase()] : undefined;
  if (symbol) return `${symbol}${faceValue}`;
  return currency ? `${faceValue} ${currency}` : faceValue;
}

/** 「普30 · 保护森林 · ¥0.10」这样的单行摘要，用于邮票卡片标题。 */
export function composeStampTitle(parts: {
  name?: string | null;
  catalogRef?: string | null;
  faceValue?: string | null;
  currency?: string | null;
}): string {
  const chunks: string[] = [];
  if (parts.catalogRef) chunks.push(parts.catalogRef);
  if (parts.name) chunks.push(parts.name);
  const value = formatFaceValue(parts.faceValue, parts.currency);
  if (value) chunks.push(value);
  return chunks.length > 0 ? chunks.join(' · ') : '未命名邮票';
}

/** 截断长文本，保留末尾的可辨识度。 */
export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

export function pluralizeCount(count: number, unit: string): string {
  return `${count} ${unit}`;
}
