import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../api';
import { useDebouncedValue, useOutsideClick } from '../hooks';
import type { PlaceRead, PostOfficeRead } from '../types';
import { Field } from './Field';
import { Modal } from './Modal';
import { PlacePicker } from './PlacePicker';
import { Alert, InlineSpinner } from './States';
import { useToast } from './Toast';

interface PostOfficePickerProps {
  value: string;
  onChange: (office: PostOfficeRead | null) => void;
  /** 详情页能从 PostmarkRead.post_office 直接拿到 */
  initialSelected?: PostOfficeRead | null;
  disabled?: boolean;
  allowCreate?: boolean;
  placeholder?: string;
  /**
   * **只在这个地点（含其下级）里找**（`ok/18` §3.3 D-④，用户 2026-10-12 要求）。
   *
   * 为什么必须有：局名短名撞车极凶（实测「人民路」出现在 **104** 个县）。不限定
   * 范围时，人搜「八一路」会得到全国几十条同名 —— 而他要的是"南平的八一路"。
   * 接口本来就支持（`place_id` + `place_descendants`），是界面没传。
   */
  placeId?: string;
}

/**
 * 邮局选择器：增量补全 + 就地新建。
 *
 * 只有 id、没有对象时按 id 反查一次（见下面的 `resolveQuery`）。
 */
