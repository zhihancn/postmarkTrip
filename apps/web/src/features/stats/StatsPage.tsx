import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../shared/api';
import { EmptyState } from '../../shared/components/EmptyState';
import { Field } from '../../shared/components/Field';
import { Select } from '../../shared/components/Select';
import { ErrorState, LoadingState } from '../../shared/components/States';
import { formatDate } from '../../shared/format';
import { useVocabSafe } from '../../shared/vocab';
import type { CountBucket } from '../../shared/types';

/** 用 div + CSS 画的横向条形图。刻意不引图表库 —— 只需要这一种图。 */
function Bars({ buckets, emptyText }: { buckets: CountBucket[]; emptyText: string }) {
  if (buckets.length === 0) {
    return <div className="small faint">{emptyText}</div>;
  }

  const max = Math.max(...buckets.map((item) => item.count), 1);

  return (
    <div className="bars">
      {buckets.map((bucket) => (
        <div className="bar" key={bucket.key}>
          <div className="bar__label" title={bucket.label ?? bucket.key}>
            {bucket.label ?? bucket.key}
          </div>
          <div className="bar__track">
            <div
              className="bar__fill"
              style={{ width: `${Math.max(2, (bucket.count / max) * 100)}%` }}
            />
          </div>
          <div className="bar__value">{bucket.count}</div>
        </div>
      ))}
    </div>
  );
}

export function StatsPage() {
  const { vocab } = useVocabSafe();
  const [placeLevel, setPlaceLevel] = useState(1);

  const overview = useQuery({
    queryKey: ['stats', 'overview'],
    queryFn: api.stats.overview,
  });

  const byYear = useQuery({
    queryKey: ['stats', 'by-year'],
    queryFn: api.stats.byYear,
  });

  const byPlace = useQuery({
    queryKey: ['stats', 'by-place', placeLevel],
    queryFn: () => api.stats.byPlace(placeLevel),
  });

  if (overview.isLoading) return <LoadingState text="正在统计…" />;
  if (overview.isError) {
    return <ErrorState error={overview.error} onRetry={() => overview.refetch()} />;
  }

  const data = overview.data;
  if (!data) return <LoadingState />;

  if (data.cards === 0) {
    return (
      <div className="page">
        <header className="page__header">
          <div className="page__heading">
            <h1 className="page__title">统计</h1>
          </div>
        </header>
        <EmptyState
          icon="📊"
          title="还没有数据可以统计"
          text="先录入几张邮戳卡，这里会显示你去过多少邮局、跑过哪些地方、每年跑了多少。"
          action={
            <Link to="/cards/new" className="btn btn--primary">
              ＋ 新增第一张卡片
            </Link>
          }
        />
      </div>
    );
  }

  const placeLevelOptions = vocab.level_labels.map((label, index) => ({
    value: String(index),
    label,
  }));

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <h1 className="page__title">统计</h1>
          <div className="page__subtitle">
            跑戳的成就感来源之一。
            {data.first_visit_date ? (
              <>
                {' '}
                记录覆盖 {formatDate(data.first_visit_date)} ～ {formatDate(data.last_visit_date)}。
              </>
            ) : null}
          </div>
        </div>
      </header>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-card__value">{data.cards}</div>
          <div className="stat-card__label">邮戳卡</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__value">{data.postmarks}</div>
          <div className="stat-card__label">邮戳</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__value">{data.stamps}</div>
          <div className="stat-card__label">邮票</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__value">{data.post_offices}</div>
          <div className="stat-card__label">去过的邮局</div>
        </div>
      </div>

      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">按层级统计地点</div>
          <div className="small muted">
            层级名而不是"第 N 级" —— 直辖市的中间层是空的，所以按名字筛才准
          </div>
        </div>
        <div className="panel__body">
          <div className="col" style={{ gap: 12 }}>
            {Object.keys(data.places_by_level).length > 0 ? (
              <div className="row row--wrap" style={{ gap: 8 }}>
                {Object.entries(data.places_by_level).map(([label, count]) => (
                  <span key={label} className="badge badge--primary">
                    {label} {count}
                  </span>
                ))}
              </div>
            ) : (
              <div className="small faint">还没有关联到行政区划的记录。</div>
            )}
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">按年份分布</div>
        </div>
        <div className="panel__body">
          {byYear.isLoading ? (
            <div className="small muted">加载中…</div>
          ) : byYear.isError ? (
            <ErrorState error={byYear.error} onRetry={() => byYear.refetch()} />
          ) : (
            <Bars buckets={byYear.data?.buckets ?? []} emptyText="还没有可统计的年份。" />
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">按地域分布</div>
          <div className="right" style={{ minWidth: 160 }}>
            <Field label="层级">
              <Select
                value={String(placeLevel)}
                onChange={(value) => setPlaceLevel(Number(value))}
                options={placeLevelOptions}
              />
            </Field>
          </div>
        </div>
        <div className="panel__body">
          {byPlace.isLoading ? (
            <div className="small muted">加载中…</div>
          ) : byPlace.isError ? (
            <ErrorState error={byPlace.error} onRetry={() => byPlace.refetch()} />
          ) : (
            <>
              {byPlace.data?.level_label ? (
                <div className="small muted" style={{ marginBottom: 8 }}>
                  当前层级：{byPlace.data.level_label}
                </div>
              ) : null}
              <Bars
                buckets={byPlace.data?.buckets ?? []}
                emptyText="这个层级还没有数据。换个层级试试。"
              />
            </>
          )}
        </div>
      </section>
    </div>
  );
}
