import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { useConfirm } from '../../shared/components/Confirm';
import { EmptyState } from '../../shared/components/EmptyState';
import { Field } from '../../shared/components/Field';
import { Select } from '../../shared/components/Select';
import { ErrorState, LoadingState } from '../../shared/components/States';
import { TAGS_QUERY_KEY } from '../../shared/components/TagPicker';
import { useToast } from '../../shared/components/Toast';
import { labelOf, useVocabSafe } from '../../shared/vocab';
import type { TagCategory } from '../../shared/types';

const UNCATEGORIZED = '__none__';

export function TagsPanel() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const toast = useToast();
  const { vocab } = useVocabSafe();

  const [name, setName] = useState('');
  const [category, setCategory] = useState('');
  const [color, setColor] = useState('#1f6feb');

  const query = useQuery({
    queryKey: TAGS_QUERY_KEY,
    queryFn: () => api.tags.list(),
    staleTime: 30_000,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: TAGS_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: ['cards'] });
  };

  const createMutation = useMutation({
    mutationFn: () =>
      api.tags.create({
        name: name.trim(),
        category: category === '' ? null : (category as TagCategory),
        color: color === '' ? null : color,
      }),
    onSuccess: (created) => {
      toast.success(`已新增标签「${created.name}」`);
      setName('');
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.tags.remove(id),
    onSuccess: () => {
      toast.success('已删除标签（同时解除了它在所有记录上的引用）');
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const onDelete = async (id: string, tagName: string) => {
    const ok = await confirm({
      title: `删除标签「${tagName}」？`,
      danger: true,
      confirmText: '删除',
      description: '标签会从所有已打标的记录上移除，但记录本身不会被删除。',
    });
    if (ok) deleteMutation.mutate(id);
  };

  const tags = query.data ?? [];

  // 按分类分组；未分类的单独一组
  const groups = new Map<string, typeof tags>();
  for (const tag of tags) {
    const key = tag.category ?? UNCATEGORIZED;
    const bucket = groups.get(key);
    if (bucket) bucket.push(tag);
    else groups.set(key, [tag]);
  }

  const orderedKeys = [
    ...vocab.tag_categories.map((item) => item.value).filter((value) => groups.has(value)),
    ...(groups.has(UNCATEGORIZED) ? [UNCATEGORIZED] : []),
  ];

  const groupLabel = (key: string) =>
    key === UNCATEGORIZED ? '未分类' : labelOf(vocab.tag_categories, key);

  return (
    <section className="panel">
      <div className="panel__head">
        <div className="panel__title">标签</div>
        <div className="small muted">
          灵活的分类维度。注意有些 tag 同时是结构化字段（如「主题邮局」既是邮局属性，也可以打标）
        </div>
      </div>

      <div className="panel__body">
        <div className="toolbar__row" style={{ marginBottom: 14 }}>
          <Field label="标签名" className="field--wide" htmlFor="tag-name">
            <input
              id="tag-name"
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && name.trim() !== '') {
                  event.preventDefault();
                  createMutation.mutate();
                }
              }}
              placeholder="如「主题邮局」「彩色戳」"
            />
          </Field>

          <Field label="分类">
            <Select
              value={category}
              onChange={setCategory}
              options={vocab.tag_categories}
              placeholder="未分类"
            />
          </Field>

          <Field label="颜色">
            <input
              className="input"
              type="color"
              style={{ width: 64, padding: 3, height: 34 }}
              value={color}
              onChange={(event) => setColor(event.target.value)}
            />
          </Field>

          <button
            type="button"
            className="btn btn--primary"
            disabled={name.trim() === '' || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? '新增中…' : '＋ 新增标签'}
          </button>
        </div>

        {query.isLoading ? <LoadingState text="加载标签…" /> : null}
        {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}

        {query.data ? (
          tags.length === 0 ? (
            <EmptyState
              icon="🏷"
              title="还没有标签"
              text="常用的是「主题邮局」和「彩色戳」。在上面的输入框里加一个。"
            />
          ) : (
            <div className="col" style={{ gap: 14 }}>
              {orderedKeys.map((key) => (
                <div key={key}>
                  <div className="field__label" style={{ marginBottom: 6 }}>
                    {groupLabel(key)}
                  </div>
                  <div className="chip-list">
                    {(groups.get(key) ?? []).map((tag) => (
                      <span key={tag.id} className="chip chip--static">
                        {tag.color ? (
                          <span className="chip__dot" style={{ background: tag.color }} aria-hidden="true" />
                        ) : null}
                        {tag.name}
                        <button
                          type="button"
                          className="chip__remove"
                          aria-label={`删除标签 ${tag.name}`}
                          title="删除标签"
                          onClick={() => {
                            void onDelete(tag.id, tag.name);
                          }}
                        >
                          ✕
                        </button>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )
        ) : null}
      </div>
    </section>
  );
}
