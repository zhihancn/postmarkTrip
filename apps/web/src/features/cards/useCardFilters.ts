import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';

import type { CardListParams } from '../../shared/api';

export type ViewMode = 'grid' | 'list';

export interface CardFilterState {
  q: string;
  visit_date_from: string;
  visit_date_to: string;
  place_id: string;
  place_descendants: boolean;
  post_office_id: string;
  postmark_type: string;
  source: string;
  status: string;
  /** 三态：'' = 不限、'true' = 只要彩色、'false' = 只要非彩色 */
  is_color: '' | 'true' | 'false';
  provenance: string;
  /** 注意：后端按**标签名**过滤，不是标签 id */
  tag: string;
  include_deleted: boolean;
  order_by: 'visit_date' | 'created_at' | 'title';
  order_desc: boolean;
  page: number;
  size: number;
  view: ViewMode;
}

const DEFAULTS: CardFilterState = {
  q: '',
  visit_date_from: '',
  visit_date_to: '',
  place_id: '',
  place_descendants: true,
  post_office_id: '',
  postmark_type: '',
  source: '',
  status: '',
  is_color: '',
  provenance: '',
  tag: '',
  include_deleted: false,
  order_by: 'visit_date',
  order_desc: true,
  page: 1,
  size: 24,
  view: 'grid',
};

const ORDER_FIELDS = ['visit_date', 'created_at', 'title'] as const;

function parseBoolean(raw: string | null, fallback: boolean): boolean {
  if (raw === null) return fallback;
  return raw === '1' || raw === 'true';
}

/**
 * 筛选条件**放在 URL 里**，而不是组件 state。
 *
 * 好处：刷新不丢、可以收藏/分享某个筛选视图、浏览器前进后退符合直觉、
 * 从详情页返回时筛选还在。代价是每次改都要 replace 一次 URL —— 用 replace
 * 避免把历史记录刷满。
 */
export function useCardFilters() {
  const [searchParams, setSearchParams] = useSearchParams();

  const state = useMemo<CardFilterState>(() => {
    const raw = (key: string, fallback: string) => searchParams.get(key) ?? fallback;

    const orderByRaw = raw('order_by', DEFAULTS.order_by);
    const order_by = (ORDER_FIELDS as readonly string[]).includes(orderByRaw)
      ? (orderByRaw as CardFilterState['order_by'])
      : DEFAULTS.order_by;

    const viewRaw = raw('view', DEFAULTS.view);
    const view: ViewMode = viewRaw === 'list' ? 'list' : 'grid';

    const isColorRaw = searchParams.get('is_color');
    const is_color: CardFilterState['is_color'] =
      isColorRaw === 'true' ? 'true' : isColorRaw === 'false' ? 'false' : '';

    const pageRaw = Number(searchParams.get('page') ?? DEFAULTS.page);
    const sizeRaw = Number(searchParams.get('size') ?? DEFAULTS.size);

    return {
      q: raw('q', DEFAULTS.q),
      visit_date_from: raw('visit_date_from', DEFAULTS.visit_date_from),
      visit_date_to: raw('visit_date_to', DEFAULTS.visit_date_to),
      place_id: raw('place_id', DEFAULTS.place_id),
      place_descendants: parseBoolean(searchParams.get('place_descendants'), DEFAULTS.place_descendants),
      post_office_id: raw('post_office_id', DEFAULTS.post_office_id),
      postmark_type: raw('postmark_type', DEFAULTS.postmark_type),
      source: raw('source', DEFAULTS.source),
      status: raw('status', DEFAULTS.status),
      is_color,
      provenance: raw('provenance', DEFAULTS.provenance),
      tag: raw('tag', DEFAULTS.tag),
      include_deleted: parseBoolean(searchParams.get('include_deleted'), DEFAULTS.include_deleted),
      order_by,
      order_desc: parseBoolean(searchParams.get('order_desc'), DEFAULTS.order_desc),
      page: Number.isFinite(pageRaw) && pageRaw >= 1 ? Math.floor(pageRaw) : DEFAULTS.page,
      size: Number.isFinite(sizeRaw) && sizeRaw >= 1 ? Math.floor(sizeRaw) : DEFAULTS.size,
      view,
    };
  }, [searchParams]);

  const patch = useCallback(
    (next: Partial<CardFilterState>) => {
      const merged: CardFilterState = { ...state, ...next };

      // 只把"非默认值"写进 URL，保持地址栏可读
      const params = new URLSearchParams();
      for (const key of Object.keys(DEFAULTS) as (keyof CardFilterState)[]) {
        const value = merged[key];
        const fallback = DEFAULTS[key];
        if (value === fallback) continue;
        if (typeof value === 'boolean') {
          params.set(key, value ? '1' : '0');
        } else {
          params.set(key, String(value));
        }
      }
      setSearchParams(params, { replace: true });
    },
    [state, setSearchParams],
  );

  const reset = useCallback(() => {
    // 保留视图偏好与每页条数 —— 重置筛选不该顺手改掉显示习惯
    patch({ ...DEFAULTS, view: state.view, size: state.size });
  }, [patch, state.view, state.size]);

  const activeCount = useMemo(() => {
    let count = 0;
    if (state.q !== '') count += 1;
    if (state.visit_date_from !== '' || state.visit_date_to !== '') count += 1;
    if (state.place_id !== '') count += 1;
    if (state.post_office_id !== '') count += 1;
    if (state.postmark_type !== '') count += 1;
    if (state.source !== '') count += 1;
    if (state.status !== '') count += 1;
    if (state.is_color !== '') count += 1;
    if (state.provenance !== '') count += 1;
    if (state.tag !== '') count += 1;
    return count;
  }, [state]);

  return { state, patch, reset, activeCount };
}

export function toListParams(state: CardFilterState, overrides: Partial<CardListParams> = {}): CardListParams {
  return {
    q: state.q.trim() === '' ? null : state.q.trim(),
    visit_date_from: state.visit_date_from === '' ? null : state.visit_date_from,
    visit_date_to: state.visit_date_to === '' ? null : state.visit_date_to,
    place_id: state.place_id === '' ? null : state.place_id,
    place_descendants: state.place_descendants,
    post_office_id: state.post_office_id === '' ? null : state.post_office_id,
    postmark_type: state.postmark_type === '' ? null : state.postmark_type,
    source: state.source === '' ? null : state.source,
    status: state.status === '' ? null : state.status,
    is_color: state.is_color === '' ? null : state.is_color === 'true',
    provenance: state.provenance === '' ? null : state.provenance,
    tag: state.tag === '' ? null : state.tag,
    include_deleted: state.include_deleted,
    order_by: state.order_by,
    order_desc: state.order_desc,
    page: state.page,
    size: state.size,
    ...overrides,
  };
}
