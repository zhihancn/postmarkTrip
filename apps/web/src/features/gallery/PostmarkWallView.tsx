import { useEffect, useMemo, useState } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import { api, assetThumbUrl } from '../../shared/api';
import { Field } from '../../shared/components/Field';
import { Select } from '../../shared/components/Select';
import { TAGS_QUERY_KEY } from '../../shared/components/TagPicker';
import { Alert, ErrorState, LoadingState } from '../../shared/components/States';
import { prioritizeOptions, useVocabSafe } from '../../shared/vocab';
import type {
  GalleryPostmarkGroupBy,
  GalleryPostmarkItem,
  GalleryPostmarkSort,
  GalleryPostmarkWall,
} from '../../shared/types';
import { GalleryEmpty } from './GalleryEmpty';
import { toPostmarkParams } from './galleryFilters';
import type { GalleryOutletContext } from './useGalleryFilters';
import {
  MAX_WALL_ITEMS,
  mergeWallGroups,
  wallLoadedCount,
  wallTileTitle,
} from './postmarkWall';

const PAGE_SIZE = 60;

const GROUP_OPTIONS: { value: GalleryPostmarkGroupBy; label: string }[] = [
  { value: 'type', label: '按戳型' },
  { value: 'color', label: '按颜色' },
  { value: 'tag', label: '按标签' },
  { value: 'province', label: '按省份' },
  { value: 'none', label: '不分组' },
];

const SORT_OPTIONS: { value: GalleryPostmarkSort; label: string }[] = [
  { value: 'visit_desc', label: '时间（新→旧）' },
  { value: 'visit_asc', label: '时间（旧→新）' },
];

function WallTile({ item }: { item: GalleryPostmarkItem }) {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [item.asset_id]);

  const showImage = item.asset_id !== null && !failed;
  const assetId = item.asset_id;

  return (
    <Link
      className="wall-tile"
      to={`/cards/${item.card_id}?postmark=${item.postmark_id}`}
      title={wallTileTitle(item)}
    >
      {showImage && assetId !== null ? (
        <img
          className="wall-tile__img"
          src={assetThumbUrl(assetId, 320, true)}
          alt={item.inscription}
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="wall-tile__empty">
          {item.asset_id === null ? '无戳图' : '图片缺失'}
        </div>
      )}

      {item.is_color ? <span className="wall-tile__dot" title="彩色戳" /> : null}

      <span className="wall-tile__caption">
        <strong>{item.inscription}</strong>
        <span>
          {item.visit_date} · {item.place_name ?? '未标注地点'}
        </span>
      </span>
    </Link>
  );
}

/**
 * 戳墙（M3b）。
 *
 * 一页 60 枚，服务端按 `group_by` 分组；「加载更多」后把同名的组合并
 * （`mergeWallGroups`），否则两页都有的"日戳"会被画成两个组头。
 *
 * **没有抠图**的戳仍然画出来（虚线 + "无戳图"）并显式计数；文案里没有
 * "缺票/补票" —— 戳墙不判断票（`02` §一、W7）。
 */
export function PostmarkWallView() {
  const { state, patch, reset, activeCount } = useOutletContext<GalleryOutletContext>();
  const { vocab } = useVocabSafe();
  const params = useMemo(() => toPostmarkParams(state), [state]);

  const tags = useQuery({
    queryKey: TAGS_QUERY_KEY,
    queryFn: () => api.tags.list(),
    staleTime: 60_000,
  });
  const hasTags = (tags.data ?? []).length > 0;

  const query = useInfiniteQuery({
    queryKey: ['gallery', 'postmarks', params],
    queryFn: ({ pageParam }) =>
      api.gallery.postmarks({ ...params, page: pageParam, size: PAGE_SIZE }),
    initialPageParam: 1,
    getNextPageParam: (last: GalleryPostmarkWall) => {
      const loaded = last.page * last.size;
      if (loaded >= MAX_WALL_ITEMS) return undefined;
      return loaded < last.total ? last.page + 1 : undefined;
    },
  });

  const pages = query.data?.pages ?? [];
  const groups = useMemo(() => mergeWallGroups(pages), [pages]);
  const loaded = wallLoadedCount(groups);

  const total = pages[0]?.total ?? 0;
  const withoutCut = pages[0]?.without_cut ?? 0;
  const withCut = pages[0]?.with_cut ?? 0;

  if (query.isLoading) return <LoadingState text="正在把戳排上墙…" />;

  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  const groupOptions = GROUP_OPTIONS.filter((option) => option.value !== 'tag' || hasTags);
  const firstItem = groups.flatMap((group) => group.items)[0];

  return (
    <div className="wall">
      <div className="toolbar wall-controls">
        <div className="toolbar__row">
          <Field label="分组">
            <Select
              value={state.group_by}
              onChange={(value) => patch({ group_by: value as GalleryPostmarkGroupBy })}
              options={groupOptions}
            />
          </Field>

          <Field label="戳种">
            <Select
              value={state.postmark_type}
              onChange={(value) => patch({ postmark_type: value })}
              options={prioritizeOptions(vocab.postmark_types, vocab.primary_postmark_types)}
              placeholder="全部戳种"
            />
          </Field>

          <Field label="颜色">
            <Select
              value={state.is_color}
              onChange={(value) =>
                patch({
                  is_color: value === 'true' ? 'true' : value === 'false' ? 'false' : '',
                })
              }
              options={[
                { value: 'true', label: '彩色戳' },
                { value: 'false', label: '单色戳' },
              ]}
              placeholder="不限"
            />
          </Field>

          <Field label="排序">
            <Select
              value={state.sort}
              onChange={(value) => patch({ sort: value as GalleryPostmarkSort })}
              options={SORT_OPTIONS}
            />
          </Field>
        </div>
      </div>

      {total === 0 ? (
        <GalleryEmpty filtered={activeCount > 0} onReset={reset} resource="戳" />
      ) : (
        <>
          <div className="gallery-counts">
            <span>
              共 {total} 枚戳 · 已显示 {loaded} 枚
            </span>
            {withoutCut > 0 ? (
              <span className="badge badge--warn" title="有戳记录但没有抠图（技术性）">
                {withoutCut} 枚无戳图
              </span>
            ) : null}
          </div>

          {withCut === 0 && firstItem ? (
            <Alert
              kind="warn"
              title={`${total} 枚戳都还没有抠图`}
              action={
                <Link
                  className="btn btn--sm"
                  to={`/cards/${firstItem.card_id}?postmark=${firstItem.postmark_id}`}
                >
                  去补第一枚
                </Link>
              }
            >
              确认时没有给圆心、或抠图失败。记录仍然在，只是墙上画虚线占位。
            </Alert>
          ) : null}

          {groups.map((group) => (
            <section className="wall-group" key={group.key}>
              <h2 className="wall-group__title">
                {group.label}
                <span className="wall-group__count">{group.count} 枚</span>
              </h2>
              <div className="wall-grid">
                {group.items.map((item) => (
                  <WallTile item={item} key={item.postmark_id} />
                ))}
              </div>
            </section>
          ))}

          {loaded >= MAX_WALL_ITEMS && total > loaded ? (
            <Alert kind="warn" title="已经排到 600 枚">
              墙不再往下加载了（防止浏览器内存被吃掉）。用上面的筛选缩小范围后再看。
            </Alert>
          ) : null}

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
                已显示 {loaded} / {total}
              </span>
            </div>
          ) : (
            <div className="gallery-more small faint">已显示全部 {total} 枚</div>
          )}
        </>
      )}
    </div>
  );
}
