import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, exportCardsCsvUrl, toApiError } from '../../shared/api';
import { useConfirm } from '../../shared/components/Confirm';
import { EmptyState } from '../../shared/components/EmptyState';
import { Pagination } from '../../shared/components/Pagination';
import { Alert, ErrorState, LoadingState } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import { useDebouncedValue } from '../../shared/hooks';
import { CardFilters } from './CardFilters';
import { CardGrid } from './CardGrid';
import { toListParams, useCardFilters } from './useCardFilters';

export function CardsPage() {
  const confirm = useConfirm();
  const toast = useToast();
  const queryClient = useQueryClient();

  const { state, patch, reset, activeCount } = useCardFilters();

  // 输入框立刻响应，但请求等用户停下来再发
  const debouncedKeyword = useDebouncedValue(state.q, 300);

  const params = useMemo(
    () =>
      toListParams(state, {
        q: debouncedKeyword.trim() === '' ? null : debouncedKeyword.trim(),
      }),
    [state, debouncedKeyword],
  );

  const cardsQuery = useQuery({
    queryKey: ['cards', params],
    queryFn: () => api.cards.list(params),
    placeholderData: keepPreviousData,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['cards'] });
    void queryClient.invalidateQueries({ queryKey: ['stats'] });
  };

  const restoreMutation = useMutation({
    mutationFn: (id: string) => api.cards.restore(id),
    onSuccess: () => {
      toast.success('已恢复到卡片列表');
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const purgeMutation = useMutation({
    mutationFn: (id: string) => api.cards.purge(id),
    onSuccess: () => {
      toast.success('已彻底删除（原始扫描件不会被删除）');
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const onPurge = async (id: string) => {
    const ok = await confirm({
      title: '彻底删除这张卡片？',
      danger: true,
      confirmText: '彻底删除',
      description: (
        <>
          <p>记录将被永久删除，**无法恢复**。</p>
          <p className="small muted" style={{ marginTop: 6 }}>
            原始扫描件与图片资产不会被删除 —— 它们是 L0 层证据，系统只进不出。
            也就是说图片仍占用磁盘，但不再有记录引用它。
          </p>
        </>
      ),
    });
    if (ok) purgeMutation.mutate(id);
  };

  const total = cardsQuery.data?.total ?? 0;
  const items = cardsQuery.data?.items ?? [];
  const hasFilters = activeCount > 0;
  const isTrash = state.include_deleted;

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <h1 className="page__title">{isTrash ? '回收站' : '邮戳卡'}</h1>
          <div className="page__subtitle">
            {isTrash
              ? '已删除的卡片。可以恢复，也可以彻底删除。'
              : '按跑戳日期记录每一张卡。点右上角新增，或直接扫描后上传。'}
          </div>
        </div>

        <div className="page__actions">
          <div className="segmented" role="group" aria-label="视图切换">
            <button
              type="button"
              className={`segmented__item${state.view === 'grid' ? ' segmented__item--active' : ''}`}
              onClick={() => patch({ view: 'grid' })}
            >
              网格
            </button>
            <button
              type="button"
              className={`segmented__item${state.view === 'list' ? ' segmented__item--active' : ''}`}
              onClick={() => patch({ view: 'list' })}
            >
              列表
            </button>
          </div>

          <button
            type="button"
            className="btn"
            onClick={() => {
              window.location.href = exportCardsCsvUrl(params);
            }}
            title="按当前筛选导出 CSV"
          >
            导出 CSV
          </button>

          {!isTrash ? (
            <Link to="/cards/new" className="btn btn--primary">
              ＋ 新增卡片
            </Link>
          ) : null}
        </div>
      </header>

      <CardFilters state={state} patch={patch} reset={reset} activeCount={activeCount} />

      {isTrash ? (
        <Alert kind="warn" title="你正在查看回收站">
          这里的卡片不会出现在列表、统计和导出中（回收站自身的导出除外）。
        </Alert>
      ) : null}

      {cardsQuery.isLoading ? <LoadingState text="正在加载卡片…" /> : null}

      {cardsQuery.isError ? (
        <ErrorState error={cardsQuery.error} onRetry={() => cardsQuery.refetch()} />
      ) : null}

      {cardsQuery.data ? (
        <>
          {items.length === 0 ? (
            isTrash ? (
              <EmptyState
                icon="🗑"
                title="回收站是空的"
                text="被删除的卡片会先放到这里，确认无误后再彻底删除。"
                action={
                  <button type="button" className="btn" onClick={() => patch({ include_deleted: false })}>
                    回到卡片列表
                  </button>
                }
              />
            ) : hasFilters ? (
              <EmptyState
                icon="🔍"
                title="没有符合条件的卡片"
                text="试试放宽筛选条件，或者清空关键词。"
                action={
                  <button type="button" className="btn" onClick={reset}>
                    重置筛选
                  </button>
                }
              />
            ) : (
              <EmptyState
                icon="📮"
                title="还没有记录。点右上角新增第一张邮戳卡。"
                text="如果已经有扫描件，也可以在卡片详情页把图片拖进来，先建记录再补字段。"
                action={
                  <Link to="/cards/new" className="btn btn--primary">
                    ＋ 新增卡片
                  </Link>
                }
              />
            )
          ) : (
            <CardGrid
              cards={items}
              view={state.view}
              deleted={isTrash}
              onRestore={(id) => restoreMutation.mutate(id)}
              onPurge={(id) => {
                void onPurge(id);
              }}
              busyId={
                restoreMutation.isPending
                  ? (restoreMutation.variables ?? null)
                  : purgeMutation.isPending
                    ? (purgeMutation.variables ?? null)
                    : null
              }
            />
          )}

          {total > 0 ? (
            <Pagination
              page={state.page}
              size={state.size}
              total={total}
              onPage={(page) => patch({ page })}
              onSize={(size) => patch({ size, page: 1 })}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
