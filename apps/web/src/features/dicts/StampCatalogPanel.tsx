import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { DateInput } from '../../shared/components/DateInput';
import { EmptyState } from '../../shared/components/EmptyState';
import { Field } from '../../shared/components/Field';
import { Modal } from '../../shared/components/Modal';
import { Pagination } from '../../shared/components/Pagination';
import { Select } from '../../shared/components/Select';
import { ErrorState, LoadingState } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import { useDebouncedValue } from '../../shared/hooks';
import { useVocabSafe } from '../../shared/vocab';
import type {
  CatalogLastParse,
  DuplicateGroup,
  StampCatalogCreate,
  StampCatalogRead,
  StampCatalogUpdate,
} from '../../shared/types';

const PAGE_SIZE = 24;

const STATE_OPTIONS = [
  { value: 'draft', label: '草稿' },
  { value: 'partial', label: '部分字段' },
  { value: 'verified', label: '已确认' },
  { value: 'stale', label: '来源过期' },
];

const STATE_LABEL: Record<string, string> = Object.fromEntries(
  STATE_OPTIONS.map((item) => [item.value, item.label]),
);

const PARSE_DETAIL_LABEL: Record<string, string> = {
  size_mm: '邮票规格',
  perforation: '齿孔度数',
  sheet_count: '整版枚数',
  sheet_size_mm: '整版规格',
  printing: '印刷版别',
  security: '防伪方式',
  designer: '设计者',
  editor: '责任编辑',
  printer: '印刷厂',
};

function parseLabel(key: string): string {
  return PARSE_DETAIL_LABEL[key] ?? key;
}

function faceText(value: string | null, currency: string | null): string {
  if (!value) return '—';
  const [integer, fraction = ''] = value.split('.');
  const cents = `${fraction}00`.slice(0, 2);
  return `${integer}.${cents} ${currency ?? ''}`.trim();
}

function lastParseOf(row: StampCatalogRead | null): CatalogLastParse | null {
  const raw = row?.details?.last_parse;
  if (!raw || typeof raw !== 'object') return null;
  return raw as CatalogLastParse;
}

interface FormState {
  standard_code: string;
  colnect_url: string;
  issue_url: string;
  catalog_system: string;
  catalog_ref: string;
  design_no: string;
  design_name: string;
  issue_name: string;
  face_value: string;
  currency: string;
  issue_date: string;
  note: string;
  source_state: string;
}

function emptyForm(): FormState {
  return {
    standard_code: '',
    colnect_url: '',
    issue_url: '',
    catalog_system: 'china',
    catalog_ref: '',
    design_no: '',
    design_name: '',
    issue_name: '',
    face_value: '',
    currency: 'CNY',
    issue_date: '',
    note: '',
    source_state: 'draft',
  };
}

function formFrom(row: StampCatalogRead): FormState {
  return {
    standard_code: row.standard_code,
    colnect_url: row.colnect_url ?? '',
    issue_url: row.issue_url ?? '',
    catalog_system: row.catalog_system ?? '',
    catalog_ref: row.catalog_ref ?? '',
    design_no: row.design_no ?? '',
    design_name: row.design_name ?? '',
    issue_name: row.issue_name ?? '',
    face_value: row.face_value ?? '',
    currency: row.currency ?? '',
    issue_date: row.issue_date ?? '',
    note: row.note ?? '',
    source_state: row.source_state,
  };
}

function payloadOf(form: FormState, updating: boolean): StampCatalogCreate | StampCatalogUpdate {
  const base = {
    colnect_url: form.colnect_url.trim() || null,
    issue_url: form.issue_url.trim() || null,
    catalog_system: (form.catalog_system || null) as StampCatalogCreate['catalog_system'],
    catalog_ref: form.catalog_ref.trim() || null,
    design_no: form.design_no.trim() || null,
    design_name: form.design_name.trim() || null,
    issue_name: form.issue_name.trim() || null,
    face_value: form.face_value.trim() || null,
    currency: form.currency.trim() || null,
    issue_date: form.issue_date || null,
    note: form.note.trim() || null,
  };
  if (updating) return { ...base, source_state: form.source_state as StampCatalogUpdate['source_state'] };
  return { ...base, standard_code: form.standard_code.trim() || null };
}

