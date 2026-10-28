import { useMemo, useRef, useState, type MouseEvent } from 'react';
import { Link, useNavigate, useOutletContext } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import chinaSvg from '../../assets/china-provinces.svg?raw';
import { api } from '../../shared/api';
import { Alert, ErrorState, LoadingState } from '../../shared/components/States';
import type { GalleryMapProvince } from '../../shared/types';
import { GalleryEmpty } from './GalleryEmpty';
import { toMapParams } from './galleryFilters';
import {
  emptyProvinceTooltip,
  mapSummaryText,
  mapTimelineHref,
  provinceDetail,
  provinceFillRules,
  provinceLevels,
  provinceTooltip,
} from './map';
import type { GalleryOutletContext } from './useGalleryFilters';

interface HoverState {
  code: string;
  name: string;
  x: number;
  y: number;
}

/**
 * 地图（M3d，形态 B）：左边省级清单，右边静态省份图。
 *
 * **这不是 Leaflet 地图**：没有瓦片、没有缩放拖动、没有打点，也不需要一个
 * 经纬度（`04` §三）。它回答的是"去过哪些省、各多少"，颜色与左侧数字同源。
 *
 * 交互只有三件事：
 * 1. hover 出省名 / 卡数 / 戳数；
 * 2. 点省份 → `/gallery/timeline?place_id=…`（M6）；
 * 3. 未填地点的卡单独一行、显式计数（M2）。
 */
export function MapView() {
  const { state, reset, activeCount } = useOutletContext<GalleryOutletContext>();
  const navigate = useNavigate();
  const params = useMemo(() => toMapParams(state), [state]);

  const query = useQuery({
    queryKey: ['gallery', 'map', params],
    queryFn: () => api.gallery.map(params),
  });

  const [hover, setHover] = useState<HoverState | null>(null);
  const figureRef = useRef<HTMLElement>(null);

  const data = query.data;
  const provinces = useMemo(() => data?.provinces ?? [], [data]);
  const byCode = useMemo(() => {
    const map = new Map<string, GalleryMapProvince>();
    for (const province of provinces) {
      if (province.code2) map.set(province.code2, province);
    }
    return map;
  }, [provinces]);
  const levels = useMemo(() => provinceLevels(provinces), [provinces]);
  const fillRules = useMemo(() => provinceFillRules(levels), [levels]);

  if (query.isLoading) return <LoadingState text="正在统计省份…" />;

  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  if (!data) return null;

  if (data.total_cards === 0) {
    return <GalleryEmpty filtered={activeCount > 0} onReset={reset} resource="卡片" />;
  }

  const summary = mapSummaryText(data);
  const noProvince = provinces.length === 0;

  function findPath(event: MouseEvent<HTMLDivElement>): Element | null {
    const target = event.target as Element;
    return target.closest('path[data-code]');
  }

  function handleMove(event: MouseEvent<HTMLDivElement>) {
    const path = findPath(event);
    if (path === null) {
      setHover(null);
      return;
    }
    const rect = figureRef.current?.getBoundingClientRect();
    if (!rect) return;
    const next: HoverState = {
      code: path.getAttribute('data-code') ?? '',
      name: path.getAttribute('data-name') ?? '',
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    };
    // mousemove 每秒几十次：同一省份内只挪 tooltip，不重建内容。
    setHover((previous) => {
      if (
        previous !== null &&
        previous.code === next.code &&
        previous.name === next.name &&
        Math.abs(previous.x - next.x) < 2 &&
        Math.abs(previous.y - next.y) < 2
      ) {
        return previous;
      }
      return next;
    });
  }

  function handleClick(event: MouseEvent<HTMLDivElement>) {
    const path = findPath(event);
    if (path === null) return;
    const province = byCode.get(path.getAttribute('data-code') ?? '');
    // 灰色省份没有 place_id 可跳（列表与图上数字同源，不编造一个 id）。
    if (province) navigate(mapTimelineHref(province.place_id, state));
  }

  const hovered = hover ? byCode.get(hover.code) : undefined;

  return (
    <div className="map-view-wrap">
      {noProvince ? (
        <Alert
          kind="info"
          title={activeCount > 0 ? '当前筛选下没有带地点的卡' : '还没有带地点的卡'}
        >
          地图按省级行政区聚合；{data.total_cards} 张卡都还没有可归属的邮戳地点。
          给邮戳补上地点后，这里才会点亮省份。卡片本身没有丢。
        </Alert>
      ) : null}

      <div className="map-view">
        <aside className="map-panel">
          <h2 className="map-panel__title">
            省份
            <span className="map-panel__hint">按卡数</span>
          </h2>

          {provinces.length > 0 ? (
            <ul className="map-provinces">
              {provinces.map((province) => (
                <li key={province.place_id}>
                  <Link
                    className="map-province"
                    to={mapTimelineHref(province.place_id, state)}
                    title={`${provinceTooltip(province)}；点击到时间轴`}
                  >
                    <span className="map-province__name">{province.name}</span>
                    <span className="map-province__count">{province.card_count} 张</span>
                    <span className="map-province__meta">{provinceDetail(province)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="small faint map-panel__empty">暂无可归属的省份。</p>
          )}

          {data.without_place > 0 ? (
            <Link className="map-without-place" to="/cards" title="去卡片列表补地点">
              <span>未填地点</span>
              <strong>{data.without_place} 张</strong>
              <span className="small">去卡片列表 →</span>
            </Link>
          ) : null}

          <div className="map-summary">{summary}</div>
        </aside>

        <section
          className="map-figure"
          ref={figureRef}
          onMouseMove={handleMove}
          onMouseLeave={() => setHover(null)}
          onClick={handleClick}
        >
          {fillRules ? <style>{fillRules}</style> : null}
          <div
            className="china-map"
            role="img"
            aria-label="中国省级行政区示意图，颜色深浅表示该省卡片数"
            dangerouslySetInnerHTML={{ __html: chinaSvg }}
          />

          <div className="map-legend" aria-hidden="true">
            <span>卡片数</span>
            <span className="map-legend__swatches">
              {[0, 1, 2, 3, 4].map((level) => (
                <i className={`map-legend__swatch map-legend__swatch--${level}`} key={level} />
              ))}
            </span>
            <span className="small faint">浅 → 深</span>
          </div>

          <p className="map-note small faint">
            省级示意图（Natural Earth，public domain）：颜色只表示该省卡片数，不涉及经纬度。
          </p>

          {hover ? (
            <div className="map-tooltip" style={{ left: hover.x, top: hover.y }} aria-hidden="true">
              {hovered ? provinceTooltip(hovered) : emptyProvinceTooltip(hover.name)}
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
