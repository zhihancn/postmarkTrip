import { useMemo, useState } from 'react';

import { assetPreviewUrl } from '../../shared/api';
import { PlacePicker } from '../../shared/components/PlacePicker';
import { PostOfficePicker } from '../../shared/components/PostOfficePicker';
import { InlineSpinner } from '../../shared/components/States';
import type {
  Decision,
  PlaceRead,
  PostOfficeRead,
  RecognizedCardRead,
} from '../../shared/types';
import { CardZoom } from './CardZoom';
import {
  DECISION_ICON,
  DECISION_LABEL,
  CONFIRM_FIELDS,
  FIELD_ORDER,
  currentFieldValue,
  draftFieldOf,
  editedCountOf,
  fieldLabel,
  fieldOf,
  officeLabelOf,
  placePathOf,
  resolveCardOpen,
  summaryDecision,
} from './recognize';
import type { CardDraft, PageDraft, PageView } from './recognize';

/**
 * 摘要 chip 的颜色类。**只按 decision** —— 实测 OCR 给「福建南火」的置信度是
 * 0.90 而它是错的，按置信度上色会把错值显示成绿的。
 *
 * 人自己改过的字段显示绿色 + ✎：它是人给的，标成可疑会让人怀疑刚填的东西。
 * 机器原本的判断仍然留在展开区的"机器值"那一行里（那要进 `observation`）。
 */
function chipClass(decision: Decision, value: string, edited: boolean): string {
  if (edited) return 'confirm-chip confirm-chip--ok';
  if (value === '') return 'confirm-chip confirm-chip--none';
  return `confirm-chip confirm-chip--${decision}`;
}

interface CardRowProps {
  page: PageView;
  card: RecognizedCardRead;
  draft: CardDraft;
  open: boolean;
  busy: boolean;
  submitDisabled: boolean;
  onToggle: () => void;
  onEditField: (fieldName: string, value: string) => void;
  onVisitDate: (value: string) => void;
  onExclude: (excluded: boolean) => void;
  onConfirmOne: () => void;
  /** 打开"放大看这一张"的弹层（缩略图是它唯一的入口）。 */
  onZoom: () => void;
  /** 选定/清除结构化地点、邮局（`ok/15` q4）—— 复用卡片页那两个选择器。 */
  onPickPlace: (place: PlaceRead | null) => void;
  onPickOffice: (office: PostOfficeRead | null) => void;
}