function CatalogForm({
  initial,
  onClose,
}: {
  initial: StampCatalogRead | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { vocab } = useVocabSafe();
  const updating = initial !== null;
  const [form, setForm] = useState<FormState>(initial ? formFrom(initial) : emptyForm());

  const mutation = useMutation({
    mutationFn: () =>
      updating
        ? api.stampCatalog.update(initial.id, payloadOf(form, true) as StampCatalogUpdate)
        : api.stampCatalog.create(payloadOf(form, false) as StampCatalogCreate),
    onSuccess: () => {
      toast.success(updating ? '标准票已更新' : '标准票已创建');
      void queryClient.invalidateQueries({ queryKey: ['stamp-catalog'] });
      onClose();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const patch = (next: Partial<FormState>) => setForm({ ...form, ...next });

  const [selectedDesign, setSelectedDesign] = useState(initial?.design_no ?? '');

  const live = useQuery({
    queryKey: ['stamp-catalog', 'detail', initial?.id ?? 'none'],
    queryFn: () => {
      if (!initial) throw new Error('新建时没有详情');
      return api.stampCatalog.get(initial.id);
    },
    enabled: updating,
    initialData: initial ?? undefined,
    refetchInterval: (query) => {
      const status = query.state.data?.fetch_status;
      return status === 'queued' || status === 'running' ? 1500 : false;
    },
  });
  const row = live.data ?? initial;
  const lastParse = lastParseOf(row);
  const fetchStatus = row?.fetch_status ?? 'idle';

  const refresh = useMutation({
    mutationFn: () => {
      if (!initial) throw new Error('新建时不能抓取');
      return api.stampCatalog.refresh(initial.id);
    },
    onSuccess: (data) => {
      queryClient.setQueryData(['stamp-catalog', 'detail', data.id], data);
      toast.success('已开始抓取；完成后会在下面显示解析结果');
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const apply = useMutation({
    mutationFn: () => {
      if (!initial) throw new Error('新建时不能应用解析');
      return api.stampCatalog.applyParse(initial.id, selectedDesign || null);
    },
    onSuccess: () => {
      toast.success('已应用解析结果；状态变为「部分字段」，请复核后设为已确认');
      void queryClient.invalidateQueries({ queryKey: ['stamp-catalog'] });
      onClose();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  return (
    <Modal
      open
      title={updating ? `编辑标准票 · ${initial.standard_code}` : '新建标准票'}
      size="wide"
      onClose={onClose}
      footer={
        <>
          <span className="small faint grow">
            3 列是身份锚点；图序/图名等详细字段可留空，之后补。
          </span>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {updating && row ? (
        <section className="catalog-fetch">
          <div className="catalog-fetch__head">
            <div>
              <strong>来源抓取</strong>
              <div className="small muted">
                从「原始发行网址」抓官方页；解析结果先复核，再应用。
              </div>
            </div>
            <button
              type="button"
              className="btn btn--sm btn--primary"
              disabled={
                !row.issue_url ||
                refresh.isPending ||
                fetchStatus === 'queued' ||
                fetchStatus === 'running'
              }
              onClick={() => refresh.mutate()}
            >
              {fetchStatus === 'queued' || fetchStatus === 'running'
                ? '抓取中…'
                : refresh.isPending
                  ? '提交中…'
                  : '抓取并解析'}
            </button>
          </div>

          {!row.issue_url ? (
            <div className="small muted">先在下面填写「原始发行网址」，再抓取。</div>
          ) : null}
          {fetchStatus === 'queued' || fetchStatus === 'running' ? (
            <div className="small muted">后台抓取中；完成后这里会自动出现解析结果。</div>
          ) : null}
          {fetchStatus === 'error' ? (
            <div className="catalog-fetch__error">
              抓取失败：{row.fetch_error ?? '未知错误'}（旧字段/旧快照未动，可重试）
            </div>
          ) : null}

          {lastParse ? (
            <div className="catalog-parse">
              <div className="catalog-parse__meta">
                <span>
                  解析版本 <code className="mono">{lastParse.parse_version}</code>
                </span>
                {lastParse.fetched_at ? (
                  <span>抓取于 {lastParse.fetched_at.slice(0, 19).replace('T', ' ')}</span>
                ) : null}
                {lastParse.snapshot_sha256 ? (
                  <span title={lastParse.snapshot_sha256}>
                    快照 {lastParse.snapshot_sha256.slice(0, 10)}…
                  </span>
                ) : null}
                {lastParse.final_url ? (
                  <a href={lastParse.final_url} target="_blank" rel="noreferrer">
                    打开来源页
                  </a>
                ) : null}
              </div>

              <div className="catalog-parse__grid">
                <div>
                  <span className="small muted">套名</span>
                  <strong>{lastParse.issue_name ?? '—'}</strong>
                </div>
                <div>
                  <span className="small muted">志号</span>
                  <strong>{lastParse.catalog_ref ?? '—'}</strong>
                </div>
                <div>
                  <span className="small muted">发行日</span>
                  <strong>{lastParse.issue_date ?? '—'}</strong>
                </div>
              </div>

              {lastParse.designs.length > 0 ? (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th style={{ width: 34 }} />
                      <th>图序</th>
                      <th>图名</th>
                      <th>面值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lastParse.designs.map((design, index) => {
                      const value = design.design_no ?? `design-${index}`;
                      const checked =
                        selectedDesign === value ||
                        (lastParse.designs.length === 1 && !selectedDesign);
                      return (
                        <tr
                          key={value}
                          onClick={() => setSelectedDesign(value)}
                          style={{ cursor: 'pointer' }}
                          className={checked ? 'is-selected' : undefined}
                        >
                          <td>
                            <input
                              type="radio"
                              name="catalog-design"
                              checked={checked}
                              onChange={() => setSelectedDesign(value)}
                            />
                          </td>
                          <td className="mono">{design.design_no ?? '—'}</td>
                          <td>{design.design_name ?? '—'}</td>
                          <td className="mono">
                            {design.face_value
                              ? faceText(design.face_value, design.currency)
                              : '—'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <div className="small muted">没解析到“图序/图名/面值”表；仍可手工补。</div>
              )}

              {Object.keys(lastParse.details).length > 0 ? (
                <div className="catalog-parse__details small muted">
                  {Object.entries(lastParse.details).map(([key, value]) => (
                    <span key={key}>
                      {parseLabel(key)}: {value}
                    </span>
                  ))}
                </div>
              ) : null}

              {Object.keys(lastParse.field_sources).length > 0 ? (
                <details className="small muted" style={{ marginTop: 6 }}>
                  <summary>字段来源 / 置信度</summary>
                  <div className="catalog-parse__details">
                    {Object.entries(lastParse.field_sources).map(([key, source]) => (
                      <span key={key}>
                        {parseLabel(key)}: {source.source} · {source.confidence}
                      </span>
                    ))}
                  </div>
                </details>
              ) : null}

              <div className="catalog-fetch__actions">
                <span className="small faint">
                  应用只写非空字段；状态变为「部分字段」，确认后再设为「已确认」。
                </span>
                <button
                  type="button"
                  className="btn btn--sm btn--primary"
                  disabled={
                    apply.isPending ||
                    lastParse.designs.length === 0 ||
                    (lastParse.designs.length > 1 && !selectedDesign)
                  }
                  onClick={() => apply.mutate()}
                >
                  {apply.isPending ? '应用中…' : '应用解析结果'}
                </button>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      <div className="form-section">
        <Field
          label="标准编号"
          hint="人读编号；不填自动生成 sc-xxxxxxxx。生成后不再改。"
        >
          <input
            className="input mono"
            value={form.standard_code}
            disabled={updating}
            onChange={(event) => patch({ standard_code: event.target.value })}
            placeholder="cn-2023-1-d02"
          />
        </Field>

        <Field label="Colnect 网址" hint="以图搜票结果的条目页">
          <input
            className="input"
            value={form.colnect_url}
            onChange={(event) => patch({ colnect_url: event.target.value })}
            placeholder="https://colnect.com/zt/stamps/stamp/…"
          />
        </Field>

        <Field label="原始发行网址" hint="官方发行页；一套多枚可以重复填同一个 URL">
          <input
            className="input"
            value={form.issue_url}
            onChange={(event) => patch({ issue_url: event.target.value })}
            placeholder="https://cpi.chinapost.com.cn/jy/stamp/…"
          />
        </Field>
      </div>

      <div className="form-grid form-grid--3">
        <Field label="目录体系">
          <Select
            value={form.catalog_system}
            onChange={(value) => patch({ catalog_system: value })}
            options={vocab.catalog_systems}
            placeholder="未指定"
          />
        </Field>
        <Field label="志号">
          <input
            className="input"
            value={form.catalog_ref}
            onChange={(event) => patch({ catalog_ref: event.target.value })}
            placeholder="普30 / 2023-1"
          />
        </Field>
        <Field label="图序">
          <input
            className="input"
            value={form.design_no}
            onChange={(event) => patch({ design_no: event.target.value })}
            placeholder="(2-1)"
          />
        </Field>
      </div>

      <div className="form-grid form-grid--2">
        <Field label="图名" hint="单枚图案名">
          <input
            className="input"
            value={form.design_name}
            onChange={(event) => patch({ design_name: event.target.value })}
            placeholder="保护森林"
          />
        </Field>
        <Field label="套名" hint="这套票的名称">
          <input
            className="input"
            value={form.issue_name}
            onChange={(event) => patch({ issue_name: event.target.value })}
            placeholder="癸卯年"
          />
        </Field>
      </div>

      <div className="form-grid form-grid--3">
        <Field label="面值" hint="字符串，不转浮点">
          <input
            className="input mono"
            value={form.face_value}
            onChange={(event) => patch({ face_value: event.target.value })}
            placeholder="0.40"
          />
        </Field>
        <Field label="币种">
          <input
            className="input mono"
            maxLength={3}
            value={form.currency}
            onChange={(event) => patch({ currency: event.target.value.toUpperCase() })}
            placeholder="CNY"
          />
        </Field>
        <Field label="发行日期">
          <DateInput
            value={form.issue_date}
            onChange={(value) => patch({ issue_date: value })}
          />
        </Field>
      </div>

      <div className="form-grid form-grid--2">
        <Field label="状态" hint="人工确认后才算「已确认」">
          <Select
            value={form.source_state}
            onChange={(value) => patch({ source_state: value })}
            options={STATE_OPTIONS}
          />
        </Field>
        <Field label="备注">
          <input
            className="input"
            value={form.note}
            onChange={(event) => patch({ note: event.target.value })}
          />
        </Field>
      </div>
    </Modal>
  );
}

function BackfillDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const preview = useQuery({
    queryKey: ['stamp-catalog', 'backfill'],
    queryFn: () => api.stampCatalog.backfillPreview(),
  });
  const apply = useMutation({
    mutationFn: () => api.stampCatalog.backfillApply(),
    onSuccess: (result) => {
      toast.success(
        `回填完成：新建 ${result.created} 条，关联 ${result.linked} 枚，跳过 ${result.skipped} 枚`,
      );
      void queryClient.invalidateQueries({ queryKey: ['stamp-catalog'] });
      onClose();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const groups = preview.data?.groups ?? [];
  const actionable = groups.some((group) => group.action !== 'manual');

  return (
    <Modal
      open
      title="批量回填"
      size="wide"
      onClose={onClose}
      footer={
        <>
          <span className="small faint grow">
            只处理「未关联且有志号」的票；没有志号的不猜，多个候选的跳过。
          </span>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={!actionable || apply.isPending}
            onClick={() => apply.mutate()}
          >
            {apply.isPending ? '回填中…' : '应用回填'}
          </button>
        </>
      }
    >
      {preview.isLoading ? <LoadingState text="扫描未关联票…" /> : null}
      {preview.isError ? (
        <ErrorState error={preview.error} onRetry={() => void preview.refetch()} />
      ) : null}
      {preview.data ? (
        <div className="col" style={{ gap: 10 }}>
          <div className="small muted">
            共 {preview.data.total_stamps} 枚未关联票，分成 {groups.length} 组。
          </div>
          {groups.length === 0 ? (
            <div className="small muted">
              没有可回填的票：要么都已关联，要么这些票没有志号（留着手工补）。
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>志号</th>
                  <th>票数</th>
                  <th>动作</th>
                  <th>目标标准票</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <tr key={group.key}>
                    <td className="mono">
                      {group.catalog_ref}
                      <div className="small faint">{group.catalog_system ?? '未指定体系'}</div>
                    </td>
                    <td>{group.stamp_count}</td>
                    <td>
                      {group.action === 'link'
                        ? '关联已有'
                        : group.action === 'create'
                          ? '新建草稿'
                          : '跳过'}
                      {group.reason ? <div className="small faint">{group.reason}</div> : null}
                    </td>
                    <td className="mono">
                      {group.standard_code ?? (group.action === 'create' ? 'sc-…（新建）' : '—')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
    </Modal>
  );
}

function DuplicateRow({ group, onDone }: { group: DuplicateGroup; onDone: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState(group.entries[0]?.id ?? '');
  const merge = useMutation({
    mutationFn: () =>
      api.stampCatalog.merge(
        target as StampCatalogRead['id'],
        group.entries.filter((entry) => entry.id !== target).map((entry) => entry.id),
      ),
    onSuccess: () => {
      toast.success('已合并重复标准票');
      void queryClient.invalidateQueries({ queryKey: ['stamp-catalog'] });
      onDone();
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  return (
    <div className="catalog-fetch">
      <div className="small muted">{group.reason}</div>
      <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
        <select
          className="select"
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        >
          {group.entries.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.standard_code} · {entry.catalog_ref ?? '—'} · {entry.design_name ?? '—'}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn btn--sm btn--primary"
          disabled={merge.isPending || group.entries.length < 2}
          onClick={() => merge.mutate()}
        >
          {merge.isPending ? '合并中…' : '合并到选中'}
        </button>
      </div>
      <div className="small faint" style={{ marginTop: 6 }}>
        其余条目会删除；关联的用户票会改指到选中条目。
      </div>
    </div>
  );
}

function DuplicatesDialog({ onClose }: { onClose: () => void }) {
  const duplicates = useQuery({
    queryKey: ['stamp-catalog', 'duplicates'],
    queryFn: () => api.stampCatalog.duplicates(),
  });

  return (
    <Modal open title="重复标准票" size="wide" onClose={onClose}>
      {duplicates.isLoading ? <LoadingState text="检查重复…" /> : null}
      {duplicates.isError ? (
        <ErrorState error={duplicates.error} onRetry={() => void duplicates.refetch()} />
      ) : null}
      {duplicates.data ? (
        duplicates.data.length === 0 ? (
          <div className="small muted">没有发现重复条目。</div>
        ) : (
          <div className="col" style={{ gap: 10 }}>
            {duplicates.data.map((group) => (
              <DuplicateRow key={group.key} group={group} onDone={onClose} />
            ))}
          </div>
        )
      ) : null}
    </Modal>
  );
}

export function StampCatalogPanel() {
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<StampCatalogRead | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [tool, setTool] = useState<'backfill' | 'duplicates' | null>(null);
  const debounced = useDebouncedValue(q, 300);

  const list = useQuery({
    queryKey: ['stamp-catalog', 'list', debounced, page],
    queryFn: () => api.stampCatalog.list({ q: debounced, page, size: PAGE_SIZE }),
    placeholderData: (previous) => previous,
  });

  const unlinked = useQuery({
    queryKey: ['stamp-catalog', 'unlinked'],
    queryFn: () => api.stampCatalog.unlinkedStamps({ size: 50 }),
  });

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;

  const openNew = () => {
    setEditing(null);
    setFormOpen(true);
  };
  const openEdit = (row: StampCatalogRead) => {
    setEditing(row);
    setFormOpen(true);
  };

  const unlinkedItems = useMemo(() => unlinked.data?.items ?? [], [unlinked.data]);

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="toolbar">
        <div className="toolbar__row">
          <Field label="搜索" className="field--wide">
            <input
              className="input"
              placeholder="标准编号 / 志号 / 图名 / 套名"
              value={q}
              onChange={(event) => {
                setQ(event.target.value);
                setPage(1);
              }}
            />
          </Field>
          <div className="toolbar__spacer" />
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            <a className="btn btn--sm" href="/api/stamp-catalog/export?format=csv">
              导出 CSV
            </a>
            <a className="btn btn--sm" href="/api/stamp-catalog/export?format=json">
              导出 JSON
            </a>
            <button type="button" className="btn btn--sm" onClick={() => setTool('backfill')}>
              批量回填
            </button>
            <button type="button" className="btn btn--sm" onClick={() => setTool('duplicates')}>
              重复合并
            </button>
            <button type="button" className="btn btn--primary" onClick={openNew}>
              ＋ 新建标准票
            </button>
          </div>
        </div>
      </div>

      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">标准票</div>
          <div className="small muted">
            共 {total} 条。手工维护 3 列即可；志号/图序/图名等可后补。
          </div>
        </div>
        <div className="panel__body panel__body--flush">
          {list.isLoading ? <LoadingState text="加载标准库…" /> : null}
          {list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : null}
          {!list.isLoading && !list.isError && items.length === 0 ? (
            <EmptyState
              icon="📚"
              title={debounced ? '没有匹配的标准票' : '标准票库还是空的'}
              text="从一张票的「标准票」字段新建，或在这里先建一条再关联。"
              action={
                <button type="button" className="btn btn--primary" onClick={openNew}>
                  ＋ 新建标准票
                </button>
              }
            />
          ) : null}
          {items.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>标准编号</th>
                  <th>志号 / 图序</th>
                  <th>图名 / 套名</th>
                  <th>面值</th>
                  <th>状态</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <td className="mono nowrap">{row.standard_code}</td>
                    <td>
                      {row.catalog_ref ?? '—'}
                      {row.design_no ? ` · ${row.design_no}` : ''}
                    </td>
                    <td>
                      {row.design_name ?? row.issue_name ?? '—'}
                      <div className="small faint">
                        {row.colnect_url ? (
                          <a href={row.colnect_url} target="_blank" rel="noreferrer">
                            Colnect
                          </a>
                        ) : null}
                        {row.colnect_url && row.issue_url ? ' · ' : null}
                        {row.issue_url ? (
                          <a href={row.issue_url} target="_blank" rel="noreferrer">
                            发行页
                          </a>
                        ) : null}
                      </div>
                    </td>
                    <td className="mono nowrap">
                      {row.face_value ? faceText(row.face_value, row.currency) : '—'}
                    </td>
                    <td>
                      <span className={`badge badge--${row.source_state === 'verified' ? 'ok' : 'warn'}`}>
                        {STATE_LABEL[row.source_state] ?? row.source_state}
                      </span>
                    </td>
                    <td className="right">
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => openEdit(row)}
                      >
                        编辑
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {total > PAGE_SIZE ? (
            <div style={{ padding: '0 12px' }}>
              <Pagination
                page={page}
                size={PAGE_SIZE}
                total={total}
                onPage={setPage}
              />
            </div>
          ) : null}
        </div>
      </section>

      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">未关联的用户票</div>
          <div className="small muted">
            {unlinked.data?.total ?? 0} 枚票还没有标准票（票墙「未编目」的补录队列）
          </div>
        </div>
        <div className="panel__body panel__body--flush">
          {unlinked.isLoading ? <div className="small muted">加载中…</div> : null}
          {unlinked.isError ? (
            <div className="small" style={{ color: 'var(--danger)' }}>
              未关联清单加载失败
            </div>
          ) : null}
          {!unlinked.isLoading && !unlinked.isError && unlinkedItems.length === 0 ? (
            <div className="small faint" style={{ padding: 12 }}>
              所有有目录编号的票都已关联标准票。
            </div>
          ) : null}
          {unlinkedItems.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>票</th>
                  <th>所属卡片</th>
                  <th>现有字段</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {unlinkedItems.map((row) => (
                  <tr key={row.stamp_id}>
                    <td>{row.name ?? '未命名邮票'}</td>
                    <td>
                      {row.card_visit_date}
                      {row.card_title ? ` · ${row.card_title}` : ''}
                    </td>
                    <td className="small muted">
                      {[row.catalog_ref, row.face_value, row.currency].filter(Boolean).join(' · ') || '—'}
                    </td>
                    <td className="right">
                      <Link
                        className="btn btn--sm"
                        to={`/cards/${row.card_id}?stamp=${row.stamp_id}`}
                      >
                        去关联
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
      </section>

      {formOpen ? (
        <CatalogForm
          key={editing?.id ?? 'new'}
          initial={editing}
          onClose={() => setFormOpen(false)}
        />
      ) : null}
      {tool === 'backfill' ? <BackfillDialog onClose={() => setTool(null)} /> : null}
      {tool === 'duplicates' ? <DuplicatesDialog onClose={() => setTool(null)} /> : null}
    </div>
  );
}
