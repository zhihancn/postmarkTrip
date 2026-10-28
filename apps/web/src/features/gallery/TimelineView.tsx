import { useMemo } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import { useInfiniteQuery } from '@tanstack/react-query';

import { api } from '../../shared/api';
import { Alert, ErrorState, LoadingState } from '../../shared/components/States';
import { Thumb } from '../../shared/components/Thumb';
import { labelOf, useVocabSafe } from '../../shared/vocab';
import type { GalleryTimelineItem } from '../../shared/types';
import { GalleryEmpty } from './GalleryEmpty';
import { toTimelineParams } from './galleryFilters';
import { groupTimeline, summarizePostmarks } from './timelineGroup';
import type { GalleryOutletContext } from './useGalleryFilters';

const PAGE_SIZE = 30;

function TimelineRow({ item }: { item: GalleryTimelineItem }) {
  const { vocab } = useVocabSafe();
  const place = item.place_path.length > 0 ? item.place_path.join(' / ') : '未标注地点';
  const office = item.office_names.length > 0 ? ` · ${item.office_names.join(' / ')}` : '';

  return (
    <Link className="timeline-row" to={`/cards/${item.card_id}`}>
      <div className="timeline-row__date">{item.visit_date.slice(5)}</div>

      <div className="timeline-row__main">
        <div className="timeline-row__headline">
          <span className="timeline-row__inscription">{summarizePostmarks(item)}</span>
          {item.postmark_types.map((type) => (
            <span className="badge" key={type}>
              {labelOf(vocab.postmark_types, type)}
            </span>
          ))}
          {item.has_scene ? (
            <span
              className="badge badge--primary"
              title="这张卡已有电子卡（点行进入卡片页查看）"
            >
              电子卡
            </span>
          ) : null}
        </div>

        {item.title ? <div className="timeline-row__title">{item.title}</div> : null}

        <div className="timeline-row__meta">
          {place}
          {office}
        </div>

        {item.tag_names.length > 0 ? (
          <div className="timeline-row__tags">
            {item.tag_names.map((name) => (
              <span className="badge badge--theme" key={name}>
                {name}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      <div className="timeline-row__thumb-wrap">
        <Thumb
          assetId={item.thumb_asset_id}
          alt={`${item.visit_date} 的卡片`}
          className="timeline-row__thumb"
          maxPx={320}
          placeholder="无图"
        />
      </div>
    </Link>
  );
}

/**
 * 时间轴（M3a）。
 *
 * 分页用 `useInfiniteQuery` + 「加载更多」：
 *
 * * 每页 30（`01` §六），不做虚拟滚动；
 * * **跨页同月能合并** —— 先把所有页摊平再交给 `groupTimeline`，
 *   而不是每页各分一次组（那会把同一个月画成两段）。
 */
export function TimelineView() {
  const { state, reset, activeCount } = useOutletContext<GalleryOutletContext>();
  const params = useMemo(() => toTimelineParams(state), [state]);

  const query = useInfiniteQuery({
    queryKey: ['gallery', 'timeline', params],
    queryFn: ({ pageParam }) => api.gallery.timeline({ ...params, page: pageParam, size: PAGE_SIZE }),
    initialPageParam: 1,
    getNextPageParam: (last) =>
      last.page * last.size < last.total ? last.page + 1 : undefined,
  });

  const pages = query.data?.pages ?? [];
  const items = useMemo(() => pages.flatMap((page) => page.items), [pages]);
  const grouped = useMemo(() => groupTimeline(items), [items]);

  const total = pages[0]?.total ?? 0;
  const withoutCut = pages[0]?.without_cut ?? 0;
  const withoutPostmark = pages[0]?.without_postmark ?? 0;

  if (query.isLoading) return <LoadingState text="正在整理时间轴…" />;

  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  if (total === 0) {
    return <GalleryEmpty filtered={activeCount > 0} onReset={reset} resource="卡片" />;
  }

  return (
    <div className="timeline">
      <div className="gallery-counts">
        <span>
          共 {total} 张卡 · 已显示 {items.length} 张
        </span>
        {withoutCut > 0 ? (
          <span className="badge badge--warn" title="有记录但没有卡片图，行内显示占位">
            {withoutCut} 张无卡片图
          </span>
        ) : null}
        {withoutPostmark > 0 ? (
          <span className="badge" title="这张卡还没有任何邮戳记录">
            {withoutPostmark} 张未建戳
          </span>
        ) : null}
      </div>

      {grouped.skipped > 0 ? (
        <Alert kind="warn" title="有记录的日期无法解析">
          {grouped.skipped} 条日期不在 `YYYY-MM-DD` 形状里，未显示在时间轴上。
          这不该发生（日期列是必填的），请检查这些记录。
        </Alert>
      ) : null}

      {grouped.years.map((year) => (
        <section className="timeline-year" key={year.year}>
          <h2 className="timeline-year__title">{year.year} 年</h2>

          {year.months.map((month) => (
            <div className="timeline-month" key={month.month}>
              <h3 className="timeline-month__title">{month.label}</h3>
              <div className="timeline-month__rows">
                {month.days.map((day) => (
                  <div className="timeline-day" key={day.date}>
                    {day.items.map((item) => (
                      <TimelineRow item={item} key={item.card_id} />
                    ))}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </section>
      ))}

      {query.hasNextPage ? (
        <div className="gallery-more">
          <button
            type="button"
            className="btn"
            onClick={() => void query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
          >
            {query.isFetchingNextPage ? '加载中…' : '加载更多'}
          </button>
          <span className="small faint">
            已显示 {items.length} / {total}
          </span>
        </div>
      ) : (
        <div className="gallery-more small faint">已显示全部 {total} 张</div>
      )}
    </div>
  );
}