function CardRow({
  page,
  card,
  draft,
  open,
  busy,
  submitDisabled,
  onToggle,
  onEditField,
  onVisitDate,
  onExclude,
  onConfirmOne,
  onZoom,
  onPickPlace,
  onPickOffice,
}: CardRowProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState('');

  const worst = summaryDecision(card);
  const editedCount = editedCountOf(draft);
  //: 识别给出的层级链（`place_path`）—— 后端一直在返回，界面以前没显示
  const placePath = placePathOf(card);

  // ⚠️ 下面对 `decision` 的用法有一个**已知的线上缺口**：`/api/recognize` 目前
  // 没把 `known_places` / `known_offices` 传给 `recognize_page`，所以这个 decision
  // **不含词表那道防线**（决定 4 的"高置信度且在词表里"）。表现是：
  // `office` 靠 0.85 的上限兜底（「福建南火」0.892 仍然黄），而 `place` 上限 0.92，
  // 一个 0.95 的错地名会被标成绿。界面上没法修 —— 要么后端接线，要么这里不许重算
  // （重算会是第二个真相来源）。详见 `docs/ok/12` §三之 6。
  /**
   * **当前值**：人的确认值优先，没改过就是机器值（纯函数在 `recognize.ts`，
   * 测试钉住"清空不许回落到机器值"）。
   *
   * ⚠️ 主行 chip 曾经一律读 `card`（**识别响应**），于是"改完展开区更正了、
   * 主行没变"（`ok/18` §3.2 用户观察 #3）。识别响应是**机器当初的看法**，
   * 它不该变（那是 L2 证据）；主行是**给人看的当前状态**，必须跟草稿走。
   */
  const currentValue = (name: string): string => currentFieldValue(card, draft, name);

  const chips: { name: string; text: string }[] = [
    { name: 'date', text: currentValue('date') },
    { name: 'place', text: currentValue('place') },
    //: 局名 = 局名+戳号（合成的那个标识，见 `officeLabelOf`）
    { name: 'office', text: officeLabelOf(currentValue('office'), currentValue('office_no')) },
  ];

  // 缩略图：复用页面预览图 + 该卡 bbox 的**背景裁切**，省一次请求。
  //
  // 背景尺寸用 `work_size`（而不是预览图的实际像素）：那样背景坐标就是
  // **工作尺度**坐标，与 quad/circle 同一个空间，正负位移直接可用。
  const [workW, workH] = page.work_size;
  const [bx, by, bw] = card.bbox;
  const thumbW = 46;
  const scale = thumbW / Math.max(1, bw);
  const thumbStyle = {
    backgroundImage: `url('${assetPreviewUrl(page.assetId)}')`,
    backgroundSize: `${workW * scale}px ${workH * scale}px`,
    backgroundPosition: `${-bx * scale}px ${-by * scale}px`,
  };

  const startEdit = (name: string, current: string) => {
    setEditing(name);
    setValue(current);
  };

  const commit = () => {
    if (editing === null) return;
    if (value !== (draftFieldOf(draft, editing)?.finalValue ?? '')) {
      onEditField(editing, value);
    }
    setEditing(null);
  };

  const detailFields = useMemo(() => {
    const order: string[] = [];
    for (const name of FIELD_ORDER) {
      if (card.fields.some((f) => f.field_name === name)) order.push(name);
    }
    for (const field of card.fields) {
      if (!order.includes(field.field_name)) order.push(field.field_name);
    }
    return order;
  }, [card.fields]);

  return (
    <div
      className={`confirm-card${worst === 'needs_review' ? ' confirm-card--susp' : ''}${
        worst === 'reject' ? ' confirm-card--bad' : ''
      }${draft.excluded ? ' confirm-card--excluded' : ''}${open ? ' confirm-card--open' : ''}`}
    >
      <div
        className="confirm-card__row"
        role="button"
        tabIndex={0}
        onClick={onToggle}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onToggle();
          }
        }}
      >
        <span className="confirm-card__caret" aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
        <span className="confirm-card__idx">{card.index}</span>
        {/*
          缩略图**必须是自己的点击目标**（`ok/15` q1）：
          整行是 role="button" + onClick=折叠，缩略图原来只是个 <span>，
          于是点它等于折叠，没有任何"看大图"的入口。这里 stopPropagation
          之后开放大弹层。
        */}
        <button
          type="button"
          className="confirm-card__thumb"
          style={thumbStyle}
          aria-label={`放大看第 ${card.index + 1} 张`}
          title="点击放大（原始件裁切：邮戳 / 整卡）"
          onClick={(event) => {
            event.stopPropagation();
            onZoom();
          }}
        />

        <span className="confirm-chips">
          {chips.map((chip) => {
            const field = fieldOf(card, chip.name);
            const fieldDraft = draftFieldOf(draft, chip.name);
            const decision = (field?.decision ?? 'needs_review') as Decision;
            const isEdited = fieldDraft?.edited ?? false;
            return (
              <button
                key={chip.name}
                type="button"
                className={chipClass(decision, chip.text, isEdited)}
                title={field?.reason ?? ''}
                onClick={(event) => {
                  event.stopPropagation();
                  startEdit(chip.name, fieldDraft?.finalValue ?? chip.text);
                }}
              >
                <span className="confirm-chip__k">{fieldLabel(chip.name)}</span>
                <b>{chip.text === '' ? '（空）' : chip.text}</b>
                {isEdited ? <span className="confirm-chip__pen">✎</span> : null}
              </button>
            );
          })}
          {editedCount > 0 ? (
            <span className="badge badge--primary" title="这张卡上人工改过的字段数">
              改 {editedCount}
            </span>
          ) : null}
        </span>

        <span className="confirm-card__side">
          {card.postmark_type ? <span className="badge">{card.postmark_type}</span> : null}
          {card.field_source.startsWith('vlm') ? (
            <span
              className="badge badge--primary"
              title={
                '这一张的字段来自云端 —— 重跑不会得到同样的结果。' +
                (card.field_source === 'vlm_fallback'
                  ? '（走的是**回退**通道：整卡 JSON 与本地都没拿到日期）'
                  : '（走的是**整卡一次 JSON** 通道）')
              }
            >
              云端{card.field_source === 'vlm_fallback' ? '·回退' : ''}
            </span>
          ) : null}
          {card.size_suspect ? (
            <span className="badge badge--warn" title="卡的实际尺寸与期望卡型差得较远。异形卡是合法的，所以只标注">
              尺寸可疑
            </span>
          ) : null}
          {/*
            几何来自云端（`ok/20`）：这张卡的**四个角**是云端给的，不是本地切分。
            ⚠️ 与上面的「云端」字段标记是**两件事**：字段来自云端时几何可能仍是
            本地的。这里标的是几何 —— 一张歪的卡图不会报错，只会让后面所有读数
            都跟着歪，所以它比字段来源更值得看一眼。
          */}
          {card.geometry_source === 'vlm' ? (
            <span
              className="badge badge--warn"
              title={
                '这张卡的**四个角是云端给的**（本地切分那一版与之不一致）。' +
                '云端结果不可复现：同一张图重跑，框可能差几个像素。'
              }
            >
              几何·云端
            </span>
          ) : null}
          {card.circle && card.circle.radius_over_card_width > 0.4 ? (
            <span
              className="badge badge--danger"
              title="正常邮戳约 0.23 卡宽；明显更大说明圆可能抓错了东西（实测抓到过邮票上的圆形图案）"
            >
              圆可疑 {card.circle.radius_over_card_width.toFixed(2)}
            </span>
          ) : null}
          {draft.excluded ? <span className="badge badge--danger">不录入</span> : null}
        </span>
      </div>

      {open ? (
        <div className="confirm-card__detail">
          {editing !== null ? (
            <div className="confirm-editor">
              <span className="small muted">{fieldLabel(editing)}</span>
              <input
                className="input"
                autoFocus
                value={value}
                placeholder="（清空这一项）"
                onChange={(event) => setValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') commit();
                  if (event.key === 'Escape') setEditing(null);
                }}
              />
              <button type="button" className="btn btn--sm btn--primary" onClick={commit}>
                确定
              </button>
              <button type="button" className="btn btn--sm" onClick={() => setEditing(null)}>
                取消
              </button>
            </div>
          ) : null}

          <table className="confirm-table">
            <tbody>
              <tr>
                <td className="confirm-table__k">跑戳日</td>
                <td>
                  <input
                    type="date"
                    className={draft.visitDate === '' ? 'input input--invalid' : 'input'}
                    value={draft.visitDate}
                    onChange={(event) => onVisitDate(event.target.value)}
                  />
                  <div className="confirm-table__why">
                    {draft.visitDate === ''
                      ? '缺跑戳日 —— 服务端**不替用户编日期**（会污染按日期的统计），所以这张卡提交不了'
                      : '识别日期预填。它是人的确认值，不是识别输出'}
                  </div>
                </td>
              </tr>

              {detailFields.map((name) => {
                const field = card.fields.find((f) => f.field_name === name);
                if (!field) return null;
                const fieldDraft = draftFieldOf(draft, name);
                const finalValue = fieldDraft?.finalValue ?? field.value;
                const isEdited = fieldDraft?.edited ?? false;
                const inPayload = CONFIRM_FIELDS.includes(name);
                return (
                  <tr key={name}>
                    <td className="confirm-table__k">{fieldLabel(name)}</td>
                    <td>
                      <span
                        className="confirm-table__icon"
                        style={{ color: `var(--${decisionVar(field.decision)})` }}
                      >
                        {DECISION_ICON[field.decision] ?? '!'}
                      </span>
                      <button
                        type="button"
                        className="confirm-value"
                        onClick={() => startEdit(name, finalValue)}
                      >
                        {finalValue === '' ? '（空）' : finalValue}
                      </button>
                      <span className="confirm-table__conf">{field.confidence.toFixed(2)}</span>
                      <span className="small faint">{DECISION_LABEL[field.decision]}</span>
                      {isEdited ? <span className="confirm-table__edited">✎ 已改</span> : null}
                      <div className="confirm-table__why">{field.reason}</div>
                      {name === 'place' && placePath !== '' ? (
                        <div className="confirm-table__why">
                          词表层级：<b>{placePath}</b>
                          {' '}（只有最后一级是识别读到的，上面几级是数据库补的）
                        </div>
                      ) : null}
                      {isEdited ? (
                        <div className="confirm-table__why">
                          机器值：{field.value === '' ? '（空）' : field.value}
                          {' · '}
                          确认值会进 `observation`，两者不同时另写一条 `correction`
                        </div>
                      ) : null}
                      {!inPayload ? (
                        <div className="confirm-table__why">
                          这个字段**不提交**：确认端点只收
                          {' '}
                          {CONFIRM_FIELDS.join(' / ')}，避免前端自造的字段名混进 observation 档案
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}

              {card.queue.length > 0 ? (
                <tr>
                  <td className="confirm-table__k">校对队列</td>
                  <td>
                    {card.queue.map((item) => (
                      <div key={`${item.field_name}-${item.value}`} className="confirm-table__why">
                        · <b>{fieldLabel(item.field_name)}</b> {item.value || '（空）'} —— {item.why}
                      </div>
                    ))}
                  </td>
                </tr>
              ) : null}

              <tr>
                <td className="confirm-table__k">戳型理由</td>
                <td className="muted">{card.postmark_reason || '—'}</td>
              </tr>

              {/*
                「自动挂载」：识别响应里的 `links` 就是服务端按**文本**解析出来的
                id（与确认落库同一条规则）。显示出来有两个作用：
                  ① 人知道"不用我填"（A5 里为 7 张卡手填了 14 次，大多白填）；
                  ② 解析不出来时**当场看到为什么**，而不是确认后才在提示里知道。
              */}
              {card.links ? (
                <tr>
                  <td className="confirm-table__k">自动挂载</td>
                  <td>
                    <div className="confirm-table__why">
                      地点：
                      {card.links.place_name
                        ? <b>{card.links.place_name}</b>
                        : <span className="muted">未解析出（可留空 —— 那是合法状态）</span>}
                      {' · '}邮局：
                      {card.links.office_name ? (
                        <b>
                          {card.links.office_name}
                          {card.links.office_place_name
                            ? `（${card.links.office_place_name}）`
                            : ''}
                        </b>
                      ) : (
                        <span className="muted">未解析出</span>
                      )}
                    </div>
                    {card.links.note ? (
                      <div className="confirm-table__why">{card.links.note}</div>
                    ) : null}
                    <div className="confirm-table__why faint">
                      这是**服务端按上面的文本解析**的结果（与确认落库同一条规则）；
                      你一改文本，它就作废、由服务端按新文本重解析
                    </div>
                  </td>
                </tr>
              ) : null}

              {/*
                「带出相关信息」（`ok/15` q4）：识别只给**文本**，而库里要的是 id。
                这里复用**卡片页那两个选择器**（增量搜索 + 树浏览 + 就地新建），
                人选定之后 id 会随确认载荷送出去、**优先于服务端的文本解析**。
                不选也行 —— 那时服务端按同一条词表规则解析（`ok/16` §5.3）。
              */}
              <tr>
                <td className="confirm-table__k">结构化地点</td>
                <td>
                  <PlacePicker
                    value={draft.placeId}
                    initialSelected={draft.place}
                    onChange={onPickPlace}
                    placeholder="搜区划名，如「玉山」；选中后这枚戳会挂到它的 id 上"
                  />
                  <div className="confirm-table__why">
                    {draft.placeId === ''
                      ? '未指定 → 服务端按地名文本解析（与识别判定同一条词表规则）；解析不出就留空'
                      : '已指定 → 确认时**优先用这个 id**'}
                    {' · '}它只是索引，允许为空（`中关村`「珠峰」不是行政区）
                  </div>
                </td>
              </tr>

              <tr>
                <td className="confirm-table__k">结构化邮局</td>
                <td>
                  <PostOfficePicker
                    value={draft.officeId}
                    initialSelected={draft.office}
                    onChange={onPickOffice}
                    //: 候选**限定在已选/已解析的地点子树内**（`ok/18` §3.3 D-④）。
                    //: 没选地点时不限定 —— 那至少还能搜到（虽然可能撞名）。
                    placeId={draft.placeId || undefined}
                    placeholder="搜局名或邮编，如「建溪」"
                  />
                  <div className="confirm-table__why">
                    {draft.officeId === ''
                      ? '未指定 → 服务端**只在上面那个地点的子树内**按局名解析（实测「人民路」出现在 104 个县，全局匹配必然挂错）；命中多家同名就不挂'
                      : '已指定 → 确认时**优先用这个 id**'}
                    {draft.placeId !== '' ? ' · 候选已限定在该地点的子树内' : ''}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>

          {card.ocr_lines.length > 0 ? (
            <div className="confirm-ocr">
              OCR 原文：
              {card.ocr_lines.map(([text, confidence]) => (
                <code key={`${text}-${confidence}`} title={`${confidence}`}>
                  {text}
                </code>
              ))}
            </div>
          ) : null}

          {card.notes.length > 0 ? (
            <div className="confirm-notes">
              {card.notes.map((note) => (
                <div key={note}>· {note}</div>
              ))}
            </div>
          ) : null}

          <div className="confirm-card__acts">
            <button
              type="button"
              className="btn btn--sm"
              disabled={submitDisabled}
              onClick={onConfirmOne}
            >
              {busy ? <InlineSpinner /> : null}
              只确认这一张
            </button>
            <button
              type="button"
              className={`btn btn--sm ${draft.excluded ? '' : 'btn--danger-ghost'}`}
              onClick={() => onExclude(!draft.excluded)}
            >
              {draft.excluded ? '恢复录入' : '屏蔽（不录入）'}
            </button>
            <span className="small faint">
              「只确认这一张」把其余卡全部屏蔽着一起提交 —— 后端用数组长度算
              「已屏蔽几张」，滤掉它们会让那个计数变成 0
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function decisionVar(decision: Decision): string {
  if (decision === 'auto_accept') return 'success';
  if (decision === 'reject') return 'danger';
  return 'warn';
}

interface CardListProps {
  page: PageView;
  draft: PageDraft;
  /**
   * **人工**的展开/折叠意图（三态：无记录 = 自动）。
   *
   * ⚠️ 以前这里传的是 `collapsed: ReadonlySet<number>`（只有"人工折叠"），
   * 于是 `open = needsExpanding(card) && !collapsed.has(...)` —— 机器认为干净的卡
   * **点不开**（caret 停在 ▸，像点击失效）。见 `docs/ok/15` bug1。
   */
  manualOpen: ReadonlyMap<number, boolean>;
  busyIndex: number | null;
  submitDisabled: boolean;
  /** `open` 是**取反后的目标状态**：调用方只管记下人的意图，不用自己算当前状态。 */
  onToggle: (index: number, open: boolean) => void;
  onEditField: (index: number, fieldName: string, value: string) => void;
  onVisitDate: (index: number, value: string) => void;
  onExclude: (index: number, excluded: boolean) => void;
  onConfirmOne: (index: number) => void;
  /** 选定/清除结构化的地点与邮局（`ok/15` q4）。 */
  onPickPlace: (index: number, place: PlaceRead | null) => void;
  onPickOffice: (index: number, office: PostOfficeRead | null) => void;
}

export function CardList({
  page,
  draft,
  manualOpen,
  busyIndex,
  submitDisabled,
  onToggle,
  onEditField,
  onVisitDate,
  onExclude,
  onConfirmOne,
  onPickPlace,
  onPickOffice,
}: CardListProps) {
  const draftByIndex = useMemo(
    () => new Map(draft.cards.map((card) => [card.index, card])),
    [draft.cards],
  );

  //: 放大弹层当前看的是哪一张（`null` = 关着）。状态放在这里而不是页面级：
  //: 它纯粹是"看一眼"的动作，不该影响确认流程的任何状态。
  const [zoomIndex, setZoomIndex] = useState<number | null>(null);
  const zoomCard = zoomIndex === null ? null : page.cards.find((c) => c.index === zoomIndex);

  return (
    <div className="confirm-list">
      {zoomCard ? (
        <CardZoom page={page} card={zoomCard} onClose={() => setZoomIndex(null)} />
      ) : null}
      {page.cards.map((card) => {
        const cardDraft = draftByIndex.get(card.index);
        if (!cardDraft) return null;
        // 展开规则：**人工意图优先，机器规则（方案 3）兜底** —— 见 resolveCardOpen。
        const open = resolveCardOpen(card, manualOpen);
        return (
          <CardRow
            key={card.index}
            page={page}
            card={card}
            draft={cardDraft}
            open={open}
            busy={busyIndex === card.index}
            submitDisabled={submitDisabled}
            onToggle={() => onToggle(card.index, !open)}
            onZoom={() => setZoomIndex(card.index)}
            onEditField={(name, value) => onEditField(card.index, name, value)}
            onVisitDate={(value) => onVisitDate(card.index, value)}
            onExclude={(excluded) => onExclude(card.index, excluded)}
            onConfirmOne={() => onConfirmOne(card.index)}
            onPickPlace={(place) => onPickPlace(card.index, place)}
            onPickOffice={(office) => onPickOffice(card.index, office)}
          />
        );
      })}
    </div>
  );
}
