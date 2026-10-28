import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../api';
import { useDebouncedValue, useOutsideClick } from '../hooks';
import type { PlaceRead } from '../types';
import { useVocabSafe } from '../vocab';
import { Field } from './Field';
import { Modal } from './Modal';
import { PlaceTree } from './PlaceTree';
import { Alert, InlineSpinner } from './States';
import { Select } from './Select';
import { useToast } from './Toast';

interface PlacePickerProps {
  value: string;
  onChange: (place: PlaceRead | null) => void;
  /** 外部已知的已选对象（详情页能从 PostmarkRead.place 直接拿到，省一次请求） */
  initialSelected?: PlaceRead | null;
  disabled?: boolean;
  placeholder?: string;
  allowCreate?: boolean;
}

interface CreateForm {
  name: string;
  level: number;
  country_code: string;
  admin_code: string;
  name_en: string;
}

const EMPTY_FORM: CreateForm = {
  name: '',
  level: 0,
  country_code: '',
  admin_code: '',
  name_en: '',
};

/**
 * 地点选择器：**增量补全优先，树浏览为辅**。
 *
 * 为什么两者都要：跑戳时你脑子里是"藁城区"这个名字，直接敲最快；
 * 但第一次录入或名字记不准时，树能给你上下文。需求明确要求"不要每次都从树里点"。
 */
