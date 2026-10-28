import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { useConfirm } from '../../shared/components/Confirm';
import { EmptyState } from '../../shared/components/EmptyState';
import { Field } from '../../shared/components/Field';
import { Modal } from '../../shared/components/Modal';
import { Pagination } from '../../shared/components/Pagination';
import { PlacePicker } from '../../shared/components/PlacePicker';
import { Alert, ErrorState, LoadingState } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import { useDebouncedValue } from '../../shared/hooks';
import type { PlaceRead, PostOfficeRead } from '../../shared/types';

interface FormState {
  name: string;
  postal_code: string;
  is_theme: boolean;
  theme_name: string;
  aliases: string;
}

const EMPTY: FormState = { name: '', postal_code: '', is_theme: false, theme_name: '', aliases: '' };

export function PostOfficesPanel() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const toast = useToast();

  const [keyword, setKeyword] = useState('');
  const [themeOnly, setThemeOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(50);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<PostOfficeRead | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [formPlace, setFormPlace] = useState<PlaceRead | null>(null);

  const debouncedKeyword = useDebouncedValue(keyword, 300);

  const query = useQuery({
    queryKey: ['post-offices', 'admin', debouncedKeyword, themeOnly, page, size],
    queryFn: () =>
      api.postOffices.list({
        q: debouncedKeyword.trim() === '' ? null : debouncedKeyword.trim(),
        is_theme: themeOnly ? true : null,
        page,
        size,
      }),
    placeholderData: (previous) => previous,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['post-offices'] });
  };

  const createMutation = useMutation({
    mutationFn: () =>
      api.postOffices.create({
        name: form.name.trim(),
        place_id: formPlace ? formPlace.id : null,
        postal_code: form.postal_code.trim() === '' ? null : form.postal_code.trim(),
        is_theme: form.is_theme,
        theme_name: form.theme_name.trim() === '' ? null : form.theme_name.trim(),
        aliases: form.aliases
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter((item) => item !== ''),
      }),
    onSuccess: (created) => {
      toast.success(`已新增「${created.name}」`);
      setFormOpen(false);
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const updateMutation = useMutation({
    mutationFn: () => {
      if (editing === null) throw new Error('没有正在编辑的邮局');
      return api.postOffices.update(editing.id, {
        name: form.name.trim(),
        place_id: formPlace ? formPlace.id : null,
        postal_code: form.postal_code.trim() === '' ? null : form.postal_code.trim(),
        is_theme: form.is_theme,
        theme_name: form.theme_name.trim() === '' ? null : form.theme_name.trim(),
        aliases: form.aliases
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter((item) => item !== ''),
      });
    },
    onSuccess: (updated) => {
      toast.success(`已更新「${updated.name}」`);
      setFormOpen(false);
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.postOffices.remove(id),
    onSuccess: () => {
      toast.success('已删除');
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY);
    setFormPlace(null);
    setFormOpen(true);
  };

  const openEdit = (office: PostOfficeRead) => {
    setEditing(office);
    setForm({
      name: office.name,
      postal_code: office.postal_code ?? '',
      is_theme: office.is_theme,
      theme_name: office.theme_name ?? '',
      aliases: office.aliases.join(' '),
    });
    setFormPlace(office.place);
    setFormOpen(true);
  };

  const onDelete = async (office: PostOfficeRead) => {
    const ok = await confirm({
      title: `删除邮局「${office.name}」？`,
      danger: true,
      confirmText: '删除',
      description: '被邮戳引用时后端会拒绝删除（409），以免历史记录悄悄丢掉"哪个邮局盖的"。',
    });
    if (ok) deleteMutation.mutate(office.id);
  };

  const items = query.data?.items ?? [];

  return (
    <section className="panel">
      <div className="panel__head">
        <div className="panel__title">邮局</div>
        <div className="small muted">「哪个邮局盖的」。主题邮局是**邮局**的属性，在这里维护。</div>
        <div className="right">
          <button type="button" className="btn btn--sm btn--primary" onClick={openCreate}>
            ＋ 新增邮局
          </button>
        </div>
      </div>

      <div className="panel__body">
        <div className="toolbar__row" style={{ marginBottom: 12 }}>
          <Field label="搜索" className="field--wide" hint="按名称或邮编">
            <input
              className="input"
              value={keyword}
              onChange={(event) => {
                setKeyword(event.target.value);
                setPage(1);
              }}
              placeholder="如「建溪」或「353100」"
            />
          </Field>

          <label className="checkbox" style={{ paddingBottom: 6 }}>
            <input
              type="checkbox"
              checked={themeOnly}
              onChange={(event) => {
                setThemeOnly(event.target.checked);
                setPage(1);
              }}
            />
            只看主题邮局
          </label>

          <button
            type="button"
            className="btn"
            onClick={() => {
              setKeyword('');
              setThemeOnly(false);
              setPage(1);
            }}
            disabled={keyword === '' && !themeOnly}
          >
            重置
          </button>
        </div>

        {query.isLoading ? <LoadingState text="加载邮局…" /> : null}
        {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}

        {query.data ? (
          items.length === 0 ? (
            <EmptyState
              icon="🏣"
              title={keyword === '' ? '还没有邮局记录' : '没有匹配的邮局'}
              text={
                keyword === ''
                  ? '邮局在录入邮戳时可以就地新建，也可以在这里先批量建好。'
                  : '试试换个关键词，或清空搜索。'
              }
              action={
                <button type="button" className="btn btn--primary" onClick={openCreate}>
                  ＋ 新增邮局
                </button>
              }
            />
          ) : (
            <div className="col" style={{ gap: 8 }}>
              {items.map((office) => (
                <div key={office.id} className="card-list__item">
                  <div className="card-list__main">
                    <div className="row" style={{ gap: 6 }}>
                      <strong>{office.name}</strong>
                      {office.is_theme ? (
                        <span className="badge badge--theme">
                          主题邮局{office.theme_name ? ` · ${office.theme_name}` : ''}
                        </span>
                      ) : null}
                    </div>
                    <div className="small muted">
                      {office.place ? office.place.name : '未归属行政区'}
                      {office.postal_code ? ` · ${office.postal_code}` : ''}
                      {office.aliases.length > 0 ? ` · 别名：${office.aliases.join('、')}` : ''}
                    </div>
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    <button type="button" className="btn btn--sm" onClick={() => openEdit(office)}>
                      编辑
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--danger-ghost"
                      onClick={() => {
                        void onDelete(office);
                      }}
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )
        ) : null}

        {query.data && query.data.total > 0 ? (
          <Pagination
            page={page}
            size={size}
            total={query.data.total}
            onPage={setPage}
            onSize={(next) => {
              setSize(next);
              setPage(1);
            }}
          />
        ) : null}
      </div>

      <Modal
        open={formOpen}
        title={editing ? `编辑「${editing.name}」` : '新增邮局'}
        onClose={() => setFormOpen(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setFormOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn btn--primary"
              disabled={
                form.name.trim() === '' || createMutation.isPending || updateMutation.isPending
              }
              onClick={() => (editing ? updateMutation.mutate() : createMutation.mutate())}
            >
              {createMutation.isPending || updateMutation.isPending ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <Field label="邮局名称" required htmlFor="admin-office-name">
          <input
            id="admin-office-name"
            className="input"
            autoFocus
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="中关村邮政支局"
          />
        </Field>

        <Field label="所在行政区">
          <PlacePicker value={formPlace?.id ?? ''} initialSelected={formPlace} onChange={setFormPlace} />
        </Field>

        <div className="form-grid form-grid--2">
          <Field label="邮政编码">
            <input
              className="input"
              value={form.postal_code}
              onChange={(event) => setForm({ ...form, postal_code: event.target.value })}
            />
          </Field>

          <Field label="别名 / 简称">
            <input
              className="input"
              value={form.aliases}
              onChange={(event) => setForm({ ...form, aliases: event.target.value })}
              placeholder="空格分隔"
            />
          </Field>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.is_theme}
            onChange={(event) => setForm({ ...form, is_theme: event.target.checked })}
          />
          主题邮局
        </label>

        {form.is_theme ? (
          <Field label="主题名">
            <input
              className="input"
              value={form.theme_name}
              onChange={(event) => setForm({ ...form, theme_name: event.target.value })}
              placeholder="生肖 / 故宫 / 航天"
            />
          </Field>
        ) : null}

        {createMutation.isError || updateMutation.isError ? (
          <Alert kind="error">
            {toApiError(createMutation.error ?? updateMutation.error).message}
          </Alert>
        ) : null}
      </Modal>
    </section>
  );
}
