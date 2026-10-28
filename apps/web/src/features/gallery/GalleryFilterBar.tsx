import { useQuery } from '@tanstack/react-query';

import { api } from '../../shared/api';
import { Field } from '../../shared/components/Field';
import { PlacePicker } from '../../shared/components/PlacePicker';
import { Select } from '../../shared/components/Select';
import { TAGS_QUERY_KEY } from '../../shared/components/TagPicker';
import { useVocabSafe } from '../../shared/vocab';
import type { GalleryFilterState } from './galleryFilters';

interface GalleryFilterBarProps {
  state: GalleryFilterState;
  patch: (next: Partial<GalleryFilterState>) => void;
  reset: () => void;
  activeCount: number;
  /** 「来源」只对时间轴有意义（00 §3.2），其余视图隐藏它。 */
  showSource: boolean;
}

/**
 * 展览集的筛选条。四个视图共用同一份状态（在 `GalleryPage` 里持有）。
 *
 * 改动任何一项都**立刻写进 URL**（见 `useGalleryFilters`），不点"查询"。
 * 这与卡片页的筛选条是两套组件：那一套是卡片专用的（关键词/日期/回收站…），
 * 照搬会把时间轴不需要的东西一起带进来。
 */
export function GalleryFilterBar({
  state,
  patch,
  reset,
  activeCount,
  showSource,
}: GalleryFilterBarProps) {
  const { vocab } = useVocabSafe();

  const years = useQuery({
    queryKey: ['stats', 'by-year'],
    queryFn: api.stats.byYear,
  });

  const tags = useQuery({
    queryKey: TAGS_QUERY_KEY,
    queryFn: () => api.tags.list(),
    staleTime: 60_000,
  });

  const yearOptions = (years.data?.buckets ?? []).map((bucket) => ({
    value: bucket.key,
    label: bucket.label ?? `${bucket.key} 年`,
  }));

  // 标签库为空时**不显示**这一项（00 §3.2）—— 空下拉只会让人以为功能坏了。
  const tagOptions = (tags.data ?? []).map((tag) => ({ value: tag.id, label: tag.name }));

  return (
    <div className="toolbar gallery-filters">
      <div className="toolbar__row">
        <Field label="年份">
          <Select
            value={state.year}
            onChange={(value) => patch({ year: value })}
            options={yearOptions}
            placeholder="全部年份"
          />
        </Field>

        <Field label="地点" className="field--wide" hint="选省级 = 该省全部下级">
          <PlacePicker
            value={state.place_id}
            onChange={(place) => patch({ place_id: place ? place.id : '' })}
            allowCreate={false}
            placeholder="输入地名，如「南平」"
          />
        </Field>

        {tagOptions.length > 0 ? (
          <Field label="标签">
            <Select
              value={state.tag_id}
              onChange={(value) => patch({ tag_id: value })}
              options={tagOptions}
              placeholder="全部标签"
            />
          </Field>
        ) : null}

        {showSource ? (
          <Field label="来源">
            <Select
              value={state.source}
              onChange={(value) => patch({ source: value })}
              options={vocab.card_sources}
              placeholder="全部来源"
            />
          </Field>
        ) : null}

        <div className="toolbar__spacer" />

        <button
          type="button"
          className="btn"
          onClick={reset}
          disabled={activeCount === 0}
          title="清空 URL 里的全部筛选条件"
        >
          清除筛选{activeCount > 0 ? `（${activeCount}）` : ''}
        </button>
      </div>

      <div className="toolbar__toggle">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={state.provenance === 'real_scan'}
            onChange={(event) =>
              patch({ provenance: event.target.checked ? 'real_scan' : 'all' })
            }
          />
          只看真实跑戳
        </label>
        <span className="small faint">
          关掉后会包含「生成 / 第三方」来源的戳（v1.2 才会出现生成戳，现在看不出差别）
        </span>
      </div>
    </div>
  );
}
