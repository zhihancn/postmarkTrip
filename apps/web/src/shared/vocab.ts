/**
 * 词表。**所有下拉框的取值都来自 `/api/vocab`，前端不硬编码任何枚举。**
 *
 * 理由（docs/ok/03 §1.3）：戳种没有官方完整分类，后端会持续扩充取值。
 * 前端硬编码的后果是"后端加了新戳种，用户在界面上选不到"。
 *
 * 这一条包括：找不到 label 时**回落到原始 value**，而不是显示空白 —— 这样
 * 后端先上线新取值、前端还没更新时，界面依然可用。
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { api } from './api';
import type { VocabOption, VocabResponse } from './types';

export const VOCAB_QUERY_KEY = ['vocab'] as const;

export const EMPTY_VOCAB: VocabResponse = {
  postmark_types: [],
  primary_postmark_types: [],
  card_sources: [],
  card_statuses: [],
  catalog_systems: [],
  provenances: [],
  tag_categories: [],
  level_labels: [],
  face_value_suggestions: [],
};

/**
 * 词表是静态的（重启后端才会变），所以缓存到永久：
 * `staleTime/gcTime = Infinity` 让它只请求一次。
 */
export function useVocab(): UseQueryResult<VocabResponse> {
  return useQuery({
    queryKey: VOCAB_QUERY_KEY,
    queryFn: api.vocab,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  });
}

/** 拿词表，加载中/失败时给一份空表 —— 让调用方不必到处判空。 */
export function useVocabSafe(): { vocab: VocabResponse; loading: boolean } {
  const query = useVocab();
  return { vocab: query.data ?? EMPTY_VOCAB, loading: query.isLoading };
}

/** 取中文标签。找不到就返回原始 value（而不是空串）。 */
export function labelOf(
  options: readonly VocabOption[] | undefined,
  value: string | null | undefined,
): string {
  if (!value) return '';
  const hit = options?.find((option) => option.value === value);
  return hit ? hit.label : value;
}

/** 过滤掉不可选项，用于按 primary_postmark_types 排前的下拉框。 */
export function prioritizeOptions(
  options: readonly VocabOption[],
  preferred: readonly string[],
): VocabOption[] {
  if (preferred.length === 0) return [...options];
  const rank = new Map(preferred.map((value, index) => [value, index]));
  return [...options].sort((a, b) => {
    const ra = rank.get(a.value);
    const rb = rank.get(b.value);
    if (ra === undefined && rb === undefined) return 0;
    if (ra === undefined) return 1;
    if (rb === undefined) return -1;
    return ra - rb;
  });
}
