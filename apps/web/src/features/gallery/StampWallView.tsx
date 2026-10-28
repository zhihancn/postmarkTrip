import { useEffect, useMemo, useState } from 'react';
import { Link, useOutletContext } from 'react-router-dom';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import { api, assetThumbUrl } from '../../shared/api';
import { EmptyState } from '../../shared/components/EmptyState';
import { Field } from '../../shared/components/Field';
import { Select } from '../../shared/components/Select';
import { TAGS_QUERY_KEY } from '../../shared/components/TagPicker';
import { Alert, ErrorState, LoadingState } from '../../shared/components/States';
import type {
  GalleryStampGroupBy,
  GalleryStampItem,
  GalleryStampSeries,
  GalleryStampWall,
} from '../../shared/types';
import { GalleryEmpty } from './GalleryEmpty';
import { toStampParams } from './galleryFilters';
import {
  MAX_STAMP_ITEMS,
  mergeStampGroups,
  stampAspectRatio,
  stampLoadedCount,
  stampTileTitle,
} from './stampWall';
import type { GalleryOutletContext } from './useGalleryFilters';

const PAGE_SIZE = 60;

const GROUP_OPTIONS: { value: GalleryStampGroupBy; label: string }[] = [
  { value: 'series', label: '按志号系列' },
  { value: 'face', label: '按面值' },
  { value: 'year', label: '按发行年' },
  { value: 'tag', label: '按标签' },
  { value: 'none', label: '不分组' },
];

const SERIES_OPTIONS: { value: GalleryStampSeries; label: string }[] = [
  { value: 'pu', label: '普' },
  { value: 'ji', label: '纪' },
  { value: 'te', label: '特' },
  { value: 'yearly', label: '编年' },
  { value: 'other', label: '其他' },
  { value: 'uncatalogued', label: '未编目' },
];

function StampTile({ item }: { item: GalleryStampItem }) {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [item.asset_id]);

  const assetId = item.asset_id;
  const showImage = assetId !== null && !failed;

  return (
    <Link
      className="stamp-tile"
      to={`/cards/${item.card_id}?stamp=${item.stamp_id}`}
      title={stampTileTitle(item)}
    >
      <div className="stamp-tile__frame" style={{ aspectRatio: stampAspectRatio(item) }}>
        {showImage && assetId !== null ? (
          <img
            className="stamp-tile__img"
            src={assetThumbUrl(assetId, 320, true)}
            alt={item.name ?? item.catalog_ref ?? '邮票'}
            loading="lazy"
            decoding="async"
            onError={() => setFailed(true)}
          />
        ) : (
          <div className="stamp-tile__empty">
            {assetId === null ? '无票图' : '图片缺失'}
          </div>
        )}

        {item.catalog_ref ? <span className="stamp-tile__ref">{item.catalog_ref}</span> : null}
        {item.face_value ? <span className="stamp-tile__face">{item.face_label}</span> : null}

        <span className="stamp-tile__caption">
          <strong>{item.name ?? item.catalog_ref ?? '未编目'}</strong>
          <span>
            {item.face_label} · {item.visit_date}
          </span>
        </span>
      </div>
    </Link>
  );
}

/**
 * 票墙（M3c）。
 *
 * 这块的核心不是瀑布流，是**空态**：确认建卡时不建 `stamp` 行，只有"框票"
 * 才会建。所以第一屏很可能是空的 —— 必须说清原因，并给出"去哪框票"。
 *
 * `cards_without_stamp`（票未录）与 `stamps_without_cut`（没有贴片图）
 * 是两件事，文案里不混用（`00` §3.8、`03` §二）。
 */