export function PostOfficePicker({
  value,
  onChange,
  initialSelected = null,
  disabled,
  allowCreate = true,
  placeholder = '输入邮局名或邮编，如「建溪」',
  placeId,
}: PostOfficePickerProps) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const [text, setText] = useState('');
  const [open, setOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  const [form, setForm] = useState({
    name: '',
    postalCode: '',
    isTheme: false,
    themeName: '',
    aliases: '',
  });
  const [formPlace, setFormPlace] = useState<PlaceRead | null>(null);

  const boxRef = useRef<HTMLDivElement>(null);
  useOutsideClick(boxRef, () => setOpen(false), open);

  const debounced = useDebouncedValue(text, 250);

  // ---- 已选对象的解析 ----
  //
  // `initialSelected` 只在详情页刚加载时有值；列表、新建、以及只拿到 id 的
  // 场景都没有它，这时按 id 反查一次。
  //
  // ⚠️ 这里长期留着一句"后端目前没有 GET /api/post-offices/{id}"，于是脱离
  //    initialSelected 就退化成"已选邮局（名称未加载）"。**端点其实早就补上了**
  //    （见 docs/ok/06 里那一轮后端修正），只是这句注释没人更新 ——
  //    结果前端一直没去用它，端点也就一直"没人调用"。
  const resolveQuery = useQuery({
    queryKey: ['post-offices', 'resolve', value],
    queryFn: () => api.postOffices.get(value),
    enabled: value !== '' && initialSelected?.id !== value,
    staleTime: Infinity,
  });

  const selected: PostOfficeRead | null =
    value === ''
      ? null
      : initialSelected !== null && initialSelected.id === value
        ? initialSelected
        : (resolveQuery.data ?? null);

  const searchQuery = useQuery({
    //: `placeId` 进 queryKey —— 换了地点就是另一个候选集，不能复用缓存
    queryKey: ['post-offices', 'search', debounced, placeId ?? ''],
    queryFn: () =>
      api.postOffices.list({
        q: debounced,
        size: 20,
        //: 只在这个地点**含其下级**里找（`place_descendants`）——
        //: 不限定就会把全国的「八一路」都端上来（`ok/18` §3.3 D-④）
        ...(placeId ? { place_id: placeId, place_descendants: true } : {}),
      }),
    enabled: debounced.trim().length >= 1,
    staleTime: 30_000,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      api.postOffices.create({
        name: form.name.trim(),
        place_id: formPlace ? formPlace.id : null,
        postal_code: form.postalCode.trim() === '' ? null : form.postalCode.trim(),
        is_theme: form.isTheme,
        theme_name: form.themeName.trim() === '' ? null : form.themeName.trim(),
        aliases: form.aliases
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter((item) => item !== ''),
      }),
    onSuccess: (created) => {
      toast.success(`已新增邮局「${created.name}」`);
      onChange(created);
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['post-offices'] });
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const openCreate = () => {
    setForm({
      name: selected?.name ?? text.trim(),
      postalCode: selected?.postal_code ?? '',
      isTheme: selected?.is_theme ?? false,
      themeName: selected?.theme_name ?? '',
      aliases: (selected?.aliases ?? []).join(' '),
    });
    setFormPlace(selected?.place ?? null);
    setCreateOpen(true);
  };

  useEffect(() => {
    setText('');
    setOpen(false);
  }, [value]);

  const results = searchQuery.data?.items ?? [];

  return (
    <div className="picker" ref={boxRef}>
      {value !== '' ? (
        <div className="picker__selected">
          <span className="picker__selected-name" title={selected?.name ?? undefined}>
            {selected
              ? selected.name
              : resolveQuery.isError
                ? '已选邮局（查不到，可能已被删除）'
                : '已选邮局（名称加载中…）'}
          </span>
          {selected?.is_theme ? <span className="badge badge--theme">主题邮局</span> : null}
          {selected?.postal_code ? (
            <span className="picker__selected-level">{selected.postal_code}</span>
          ) : null}
          {!disabled ? (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => onChange(null)}
              title="清除邮局"
            >
              ✕
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="picker__control">
        <input
          className="input"
          placeholder={value === '' ? placeholder : '换一个邮局…'}
          value={text}
          disabled={disabled}
          onChange={(event) => {
            setText(event.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
        />
        {allowCreate && !disabled ? (
          <button type="button" className="btn btn--sm" onClick={openCreate} title="新增邮局">
            ＋
          </button>
        ) : null}
      </div>

      {open && !disabled ? (
        <div className="picker__dropdown">
          {debounced.trim().length === 0 ? (
            <div className="picker__hint">输入邮局名或邮编开始搜索。</div>
          ) : searchQuery.isLoading ? (
            <div className="picker__hint row">
              <InlineSpinner /> 搜索中…
            </div>
          ) : searchQuery.isError ? (
            <div className="picker__hint" style={{ color: 'var(--danger)' }}>
              搜索失败
            </div>
          ) : (
            <>
              {results.length === 0 ? (
                <div className="picker__hint">没有匹配的邮局。</div>
              ) : (
                results.map((office) => (
                  <button
                    key={office.id}
                    type="button"
                    className="picker__option"
                    onClick={() => {
                      onChange(office);
                      setOpen(false);
                    }}
                  >
                    <span className="picker__option-name">{office.name}</span>
                    <span className="picker__option-meta">
                      {office.is_theme ? '主题 · ' : ''}
                      {office.place?.name ?? ''}
                      {office.postal_code ? ` · ${office.postal_code}` : ''}
                    </span>
                  </button>
                ))
              )}

              {allowCreate ? (
                <button
                  type="button"
                  className="picker__option picker__create"
                  onClick={openCreate}
                >
                  <span className="picker__option-name">＋ 新增邮局「{debounced.trim()}」</span>
                </button>
              ) : null}

              {searchQuery.data && searchQuery.data.total > results.length ? (
                <div className="picker__hint">
                  仅显示前 {results.length} 条（共 {searchQuery.data.total} 条），请继续输入以缩小范围。
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}

      <Modal
        open={createOpen}
        title="新增邮局"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setCreateOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn btn--primary"
              disabled={form.name.trim() === '' || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {createMutation.isPending ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <Field label="邮局名称" required htmlFor="office-name">
          <input
            id="office-name"
            className="input"
            autoFocus
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="如「中关村邮政支局」"
          />
        </Field>

        <Field label="所在行政区" hint="选填，但填了之后「按地点筛选邮局」才能用">
          <PlacePicker value={formPlace?.id ?? ''} initialSelected={formPlace} onChange={setFormPlace} />
        </Field>

        <div className="form-grid form-grid--2">
          <Field label="邮政编码" hint="日戳下环通常就是它">
            <input
              className="input"
              value={form.postalCode}
              onChange={(event) => setForm({ ...form, postalCode: event.target.value })}
              placeholder="100080"
            />
          </Field>

          <Field label="别名 / 简称" hint="空格或逗号分隔，用于模糊匹配">
            <input
              className="input"
              value={form.aliases}
              onChange={(event) => setForm({ ...form, aliases: event.target.value })}
              placeholder="中关村 中关村支局"
            />
          </Field>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.isTheme}
            onChange={(event) => setForm({ ...form, isTheme: event.target.checked })}
          />
          主题邮局
        </label>

        {form.isTheme ? (
          <Field label="主题名" hint="如「生肖」「故宫」「航天」">
            <input
              className="input"
              value={form.themeName}
              onChange={(event) => setForm({ ...form, themeName: event.target.value })}
            />
          </Field>
        ) : null}

        <Alert kind="info">
          主题邮局是**邮局**的属性，不是戳的属性 —— 所以它在这里，而不是在标签里。
          标签里也可以另外打一个「主题邮局」，两者并存不冲突。
        </Alert>

        {createMutation.isError ? <Alert kind="error">{toApiError(createMutation.error).message}</Alert> : null}
      </Modal>
    </div>
  );
}
