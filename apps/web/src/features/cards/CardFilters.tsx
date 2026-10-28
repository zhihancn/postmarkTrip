import { useQuery } from '@tanstack/react-query';

import { api } from '../../shared/api';
import { DateInput } from '../../shared/components/DateInput';
import { Field } from '../../shared/components/Field';
import { PlacePicker } from '../../shared/components/PlacePicker';
import { PostOfficePicker } from '../../shared/components/PostOfficePicker';
import { Select } from '../../shared/components/Select';
import { labelOf, prioritizeOptions, useVocabSafe } from '../../shared/vocab';
import { TAGS_QUERY_KEY } from '../../shared/components/TagPicker';
import type { CardFilterState } from './useCardFilters';

interface CardFiltersProps {
  state: CardFilterState;
  patch: (next: Partial<CardFilterState>) => void;
  reset: () => void;
  activeCount: number;
}

/**
 * 筛选栏。所有下拉取值来自 /api/vocab；改任何一项都回到第 1 页
 * —— 否则会出现"第 3 页但没有 3 页那么多种结果"的空白。
 */
export function CardFilters({ state, patch, reset, activeCount }: CardFiltersProps) {
  const { vocab } = useVocabSafe();

  const tagsQuery = useQuery({
    queryKey: TAGS_QUERY_KEY,
    queryFn: () => api.tags.list(),
    staleTime: 60_000,
  });

  const postmarkTypeOptions = prioritizeOptions(vocab.postmark_types, vocab.primary_postmark_types);
  const tagOptions = (tagsQuery.data ?? []).map((tag) => ({ value: tag.name, label: tag.name }));

  return (
    <div className="toolbar">
      <div className="toolbar__row">
        <Field label="关键词" className="field--wide">
          <input
            className="input"
            placeholder="戳面文字 / 局名 / 票名 / 志号 / 备注"
            value={state.q}
            onChange={(event) => patch({ q: event.target.value, page: 1 })}
          />
        </Field>

        <Field label="跑戳日期（起）">
          <DateInput
            value={state.visit_date_from}
            onChange={(value) => patch({ visit_date_from: value, page: 1 })}
          />
        </Field>

        <Field label="跑戳日期（止）">
          <DateInput
            value={state.visit_date_to}
            onChange={(value) => patch({ visit_date_to: value, page: 1 })}
          />
        </Field>

        <button
          type="button"
          className="btn"
          onClick={() => patch({ visit_date_from: '', visit_date_to: '', page: 1 })}
          disabled={state.visit_date_from === '' && state.visit_date_to === ''}
        >
          清除日期
        </button>
      </div>

      <div className="toolbar__row">
        <Field label="地点" className="field--wide" hint={state.place_descendants ? '含所有下级行政区' : '仅限该节点'}>
          <PlacePicker
            value={state.place_id}
            onChange={(place) => patch({ place_id: place ? place.id : '', page: 1 })}
            allowCreate={false}
            placeholder="输入地名，如「南平」"
          />
        </Field>

        <Field label="含下级">
          <label className="checkbox" style={{ padding: '7px 0' }}>
            <input
              type="checkbox"
              checked={state.place_descendants}
              onChange={(event) => patch({ place_descendants: event.target.checked, page: 1 })}
            />
            勾选后「北京市」会带出所有区
          </label>
        </Field>
      </div>

      <div className="toolbar__row">
        <Field label="邮局" className="field--wide">
          <PostOfficePicker
            value={state.post_office_id}
            onChange={(office) => patch({ post_office_id: office ? office.id : '', page: 1 })}
            allowCreate={false}
            placeholder="输入邮局名或邮编"
          />
        </Field>

        <Field label="戳种">
          <Select
            value={state.postmark_type}
            onChange={(value) => patch({ postmark_type: value, page: 1 })}
            options={postmarkTypeOptions}
            placeholder="不限"
          />
        </Field>

        <Field label="来源">
          <Select
            value={state.source}
            onChange={(value) => patch({ source: value, page: 1 })}
            options={vocab.card_sources}
            placeholder="不限"
          />
        </Field>

        <Field label="状态">
          <Select
            value={state.status}
            onChange={(value) => patch({ status: value, page: 1 })}
            options={vocab.card_statuses}
            placeholder="不限"
          />
        </Field>
      </div>

      <div className="toolbar__row">
        <Field label="是否彩色">
          <Select
            value={state.is_color}
            onChange={(value) =>
              patch({ is_color: value === 'true' ? 'true' : value === 'false' ? 'false' : '', page: 1 })
            }
            options={[
              { value: 'true', label: '只看彩色戳' },
              { value: 'false', label: '只看非彩色' },
            ]}
            placeholder="不限"
          />
        </Field>

        <Field
          label="来源标记"
          hint="「生成/美化」的条目不是真实跑戳，默认统计会排除"
        >
          <Select
            value={state.provenance}
            onChange={(value) => patch({ provenance: value, page: 1 })}
            options={vocab.provenances}
            placeholder="不限"
          />
        </Field>

        <Field label="标签">
          <Select
            value={state.tag}
            onChange={(value) => patch({ tag: value, page: 1 })}
            options={tagOptions}
            placeholder="不限"
          />
        </Field>

        <Field label="排序">
          <Select
            value={`${state.order_by}:${state.order_desc ? 'desc' : 'asc'}`}
            onChange={(value) => {
              const [orderBy, direction] = value.split(':');
              patch({
                order_by: (orderBy as CardFilterState['order_by']) ?? 'visit_date',
                order_desc: direction === 'desc',
                page: 1,
              });
            }}
            options={[
              { value: 'visit_date:desc', label: '跑戳日期（新→旧）' },
              { value: 'visit_date:asc', label: '跑戳日期（旧→新）' },
              { value: 'created_at:desc', label: '录入时间（新→旧）' },
              { value: 'created_at:asc', label: '录入时间（旧→新）' },
              { value: 'title:asc', label: '标题（A→Z）' },
              { value: 'title:desc', label: '标题（Z→A）' },
            ]}
          />
        </Field>

        <div className="toolbar__spacer" />

        <button type="button" className="btn" onClick={reset} disabled={activeCount === 0}>
          重置筛选{activeCount > 0 ? `（${activeCount}）` : ''}
        </button>
      </div>

      <div className="toolbar__toggle">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={state.include_deleted}
            onChange={(event) =>
              patch({
                include_deleted: event.target.checked,
                page: 1,
                // 回收站里的记录没有"跑戳日期排序"以外的意义，保持原排序即可
              })
            }
          />
          回收站视图
        </label>

        {state.postmark_type !== '' ? (
          <span className="badge badge--primary">
            戳种：{labelOf(vocab.postmark_types, state.postmark_type)}
          </span>
        ) : null}

        {state.include_deleted ? (
          <span className="badge badge--warn">正在查看已删除的卡片</span>
        ) : null}
      </div>
    </div>
  );
}
