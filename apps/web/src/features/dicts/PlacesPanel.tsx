import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { useConfirm } from '../../shared/components/Confirm';
import { Field } from '../../shared/components/Field';
import { Modal } from '../../shared/components/Modal';
import { PlaceTree } from '../../shared/components/PlaceTree';
import { Select } from '../../shared/components/Select';
import { Alert } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import { useVocabSafe } from '../../shared/vocab';
import type { PlaceNode } from '../../shared/types';

interface FormState {
  name: string;
  level: number;
  country_code: string;
  admin_code: string;
  name_en: string;
}

const EMPTY: FormState = { name: '', level: 0, country_code: '', admin_code: '', name_en: '' };

export function PlacesPanel() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const toast = useToast();
  const { vocab } = useVocabSafe();

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<PlaceNode | null>(null);
  const [parent, setParent] = useState<PlaceNode | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['places'] });
  };

  const createMutation = useMutation({
    mutationFn: () =>
      api.places.create({
        name: form.name.trim(),
        parent_id: parent ? parent.id : null,
        level: form.level,
        level_label: vocab.level_labels[form.level] ?? null,
        country_code: form.country_code.trim() === '' ? null : form.country_code.trim().toUpperCase(),
        admin_code: form.admin_code.trim() === '' ? null : form.admin_code.trim(),
        name_en: form.name_en.trim() === '' ? null : form.name_en.trim(),
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
      if (editing === null) throw new Error('没有正在编辑的节点');
      return api.places.update(editing.id, {
        name: form.name.trim(),
        level_label: vocab.level_labels[form.level] ?? null,
        admin_code: form.admin_code.trim() === '' ? null : form.admin_code.trim(),
        name_en: form.name_en.trim() === '' ? null : form.name_en.trim(),
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
    mutationFn: (id: string) => api.places.remove(id),
    onSuccess: () => {
      toast.success('已删除');
      invalidate();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const openCreateRoot = () => {
    setEditing(null);
    setParent(null);
    setForm({ ...EMPTY, level: 0 });
    setFormOpen(true);
  };

  const openCreateChild = (node: PlaceNode) => {
    setEditing(null);
    setParent(node);
    setForm({
      ...EMPTY,
      level: node.level + 1,
      country_code: node.country_code ?? '',
    });
    setFormOpen(true);
  };

  const openEdit = (node: PlaceNode) => {
    setEditing(node);
    setParent(null);
    setForm({
      name: node.name,
      level: node.level,
      country_code: node.country_code ?? '',
      admin_code: node.admin_code ?? '',
      name_en: node.name_en ?? '',
    });
    setFormOpen(true);
  };

  const onDelete = async (node: PlaceNode) => {
    const ok = await confirm({
      title: `删除地点「${node.name}」？`,
      danger: true,
      confirmText: '删除',
      description: (
        <div className="small">
          <p>只有在**没有下级**、且**没有被任何邮戳引用**时才能删除。</p>
          <p style={{ marginTop: 6 }}>
            后端会因此返回 409，而不是静默把引用置空 —— 那会让历史记录悄悄丢字段。
          </p>
        </div>
      ),
    });
    if (ok) deleteMutation.mutate(node.id);
  };

  const levelInvalid = !editing && parent !== null && form.level <= parent.level;

  return (
    <section className="panel">
      <div className="panel__head">
        <div className="panel__title">行政区划</div>
        <div className="small muted">
          自引用树，**不硬编码层级数**：中国 4–5 级、日本 3 级、美国 3 级都装得下；
          直辖市如实存成「省级 → 县级」，中间层留空。
        </div>
        <div className="right">
          <button type="button" className="btn btn--sm btn--primary" onClick={openCreateRoot}>
            ＋ 新增顶级地点
          </button>
        </div>
      </div>

      <div className="panel__body">
        <PlaceTree
          showOfficeCount
          renderActions={(node) => (
            <>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                title="在此节点下新增下级"
                onClick={() => openCreateChild(node)}
              >
                ＋子级
              </button>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                title="编辑"
                onClick={() => openEdit(node)}
              >
                改
              </button>
              <button
                type="button"
                className="btn btn--sm btn--danger-ghost"
                title="删除"
                onClick={() => {
                  void onDelete(node);
                }}
              >
                删
              </button>
            </>
          )}
        />
      </div>

      <Modal
        open={formOpen}
        title={editing ? `编辑「${editing.name}」` : parent ? `在「${parent.name}」下新增下级` : '新增顶级地点'}
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
                form.name.trim() === '' ||
                levelInvalid ||
                createMutation.isPending ||
                updateMutation.isPending
              }
              onClick={() => (editing ? updateMutation.mutate() : createMutation.mutate())}
            >
              {createMutation.isPending || updateMutation.isPending ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        {parent && !editing ? (
          <Alert kind="info">
            上级：<strong>{parent.name}（{parent.level_label ?? `L${parent.level}`}）</strong>
          </Alert>
        ) : null}

        <Field label="名称" required htmlFor="dict-place-name">
          <input
            id="dict-place-name"
            className="input"
            autoFocus
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
        </Field>

        <div className="form-grid form-grid--2">
          <Field
            label="层级"
            hint={editing ? '编辑时只改标签，不改层级深度（改深度会牵动物化路径）' : `层级深度 ${form.level}`}
            error={levelInvalid ? `必须深于上级（上级 level=${parent?.level ?? 0}）` : null}
          >
            <Select
              value={String(form.level)}
              disabled={editing !== null}
              onChange={(value) => setForm({ ...form, level: Number(value) })}
              options={vocab.level_labels.map((label, index) => ({ value: String(index), label }))}
              invalid={levelInvalid}
            />
          </Field>

          <Field label="国家代码" hint="ISO 3166-1 两位">
            <input
              className="input"
              maxLength={2}
              value={form.country_code}
              disabled={editing !== null}
              onChange={(event) => setForm({ ...form, country_code: event.target.value })}
            />
          </Field>

          <Field label="行政代码">
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
    </section>
  );
}