export function PlacePicker({
  value,
  onChange,
  initialSelected = null,
  disabled,
  placeholder = '输入地点名搜索，如「藁城」',
  allowCreate = true,
}: PlacePickerProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { vocab } = useVocabSafe();

  const [text, setText] = useState('');
  const [open, setOpen] = useState(false);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<CreateForm>(EMPTY_FORM);

  const boxRef = useRef<HTMLDivElement>(null);
  useOutsideClick(boxRef, () => setOpen(false), open);

  const debounced = useDebouncedValue(text, 250);

  // ---- 已选对象的解析：先用外部给的，缺失时按 id 查一次 ----
  const known = initialSelected !== null && initialSelected.id === value ? initialSelected : null;
  const resolveQuery = useQuery({
    queryKey: ['places', 'resolve', value],
    queryFn: () => api.places.tree(value, 1),
    enabled: value !== '' && known === null,
    staleTime: Infinity,
  });
  const selected: PlaceRead | null =
    value === '' ? null : (known ?? resolveQuery.data?.[0] ?? null);

  // ---- 增量搜索 ----
  const searchQuery = useQuery({
    queryKey: ['places', 'search', debounced],
    queryFn: () => api.places.list({ q: debounced, limit: 20 }),
    enabled: debounced.trim().length >= 1,
    staleTime: 30_000,
  });

  // ---- 新建 ----
  const createMutation = useMutation({
    mutationFn: () => {
      const parent = selected;
      return api.places.create({
        name: form.name.trim(),
        parent_id: parent ? parent.id : null,
        level: form.level,
        level_label: vocab.level_labels[form.level] ?? null,
        country_code: form.country_code.trim() === '' ? null : form.country_code.trim().toUpperCase(),
        admin_code: form.admin_code.trim() === '' ? null : form.admin_code.trim(),
        name_en: form.name_en.trim() === '' ? null : form.name_en.trim(),
      });
    },
    onSuccess: (created) => {
      toast.success(`已新增地点「${created.name}」`);
      onChange(created);
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['places'] });
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const openCreate = () => {
    const parent = selected;
    setForm({
      ...EMPTY_FORM,
      level: parent ? parent.level + 1 : 0,
      country_code: parent?.country_code ?? '',
    });
    setCreateOpen(true);
  };

  // 选中的地点变了，把搜索词与下拉收起来
  useEffect(() => {
    setText('');
    setOpen(false);
  }, [value]);

  const parentLevel = selected?.level ?? -1;
  const levelInvalid = form.level <= parentLevel && selected !== null;

  return (
    <div className="picker" ref={boxRef}>
      {selected ? (
        <div className="picker__selected">
          <span className="picker__selected-name" title={selected.path ?? selected.name}>
            {selected.name}
          </span>
          {selected.level_label ? (
            <span className="picker__selected-level">（{selected.level_label}）</span>
          ) : null}
          {!disabled ? (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => onChange(null)}
              title="清除地点"
            >
              ✕
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="picker__control">
        <input
          className="input"
          placeholder={selected ? '换一个地点…' : placeholder}
          value={text}
          disabled={disabled}
          onChange={(event) => {
            setText(event.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
        />
        <button
          type="button"
          className="btn btn--sm"
          disabled={disabled}
          onClick={() => setBrowseOpen(true)}
          title="按行政区划层级浏览"
        >
          浏览
        </button>
        {allowCreate && !disabled ? (
          <button type="button" className="btn btn--sm" onClick={openCreate} title="新增地点">
            ＋
          </button>
        ) : null}
      </div>

      {open && !disabled ? (
        <div className="picker__dropdown">
          {debounced.trim().length === 0 ? (
            <div className="picker__hint">输入地名开始搜索，或点「浏览」按层级查找。</div>
          ) : searchQuery.isLoading ? (
            <div className="picker__hint row">
              <InlineSpinner /> 搜索中…
            </div>
          ) : searchQuery.isError ? (
            <div className="picker__hint" style={{ color: 'var(--danger)' }}>
              搜索失败
            </div>
          ) : (searchQuery.data ?? []).length === 0 ? (
            <div className="picker__hint">
              没有匹配「{debounced}」的地点。
              {allowCreate ? (
                <button type="button" className="btn btn--sm btn--ghost" onClick={openCreate}>
                  新建一个
                </button>
              ) : null}
            </div>
          ) : (
            (searchQuery.data ?? []).map((place) => (
              <button
                key={place.id}
                type="button"
                className="picker__option"
                onClick={() => {
                  onChange(place);
                  setOpen(false);
                }}
              >
                <span className="picker__option-name">{place.name}</span>
                <span className="picker__option-meta">
                  {place.level_label ?? `L${place.level}`}
                  {place.admin_code ? ` · ${place.admin_code}` : ''}
                </span>
              </button>
            ))
          )}
        </div>
      ) : null}

      {/* ---------------- 树浏览 ---------------- */}
      <Modal
        open={browseOpen}
        title="按行政区划选择地点"
        size="wide"
        onClose={() => setBrowseOpen(false)}
        footer={
          <>
            <span className="small faint grow">
              层层展开。点地名即选中；也可以直接在上一个框里搜索。
            </span>
            <button type="button" className="btn" onClick={() => setBrowseOpen(false)}>
              关闭
            </button>
          </>
        }
      >
        <PlaceTree
          selectedId={value === '' ? null : value}
          onSelect={(place) => {
            onChange(place);
            setBrowseOpen(false);
          }}
        />
      </Modal>

      {/* ---------------- 新建 ---------------- */}
      <Modal
        open={createOpen}
        title="新增地点"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setCreateOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn btn--primary"
              disabled={form.name.trim() === '' || levelInvalid || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {createMutation.isPending ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <Alert kind="info">
          上级：
          <strong>{selected ? `${selected.name}（${selected.level_label ?? `L${selected.level}`}）` : '无（作为顶级节点，通常是国家）'}</strong>
          。层级必须深于上级 —— 这就是为什么直辖市会如实存成「省级 → 县级」，中间层留空。
        </Alert>

        <Field label="名称" required htmlFor="place-name">
          <input
            id="place-name"
            className="input"
            value={form.name}
            autoFocus
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="如「藁城区」"
          />
        </Field>

        <div className="form-grid form-grid--2">
          <Field
            label="层级"
            hint={`层级深度 ${form.level}`}
            error={levelInvalid ? `必须深于上级（上级 level=${parentLevel}）` : null}
          >
            <Select
              value={String(form.level)}
              onChange={(next) => setForm({ ...form, level: Number(next) })}
              options={vocab.level_labels.map((label, index) => ({
                value: String(index),
                label,
              }))}
              invalid={levelInvalid}
            />
          </Field>

          <Field label="国家代码" hint="ISO 3166-1 两位，如 CN">
            <input
              className="input"
              value={form.country_code}
              maxLength={2}
              onChange={(event) => setForm({ ...form, country_code: event.target.value })}
              placeholder="CN"
            />
          </Field>

          <Field label="行政代码" hint="官方区划代码或邮编，可留空">
            <input
              className="input"
              value={form.admin_code}
              onChange={(event) => setForm({ ...form, admin_code: event.target.value })}
            />
          </Field>

          <Field label="英文名">
            <input
              className="input"
              value={form.name_en}
              onChange={(event) => setForm({ ...form, name_en: event.target.value })}
            />
          </Field>
        </div>
      </Modal>
    </div>
  );
}
