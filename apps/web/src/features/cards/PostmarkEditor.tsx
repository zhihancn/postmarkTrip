import { useState } from 'react';

import { DateInput } from '../../shared/components/DateInput';
import { Field } from '../../shared/components/Field';
import { PlacePicker } from '../../shared/components/PlacePicker';
import { PostOfficePicker } from '../../shared/components/PostOfficePicker';
import { Select } from '../../shared/components/Select';
import { TagPicker } from '../../shared/components/TagPicker';
import { labelOf, prioritizeOptions, useVocabSafe } from '../../shared/vocab';
import { AssetUploader } from './AssetUploader';
import { applyPlaceChange, type PostmarkDraft } from './draft';

interface PostmarkEditorProps {
  draft: PostmarkDraft;
  index: number;
  total: number;
  /** 已有 id 的邮戳挂一个 DOM id，供 `?postmark=` 深链定位（戳墙 W5）。 */
  domId?: string;
  onChange: (next: PostmarkDraft) => void;
  onRemove: () => void;
  disabled?: boolean;
}

/**
 * 单条邮戳。**字段顺序按录入频率排**：戳面原文 → 日期 → 地点/邮局 → 其余。
 *
 * 时间码 / 戳号是实测出来的字段（need/03 §三.6）：底弧"建溪 1"里的"1"是戳号，
 * 日期里 "2026.09.25.15" 的 ".15" 是时间码，两者都不是装饰。
 */