export function StampWallView() {
  const { state, patch, reset, activeCount } = useOutletContext<GalleryOutletContext>();
  const params = useMemo(() => toStampParams(state), [state]);

  const tags = useQuery({
    queryKey: TAGS_QUERY_KEY,
    queryFn: () => api.tags.list(),
    staleTime: 60_000,
  });
  const hasTags = (tags.data ?? []).length > 0;

  const query = useInfiniteQuery({
    queryKey: ['gallery', 'stamps', params],
    queryFn: ({ pageParam }) => api.gallery.stamps({ ...params, page: pageParam, size: PAGE_SIZE }),
    initialPageParam: 1,
    getNextPageParam: (last: GalleryStampWall) => {
      const loaded = last.page * last.size;
      if (loaded >= MAX_STAMP_ITEMS) return undefined;
      return loaded < last.total ? last.page + 1 : undefined;
    },
  });

  const pages = query.data?.pages ?? [];
  const groups = useMemo(() => mergeStampGroups(pages), [pages]);
  const loaded = stampLoadedCount(groups);

  const total = pages[0]?.total ?? 0;
  const withoutCut = pages[0]?.stamps_without_cut ?? 0;
  const withCut = pages[0]?.with_cut ?? 0;
  const cardsWithoutStamp = pages[0]?.cards_without_stamp ?? 0;
  const firstMissing = pages[0]?.first_card_without_stamp ?? null;

  if (query.isLoading) return <LoadingState text="正在把票排上墙…" />;

  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  const groupOptions = GROUP_OPTIONS.filter((option) => option.value !== 'tag' || hasTags);

  if (total === 0) {
    if (activeCount > 0) {
      return <GalleryEmpty filtered onReset={reset} resource="票" />;
    }

    if (cardsWithoutStamp > 0) {
      return (
        <EmptyState
          icon="🖼"
          title="票墙是空的"
          text={
            <>
              确认建卡时<strong>不会</strong>自动建票记录 —— 只有「框票」才会创建票。
              现在有 <strong>{cardsWithoutStamp}</strong> 张卡有戳记录、但还没有票记录（票未录）。
            </>
          }
          action={
            <div className="row" style={{ gap: 8 }}>
              {firstMissing ? (
                <Link className="btn btn--primary" to={`/cards/${firstMissing}#ecard`}>
                  去最近一张卡框票
                </Link>
              ) : null}
              <Link className="btn" to="/cards">
                看看卡片列表
              </Link>
            </div>
          }
        />
      );
    }

    return (
      <EmptyState
        icon="🖼"
        title="还没有邮票记录"
        text="先录入卡片；在卡片详情页的「电子卡」面板里框一次邮票，票墙才有内容。"
        action={
          <Link className="btn btn--primary" to="/cards">
            去卡片列表
          </Link>
        }
      />
    );
  }

  return (
    <div className="stamp-wall">
      <div className="toolbar stamp-controls">
        <div className="toolbar__row">
          <Field label="分组">
            <Select
              value={state.stamp_group_by}
              onChange={(value) => patch({ stamp_group_by: value as GalleryStampGroupBy })}
              options={groupOptions}
            />
          </Field>

          <Field label="系列">
            <Select
              value={state.stamp_series}
              onChange={(value) => patch({ stamp_series: value as GalleryStampSeries | '' })}
              options={SERIES_OPTIONS}
              placeholder="全部系列"
            />
          </Field>
        </div>
      </div>

      <div className="gallery-counts">
        <span>
          共 {total} 枚票 · 已显示 {loaded} 枚
        </span>
        {withoutCut > 0 ? (
          <span className="badge badge--warn" title="有票记录但没有贴片图（技术性）">
            {withoutCut} 枚无票图
          </span>
        ) : null}
      </div>

      {cardsWithoutStamp > 0 ? (
        <Alert
          kind="info"
          title={`还有 ${cardsWithoutStamp} 张卡没有票记录（票未录）`}
          action={
            firstMissing ? (
              <Link className="btn btn--sm" to={`/cards/${firstMissing}#ecard`}>
                去最近一张卡框票
              </Link>
            ) : null
          }
        >
          这是"有戳记录、没有票记录"的数据缺口，不是缺贴片；框一次票即可补上。
        </Alert>
      ) : null}

      {withCut === 0 ? (
        <Alert kind="warn" title={`${total} 枚票都还没有框出图`}>
          记录仍然在，只是墙上画虚线占位。去卡片详情页的「电子卡」面板框一次票即可。
        </Alert>
      ) : null}

      {groups.map((group) => (
        <section className="stamp-group" key={group.key}>
          <h2 className="stamp-group__title">
            {group.label}
            <span className="stamp-group__count">{group.count} 枚</span>
            {group.key === 'uncatalogued' ? (
              <Link className="small" to="/dicts?tab=catalog" style={{ marginLeft: 'auto' }}>
                去标准库补录 →
              </Link>
            ) : null}
          </h2>
          <div className="stamp-columns">
            {group.items.map((item) => (
              <StampTile item={item} key={item.stamp_id} />
            ))}
          </div>
        </section>
      ))}

      {loaded >= MAX_STAMP_ITEMS && total > loaded ? (
        <Alert kind="warn" title="已经排到 600 枚">
          墙不再往下加载了。用上面的筛选缩小范围后再看。
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
    </div>
  );
}
