import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../api';
import type { TagRead } from '../types';
import { useVocabSafe } from '../vocab';
import { InlineSpinner } from './States';
import { Select } from './Select';
import { useToast } from './Toast';

export const TAGS_QUERY_KEY = ['tags'] as const;

interface TagPickerProps {
  value: string[];
  onChange: (ids: string[]) => void;
  allowCreate?: boolean;
  disabled?: boolean;
}

/**
 * 多选标签。标签不是"少量固定枚举"，所以做成 chip 点选 + 就地新建，
 * 而不是下拉多选（下拉在标签变多之后难用）。
 */
export function TagPicker({ value, onChange, allowCreate = true, disabled }: TagPickerProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { vocab } = useVocabSafe();

  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newCategory, setNewCategory] = useState('');

  const tagsQuery = useQuery({
    queryKey: TAGS_QUERY_KEY,
    queryFn: () => api.tags.list(),
    staleTime: 60_000,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      api.tags.create({
        name: newName.trim(),
        category: newCategory === '' ? null : (newCategory as TagRead['category']),
      }),
    onSuccess: (created) => {
      toast.success(`已新增标签「${created.name}」`);
      setNewName('');
      setCreating(false);
      void queryClient.invalidateQueries({ queryKey: TAGS_QUERY_KEY });
      onChange([...value, created.id]);
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const tags = tagsQuery.data ?? [];

  const toggle = (id: string) => {
    if (disabled) return;
    if (value.includes(id)) {
      onChange(value.filter((item) => item !== id));
    } else {
      onChange([...value, id]);
    }
  };

  return (
    <div className="col" style={{ gap: 6 }}>
      {tagsQuery.isLoading ? (
        <div className="row small muted">
          <InlineSpinner /> 加载标签…
        </div>
      ) : null}

      {tagsQuery.isError ? (
        <div className="small" style={{ color: 'var(--danger)' }}>
          标签加载失败
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => tagsQuery.refetch()}>
            重试
          </button>
        </div>
      ) : null}

      <div className="chip-list">
        {tags.map((tag) => {
          const active = value.includes(tag.id);
          return (
            <button
              key={tag.id}
              type="button"
              className={`chip${active ? ' chip--active' : ''}`}
              disabled={disabled}
              aria-pressed={active}
              onClick={() => toggle(tag.id)}
            >
              {tag.color ? (
                <span className="chip__dot" style={{ background: tag.color }} aria-hidden="true" />
              ) : null}
              {tag.name}
            </button>
          );
        })}

        {allowCreate && !disabled ? (
          <button
            type="button"
            className="chip"
            onClick={() => setCreating((value_) => !value_)}
            title="新建标签"
          >
            ＋ 新建
          </button>
        ) : null}

        {tags.length === 0 && !tagsQuery.isLoading ? (
          <span className="small faint">还没有标签。点「＋ 新建」加一个，例如「主题邮局」。</span>
        ) : null}
      </div>

      {creating ? (
        <div className="row row--wrap" style={{ gap: 6 }}>
          <input
            className="input"
            style={{ maxWidth: 190 }}
            placeholder="标签名，如「彩色戳」"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && newName.trim() !== '') {
                event.preventDefault();
                createMutation.mutate();
              }
            }}
          />
          <div style={{ minWidth: 120 }}>
            <Select
              value={newCategory}
              onChange={setNewCategory}
              options={vocab.tag_categories}
              placeholder="分类（可选）"
            />
          </div>
          <button
            type="button"
            className="btn btn--sm btn--primary"
            disabled={newName.trim() === '' || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? '创建中…' : '创建'}
          </button>
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => setCreating(false)}>
            取消
          </button>
        </div>
      ) : null}
    </div>
  );
}