export function PostmarkEditor({
  draft,
  index,
  total,
  domId,
  onChange,
  onRemove,
  disabled,
}: PostmarkEditorProps) {
  const { vocab } = useVocabSafe();
  const [expanded, setExpanded] = useState(true);

  const patch = (next: Partial<PostmarkDraft>) => onChange({ ...draft, ...next });

  const postmarkTypeOptions = prioritizeOptions(vocab.postmark_types, vocab.primary_postmark_types);
  const missingInscription = draft.inscription.trim() === '';

  const summaryBits = [
    labelOf(vocab.postmark_types, draft.postmark_type),
    draft.postmark_date || draft.postmark_date_text || '无日期',
    draft.place?.name ?? '',
    draft.post_office?.name ?? '',
  ].filter((item) => item !== '');

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
          <span>邮戳 {index + 1}</span>
          {draft.id === null ? <span className="badge badge--warn">新增</span> : null}
          {draft.is_color ? <span className="badge badge--color">彩色</span> : null}
          {draft.provenance !== 'real_scan' ? (
            <span className="badge badge--generated">
              {labelOf(vocab.provenances, draft.provenance)}
            </span>
          ) : null}
          <span className="small muted nowrap" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {missingInscription ? '（缺戳面原文）' : summaryBits.join(' · ')}
          </span>
        </div>

        <div className="sub-card__actions">
          <button
            type="button"
            className="btn btn--sm btn--danger-ghost"
            onClick={onRemove}
            disabled={disabled || total <= 1}
            title={total <= 1 ? '至少要保留一条邮戳' : '删除这条邮戳'}
          >
            删除
          </button>
        </div>
      </div>

      <div className="sub-card__body">
        <div className="form-grid form-grid--2">
          <Field label="戳种" htmlFor={`pm-type-${draft.key}`}>
            <Select
              id={`pm-type-${draft.key}`}
              value={draft.postmark_type}
              onChange={(value) => patch({ postmark_type: value })}
              options={postmarkTypeOptions}
              disabled={disabled}
            />
          </Field>

          <Field label="时间码" hint="日戳上日期后的那一截，如 2026.09.25.**15** 里的 15">
            <input
              className="input"
              value={draft.time_code}
              disabled={disabled}
              onChange={(event) => patch({ time_code: event.target.value })}
              placeholder="15"
            />
          </Field>
        </div>

        <Field
          label="戳面原文"
          required
          hint="戳上刻的字，**原样录入，不要规范化**。这是证据，也是列表里显示的内容。"
          error={missingInscription ? '必填 —— 它是唯一必填的文本字段' : null}
        >
          <input
            className="input"
            value={draft.inscription}
            disabled={disabled}
            onChange={(event) => patch({ inscription: event.target.value })}
            placeholder="如「福建南平」或「中国工农红军北上抗日先遣队纪念碑」"
            autoComplete="off"
          />
        </Field>

        <div className="form-grid form-grid--3">
          <Field label="戳面日期" hint="日戳的日期。可与跑戳日期不同（求戳信会差几周）">
            <DateInput
              value={draft.postmark_date}
              onChange={(value) => patch({ postmark_date: value })}
              disabled={disabled}
            />
          </Field>

          <Field label="日期原文" hint="实测有 3 种格式：2026.09.25.15 / 2026.10.01.09. / 2026.09.29">
            <input
              className="input mono"
              value={draft.postmark_date_text}
              disabled={disabled}
              onChange={(event) => patch({ postmark_date_text: event.target.value })}
              placeholder="2026.09.25.15"
            />
          </Field>

          <Field label="戳号" hint="底弧局名后的数字，如「建溪 1」里的 1">
            <input
              className="input"
              value={draft.postmark_no}
              disabled={disabled}
              onChange={(event) => patch({ postmark_no: event.target.value })}
              placeholder="1"
            />
          </Field>
        </div>

        <div className="form-grid form-grid--2">
          <Field label="地点" hint="结构化地点。可以留空 —— 戳面原文才是证据">
            <PlacePicker
              value={draft.place_id}
              initialSelected={draft.place}
              disabled={disabled}
              // 换地点会顺带处理已选的邮局 —— 规则本身在 applyPlaceChange 里，
              // 并有单元测试锁着（见 tests/draft.test.mjs）。
              onChange={(place) => patch(applyPlaceChange(draft, place))}
            />
          </Field>

          <Field label="邮局" hint="哪个邮局盖的">
            <PostOfficePicker
              value={draft.post_office_id}
              initialSelected={draft.post_office}
              disabled={disabled}
              onChange={(office) =>
                patch({ post_office_id: office ? office.id : '', post_office: office })
              }
            />
          </Field>
        </div>

        <div className="form-grid form-grid--3">
          <Field label="来源标记" hint="区分「我真跑过」与「生成美化」。默认统计只算真实扫描。">
            <Select
              value={draft.provenance}
              onChange={(value) => patch({ provenance: value as PostmarkDraft['provenance'] })}
              options={vocab.provenances}
              disabled={disabled}
            />
          </Field>

          <Field label="盖印质量" hint="对应需求里的「追求完美」—— 方便以后筛出想重盖的">
            <Select
              value={draft.is_clean === null ? '' : draft.is_clean ? 'yes' : 'no'}
              onChange={(value) => patch({ is_clean: value === '' ? null : value === 'yes' })}
              options={[
                { value: 'yes', label: '满意' },
                { value: 'no', label: '不满意（想重盖）' },
              ]}
              placeholder="未评价"
              disabled={disabled}
            />
          </Field>

          <Field label="套色层数" hint="套色戳由多色戳具依次叠加，这里记色数">
            <input
              className="input"
              type="number"
              min={1}
              max={12}
              value={draft.color_layers}
              disabled={disabled || !draft.is_color}
              onChange={(event) => patch({ color_layers: event.target.value })}
              placeholder={draft.is_color ? '如 3' : '先勾选「彩色戳」'}
            />
          </Field>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={draft.is_color}
            disabled={disabled}
            onChange={(event) =>
              patch({
                is_color: event.target.checked,
                color_layers: event.target.checked ? draft.color_layers : '',
              })
            }
          />
          彩色戳（**不要灰度化**——颜色本身是记录对象）
        </label>

        <Field label="标签">
          <TagPicker
            value={draft.tag_ids}
            onChange={(ids) => patch({ tag_ids: ids })}
            disabled={disabled}
          />
        </Field>

        <Field label="戳的特写图" hint="选填。整卡扫描图在卡片上，这里放单独拍的戳">
          <AssetUploader
            assetId={draft.image_asset_id}
            onChange={(assetId) => patch({ image_asset_id: assetId })}
            title="戳特写"
            hint="拖拽或点击上传；也可以先留空，之后补"
            compact
          />
        </Field>

        <Field label="备注">
          <textarea
            className="textarea"
            style={{ minHeight: 56 }}
            value={draft.note}
            disabled={disabled}
            onChange={(event) => patch({ note: event.target.value })}
            placeholder="例如：盖偏了 / 营业员说这枚戳下月更换 / 首次日戳…"
          />
        </Field>
      </div>
    </div>
  );
}
