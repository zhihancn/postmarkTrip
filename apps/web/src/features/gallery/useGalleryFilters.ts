import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';

import {
  activeGalleryFilterCount,
  buildGallerySearch,
  parseGalleryFilters,
  type GalleryFilterState,
} from './galleryFilters';

export interface GalleryOutletContext {
  state: GalleryFilterState;
  patch: (next: Partial<GalleryFilterState>) => void;
  reset: () => void;
  activeCount: number;
}

/**
 * 筛选条件放在 URL 里（不是组件 state）。
 *
 * 与 `useCardFilters` 同一条理由：刷新不丢、可分享、前进后退符合直觉。
 * 用 `replace` 避免每次改筛选都往历史里塞一条。
 *
 * 四个 Tab 共用同一份筛选状态 —— 切 Tab 时条件保留（`galleryPath`）。
 */
export function useGalleryFilters() {
  const [searchParams, setSearchParams] = useSearchParams();

  const state = useMemo<GalleryFilterState>(
    () => parseGalleryFilters(searchParams),
    [searchParams],
  );

  const patch = useCallback(
    (next: Partial<GalleryFilterState>) => {
      setSearchParams(buildGallerySearch({ ...state, ...next }), { replace: true });
    },
    [state, setSearchParams],
  );

  const reset = useCallback(() => {
    setSearchParams('', { replace: true });
  }, [setSearchParams]);

  const activeCount = useMemo(() => activeGalleryFilterCount(state), [state]);

  return { state, patch, reset, activeCount };
}
