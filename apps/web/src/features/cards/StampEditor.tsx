import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { DateInput } from '../../shared/components/DateInput';
import { Field } from '../../shared/components/Field';
import { Select } from '../../shared/components/Select';
import { useToast } from '../../shared/components/Toast';
import { composeStampTitle } from '../../shared/format';
import { useVocabSafe } from '../../shared/vocab';
import { AssetUploader } from './AssetUploader';
import { StampIdentifyModal } from './StampIdentifyModal';
import type { StampDraft } from './draft';
import type { StampRead } from '../../shared/types';

interface StampEditorProps {
  draft: StampDraft;
  index: number;
  /** 已有 id 的票挂一个 DOM id，供 `?stamp=` 深链定位（票墙 S?）。 */
  domId?: string;
  onChange: (next: StampDraft) => void;
  onRemove: () => void;
  disabled?: boolean;
}

/**
 * 单枚邮票。
 *
 * 面值**刻意不做硬约束**（N9）：用户不知道别人会贴什么票，硬约束会让开源后的
 * 用户录不进去。这里用 `<datalist>` 给候选（用户常用面值排在前面），
 * 但允许输入任何值。
 */
export function StampEditor({
  draft,
  index,
  domId,
  onChange,
  onRemove,
  disabled,
}: StampEditorProps) {
  const { vocab } = useVocabSafe();
  const [expanded, setExpanded] = useState(true);
  const [identifyOpen, setIdentifyOpen] = useState(false);
  const queryClient = useQueryClient();
  const toast = useToast();

  const catalogQuery = useQuery({
    queryKey: ['stamp-catalog', 'options'],
    queryFn: () => api.stampCatalog.list({ size: 200 }),
    staleTime: 60_000,
  });
  const liveStampQuery = useQuery({
    queryKey: ['stamp', draft.id],
    queryFn: () => api.stamps.get(draft.id as string),
    enabled: Boolean(draft.id),
    refetchInterval: (query) => {
      const status = query.state.data?.identify_status;
      return status === 'queued' || status === 'running' ? 1500 : false;
    },
  });
  const liveStamp = liveStampQuery.data;
  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    staleTime: 60_000,
  });
  // 严格等于 false 才关：老后端没有这个字段，滚动升级期间不该把按钮锁死。
  const identifyServiceOff = healthQuery.data?.stamp_identify?.enabled === false;
  const identify = useMutation({
    mutationFn: (force: boolean) => api.stamps.identify(draft.id as string, force),
    onSuccess: (data) => {
      queryClient.setQueryData(['stamp', data.id], data);
      setIdentifyOpen(true);
    },
    onError: (error) => toast.error(toApiError(error).message),
  });
  const catalogItems = catalogQuery.data?.items ?? [];
  const selectedCatalog = catalogItems.find((item) => item.id === draft.catalog_id) ?? null;
  const catalogLinked = draft.catalog_id !== '';
  const catalogOptions = catalogItems.map((item) => ({
    value: item.id,
    label: [item.standard_code, item.catalog_ref, item.design_name ?? item.issue_name]
      .filter(Boolean)
      .join(' · '),
  }));

  const patch = (next: Partial<StampDraft>) => onChange({ ...draft, ...next });

  const identifyBusy =
    identify.isPending ||
    liveStamp?.identify_status === 'queued' ||
    liveStamp?.identify_status === 'running';
  const candidates = liveStamp?.identify_candidates ?? [];
  const canIdentify = Boolean(draft.id && draft.image_asset_id) && !disabled && !identifyServiceOff;
  const openIdentify = () => {
    if (liveStamp?.identify_status === 'ok' && candidates.length > 0) {
      setIdentifyOpen(true);
      return;
    }
    // 没有候选/上次失败时强制重搜；idle 走普通搜索
    identify.mutate(liveStamp?.identify_status === 'ok');
  };
  const onAppliedCandidate = (stamp: StampRead) => {
    queryClient.setQueryData(['stamp', stamp.id], stamp);
    onChange({
      ...draft,
      catalog_id: stamp.catalog_id ?? '',
      catalog_ref: stamp.catalog_ref ?? '',
      catalog_system: stamp.catalog_system ?? '',
      name: stamp.name ?? '',
      face_value: stamp.face_value ?? '',
      currency: stamp.currency ?? 'CNY',
      issue_date: stamp.issue_date ?? '',
    });
    void queryClient.invalidateQueries({ queryKey: ['stamp-catalog'] });
  };

  const title = composeStampTitle({
    name: draft.name,
    catalogRef: draft.catalog_ref,
    faceValue: draft.face_value,
    currency: draft.currency,
  });

  const listId = `face-values-${draft.key}`;

  const onFaceValueChange = (value: string) => {
    const hit = vocab.face_value_suggestions.find((item) => item.value === value);
    // 选了候选就顺手把币种补上；手输的话不动用户已经填的币种
    if (hit && draft.currency.trim() === '') {
      patch({ face_value: value, currency: hit.currency });
      return;
    }
    patch({ face_value: value });
  };

  return (
    <div id={domId} className={`sub-card${expanded ? '' : ' sub-card--collapsed'}`}>
      <div className="sub-card__head">
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          title={expanded ? '收起' : '展开'}
        >
          {expanded ? '▼' : '▶'}
        </button>

        <div className="sub-card__title">
          <span>邮票 {index + 1}</span>
          {draft.id === null ? <span className="badge badge--warn">新增</span> : null}
          <span className="small muted nowrap">{title}</span>
        </div>

        <div className="sub-card__actions">
          <button
            type="button"
            className="btn btn--sm btn--danger-ghost"
            onClick={onRemove}
            disabled={disabled}
          >
            删除
          </button>
        </div>
      </div>

      <div className="sub-card__body">
        <Field
          label="标准票"
          hint="关联后票墙按标准票的志号/图名分组；不关联就会留在「未编目」"
        >
          <div className="row" style={{ gap: 6 }}>
            <div className="grow">
              <Select
                value={draft.catalog_id}
                onChange={(value) => patch({ catalog_id: value })}
                options={catalogOptions}
                placeholder={catalogItems.length === 0 ? '还没有标准票，先去新建' : '未关联标准票'}
                disabled={disabled}
              />
            </div>
            <Link className="btn btn--sm" to="/dicts?tab=catalog" target="_blank">
              标准库
            </Link>
          </div>
          {selectedCatalog ? (
            <div className="small muted">
              已关联 <code className="mono">{selectedCatalog.standard_code}</code>
              {selectedCatalog.catalog_ref ? ` · 志号 ${selectedCatalog.catalog_ref}` : ''}
              {selectedCatalog.design_no ? ` · 图序 ${selectedCatalog.design_no}` : ''}
              ；志号/票名/面值等由标准票维护
            </div>
          ) : null}
        </Field>

        <div className="form-grid form-grid--2">
          <Field
            label="志号"
            hint={
              catalogLinked
                ? '由标准票维护；改标准票即可'
                : '中国志号形如 2023-1，或普通邮票的「普30」'
            }
            htmlFor={`stamp-ref-${draft.key}`}
          >
            <input
              id={`stamp-ref-${draft.key}`}
              className="input"
              value={draft.catalog_ref}
              disabled={disabled || catalogLinked}
              onChange={(event) => patch({ catalog_ref: event.target.value })}
              placeholder="普30"
              autoComplete="off"
            />
          </Field>

          <Field label="目录体系">
            <Select
              value={draft.catalog_system}
              onChange={(value) => patch({ catalog_system: value })}
              options={vocab.catalog_systems}
              disabled={disabled || catalogLinked}
            />
          </Field>
        </div>

        <Field label="票名" hint={catalogLinked ? '由标准票的图名/套名维护' : undefined}>
          <input
            className="input"
            value={draft.name}
            disabled={disabled || catalogLinked}
            onChange={(event) => patch({ name: event.target.value })}
            placeholder="保护森林"
            autoComplete="off"
          />
        </Field>

        <div className="form-grid form-grid--3">
          <Field label="面值" hint="字符串保存，不做浮点运算">
            <input
              className="input mono"
              list={listId}
              value={draft.face_value}
              disabled={disabled || catalogLinked}
              onChange={(event) => onFaceValueChange(event.target.value)}
              placeholder="0.10"
              autoComplete="off"
            />
            <datalist id={listId}>
              {vocab.face_value_suggestions.map((item) => (
                <option key={`${item.value}-${item.currency}`} value={item.value}>
                  {item.value} {item.currency}
                </option>
              ))}
            </datalist>
          </Field>

          <Field label="币种" hint="ISO 4217 三位代码，后端会自动转大写">
            <input
              className="input mono"
              value={draft.currency}
              maxLength={3}
              disabled={disabled || catalogLinked}
              onChange={(event) => patch({ currency: event.target.value.toUpperCase() })}
              placeholder="CNY"
            />
          </Field>

          <Field label="发行日期">
            <DateInput
              value={draft.issue_date}
              onChange={(value) => patch({ issue_date: value })}
              disabled={disabled || catalogLinked}
            />
          </Field>
        </div>

        <Field label="票图" hint="这也会成为将来「以图搜票」的底库样本">
          <AssetUploader
            assetId={draft.image_asset_id}
            onChange={(assetId) => patch({ image_asset_id: assetId })}
            title="邮票图"
            hint="拖拽或点击上传"
            compact
          />
        </Field>

        <Field
          label="识别"
          hint={
            identifyServiceOff
              ? '本实例未启用「以图搜票」——它依赖外部 colnetApi'
              : draft.catalog_id
                ? '已关联标准票；重新识别只给候选，应用时确认才替换'
                : '用 Colnect 以图搜票找候选；选中后再建/选标准票'
          }
        >
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn--sm"
              disabled={!canIdentify || identifyBusy}
              title={identifyServiceOff ? '未启用邮票识别服务（COLNET_ENABLED=false）' : undefined}
              onClick={openIdentify}
            >
              {identifyBusy
                ? '识别中…'
                : liveStamp?.identify_status === 'ok' && candidates.length > 0
                  ? `查看候选（${candidates.length}）`
                  : liveStamp?.identify_status === 'error'
                    ? '重试识别'
                    : '识别邮票'}
            </button>
            {identifyServiceOff ? (
              <span className="small faint">
                需要外部 colnetApi；见 README「可选：邮票识别」
              </span>
            ) : !draft.id ? (
              <span className="small faint">先保存卡片，再识别</span>
            ) : !draft.image_asset_id ? (
              <span className="small faint">先在电子卡「框出邮票」，或上传票图</span>
            ) : liveStamp?.identify_status === 'error' ? (
              <span className="small" style={{ color: 'var(--danger)' }}>
                {liveStamp.identify_error ?? '识别失败'}
              </span>
            ) : liveStamp?.identify_status === 'ok' && candidates.length > 0 ? (
              <span className="small muted">找到 {candidates.length} 个候选，需人工选</span>
            ) : null}
          </div>
        </Field>

        <Field label="备注">
          <textarea
            className="textarea"
            style={{ minHeight: 48 }}
            value={draft.note}
            disabled={disabled}
            onChange={(event) => patch({ note: event.target.value })}
            placeholder="例如：齿孔度 13 / 有轻微折痕 / 加盖…"
          />
        </Field>
      </div>
      {identifyOpen && liveStamp ? (
        <StampIdentifyModal
          stamp={liveStamp}
          onClose={() => setIdentifyOpen(false)}
          onRefresh={() => identify.mutate(true)}
          onApplied={onAppliedCandidate}
        />
      ) : null}
    </div>
  );
}
