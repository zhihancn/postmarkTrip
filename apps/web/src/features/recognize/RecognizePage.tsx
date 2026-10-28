import { useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { AssetUploader } from '../cards/AssetUploader';
import { CardList } from './CardList';
import { Stage } from './Stage';
import { useConfirm } from '../../shared/components/Confirm';
import { EmptyState } from '../../shared/components/EmptyState';
import { Alert, InlineSpinner } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import type {
  CardSource,
  CardStatus,
  ConfirmDefaults,
  ConfirmResult,
  ConfirmSubmission,
  PlaceRead,
  PostOfficeRead,
  RecognizedPageRead,
} from '../../shared/types';
import {
  buildConfirmSubmission,
  buildSingleCardSubmission,
  pageDraftFromResponse,
  pagePlan,
  withCardExcluded,
  withFieldValue,
  withStructuredOffice,
  withStructuredPlace,
  withVisitDate,
} from './recognize';
import type { PageDraft, PageView } from './recognize';

/**
 * 识别 → 确认 → 落库。
 *
 * ## 这个页面是 A5/A6/A11 的载体
 *
 * 三层分工（`ok/11` §四）：
 *
 * | 步骤 | 端点 | 写库吗 |
 * | --- | --- | --- |
 * | 上传原始件 | `POST /api/assets` | 只写资产（L0，内容寻址去重） |
 * | 识别 | `POST /api/recognize` | **不写**（A6 靠结构保证，后端有断言） |
 * | 确认 | `POST /api/cards/confirm` | **唯一的写入点** |
 *
 * 所以"没经人工确认就落库"在结构上不可能发生 —— 不是靠人记得别写。
 *
 * ## 界面形态（用户拍板的两条）
 *
 * * **默认折叠成摘要**：每卡一行（缩略图 + 日期/地名/局名 + 状态色），
 *   只有"**有值的可疑项**"自动展开（原型里的"方案 3"）。
 *   空字段**不算异常** —— 否则每张有待填空字段的卡都会展开，等于没折叠。
 *   代价如实记下：**"读不到"不再引人注意**，所以底栏单独数了"空卡"。
 * * **整页批量确认为主 + 「只确认这一张」为次**：A5 的确认动作是 1 次；
 *   逐卡路径也存在（A6 说"无一例外经人工确认"）。
 *
 * ## 跑戳日不预填就提交不了
 *
 * `Card.visit_date` 是 NOT NULL，而它是**人的确认值**。识别给出日期时预填，
 * 缺了就拒绝 —— **服务端不替用户编一个日期**，那会污染按 `visit_date` 的统计。
 * 这里在提交前先挡一道，因为 422 的报错信息不如界面上标红直观。
 */
export function RecognizePage() {
  const toast = useToast();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [assetId, setAssetId] = useState('');
  const [page, setPage] = useState<PageView | null>(null);
  const [draft, setDraft] = useState<PageDraft | null>(null);
  /**
   * **人工**的展开/折叠意图（三态：无记录 = 自动）。默认展开的规则在
   * `needsExpanding()` 里，人工意图优先 —— 判定统一走 `resolveCardOpen()`。
   *
   * ⚠️ 原来这里是 `collapsed: Set<number>`（只有"人工折叠"），于是机器认为
   * 干净的卡点不开（见 `docs/ok/15` bug1）。
   */
  const [manualOpen, setManualOpen] = useState<ReadonlyMap<number, boolean>>(new Map());
  const [busyIndex, setBusyIndex] = useState<number | null>(null);
  /**
   * A5 的点击计数器。它把 A5 的意义直接摆出来：**「看」的量两种确认方式一样，
   * 差别在必须点几次**。字段修改次数两种方式完全相同，所以这里只数一次。
   */
  const [clicks, setClicks] = useState(0);
  const [chartWidth, setChartWidth] = useState(1100);

  const [source, setSource] = useState<CardSource>('mail_request');
  const [status, setStatus] = useState<CardStatus>('collected');
  const [useVlm, setUseVlm] = useState(false);

  // 已经提交过一次之后，同一个资产下再有卡时后端会 409（挡双击提交）。
  // 记住"这个资产已经被确认过"，第二次就自动带 confirm_again。
  const confirmedAssets = useRef<Set<string>>(new Set());

  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    staleTime: 60_000,
  });
  const vocabQuery = useQuery({
    queryKey: ['vocab'],
    queryFn: () => api.vocab(),
    staleTime: Infinity,
  });
  const vlmEnabled = healthQuery.data?.vlm_enabled ?? false;

  const defaults = useMemo<ConfirmDefaults>(() => ({ source, status }), [source, status]);

  const recognizeMutation = useMutation({
    mutationFn: (input: { assetId: string; useVlm: boolean }) =>
      api.recognition.recognize({ assetId: input.assetId, useVlm: input.useVlm }),
    onSuccess: (result: RecognizedPageRead, input: { assetId: string; useVlm: boolean }) => {
      const nextPage: PageView = { ...result, assetId: input.assetId };
      setPage(nextPage);
      setDraft(pageDraftFromResponse(input.assetId, result));
      //: 新一页识别结果 → 把上一页的展开/折叠意图**丢掉**（索引已经换了一批卡）
      setManualOpen(new Map());
      setBusyIndex(null);
      if (result.card_count === 0) {
        toast.error('这一页一张卡都没切出来。若确实是单卡，请检查扫描分辨率（dpi）。');
      } else {
        toast.success(`识别完成：${result.card_count} 张卡`);
      }
    },
    onError: (error: unknown) => {
      setBusyIndex(null);
      toast.error(toApiError(error).message);
    },
  });

  const confirmMutation = useMutation({
    mutationFn: (payload: ConfirmSubmission) => api.recognition.confirm(payload),
    onSuccess: (result: ConfirmResult, payload: ConfirmSubmission) => {
      // 同一个资产下已经有卡时后端默认 409；上面已经问过人了，所以记下来
      confirmedAssets.current.add(payload.asset_id);
      toast.success(
        `已入库 ${result.created_cards} 张卡 · ${result.postmarks} 枚戳 · ` +
          `observation ${result.observations} 条` +
          (result.corrections > 0 ? ` · correction ${result.corrections} 条` : '') +
          (result.excluded_cards > 0 ? ` · 屏蔽 ${result.excluded_cards} 张` : ''),
      );
      //: ⚠️ 解析结果（地点/局名匹配到没、自动打了什么 tag）**只在这里出现一次**：
      //: 它不写进 `postmark.note`（那一列的语义是"人写给人看的备注，机器永不自动填"）。
      //: 不显示出来，这条信息就彻底丢了 —— 而"局名没挂上"正是要人知道的事。
      if (result.notes.length > 0) {
        toast.info(result.notes.join('；'));
      }
      void queryClient.invalidateQueries({ queryKey: ['cards'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
      navigate('/cards');
    },
    onError: (error: unknown) => toast.error(toApiError(error).message),
    onSettled: () => setBusyIndex(null),
  });

  const runRecognize = (nextAssetId: string, withVlm: boolean) => {
    if (nextAssetId === '') return;
    // 先把上一轮结果丢掉：否则"正在识别"那个提示永远不显示（`page` 还在，
    // 条件分支走的是"有结果"那一条），而旧结果又已经不该再提交了。
    setPage(null);
    setDraft(null);
    setBusyIndex(null);
    recognizeMutation.mutate({ assetId: nextAssetId, useVlm: withVlm });
  };

  const onAssetChange = (nextAssetId: string) => {
    setClicks((value) => value + 1);
    setAssetId(nextAssetId);
    if (nextAssetId === '') {
      // "清除"只是解除引用。识别结果与草稿一并丢掉，避免指着另一张图提交
      setPage(null);
      setDraft(null);
      return;
    }
    runRecognize(nextAssetId, useVlm);
  };

  // `{ plan }` 而不是直接存结果：那样 plan 的类型是 `PagePlan | null`，
  // 下面 `plan === null` 的判断会把类型收窄掉，`plan.included` 才通得过严格模式。
  const { plan } = useMemo(
    () => ({ plan: page && draft ? pagePlan(page, draft) : null }),
    [page, draft],
  );

  const submitDisabled = confirmMutation.isPending || recognizeMutation.isPending;

  /**
   * 提交。`onlyIndex` 给定时只把那一张当作"要入库"，其余全部屏蔽着一起送
   * —— 后端用数组长度算"已屏蔽几张"，所以不能把它们从数组里滤掉。
   */
  /**
   * 把"缺必填项"的卡**展开**。
   *
   * 为什么必须主动做：`needsExpanding()` 的规则是"只有带值的可疑项才逼人看"，
   * 而**空字段不算异常** —— 于是缺跑戳日/缺戳面原文的卡默认是**折叠**的。
   * 底栏说"有 1 张缺戳面原文"、提交按钮又点不动，人会不知道是哪一张。
   * 这里把它们打开，人一眼就能填。
   */
  const expandForMissing = (onlyIndex: number | null, which: 'visitDate' | 'inscription') => {
    if (!draft) return;
    setManualOpen((current) => {
      const next = new Map(current);
      for (const card of draft.cards) {
        if (card.excluded) continue;
        if (onlyIndex !== null && card.index !== onlyIndex) continue;
        if (which === 'visitDate') {
          if (card.visitDate === '') next.set(card.index, true);
          continue;
        }
        const field = card.fields.find((f) => f.field_name === 'inscription');
        if (!field || (field.finalValue ?? '').trim() === '') next.set(card.index, true);
      }
      return next;
    });
  };

  const submit = async (onlyIndex: number | null) => {
    if (!page || !draft || plan === null) return;
    if (plan.included === 0) {
      toast.error('这一页的卡全被屏蔽了 —— 那就没有要入库的东西，不用调确认端点。');
      return;
    }
    const thisCard = draft.cards.find((c) => c.index === onlyIndex);
    const missingVisitDate =
      onlyIndex === null
        ? plan.missingVisitDate
        : thisCard === undefined || thisCard.visitDate === ''
          ? 1
          : 0;
    if (missingVisitDate > 0) {
      expandForMissing(onlyIndex, 'visitDate');
      toast.error('有卡缺跑戳日。服务端不会替你编一个日期（会污染按日期的统计），先补上再提交。');
      return;
    }

    //: 戳面原文是**必填的证据**（`Postmark.inscription` NOT NULL，`ok/16` §4.3）。
    //: 与跑戳日同一个道理：服务端不替人编一个原文，所以在这里先拦住，
    //: 免得人点了提交才收到 422。
    const thisInscription = thisCard?.fields.find((f) => f.field_name === 'inscription');
    const missingInscription =
      onlyIndex === null
        ? plan.missingInscription
        : thisInscription === undefined ||
            (thisInscription.finalValue ?? '').trim() === ''
          ? 1
          : 0;
    if (missingInscription > 0) {
      expandForMissing(onlyIndex, 'inscription');
      toast.error(
        '有卡缺戳面原文。它是这枚戳的证据（库里不能为空），服务端不会替你编一个 —— 已把那些卡展开，填上再提交。',
      );
      return;
    }

    const again = confirmedAssets.current.has(draft.assetId);
    if (again) {
      const ok = await confirm({
        title: '这个资产已经确认过一次',
        confirmText: '再来一次',
        description: (
          <>
            <p>同一个资产下已经有卡了。继续会**再建一批卡**，而不是更新已有的那些。</p>
            <p className="small muted" style={{ marginTop: 6 }}>
              确认端点没有幂等键（`routes_confirm.py` 的"已知限制"里写明了），
              所以它的防护正是"已有卡就 409"。这里是你明确要再来一次的意思。
            </p>
          </>
        ),
      });
      if (!ok) return;
    }

    const payload =
      onlyIndex === null
        ? buildConfirmSubmission(draft, defaults, { confirmAgain: again })
        : buildSingleCardSubmission(draft, onlyIndex, defaults, { confirmAgain: again });

    // A5 的计数：整页确认是 1 次；逐卡确认是"这一张 + 被它屏蔽掉的那些"，
    // 也就是这个人为了这次入库要走的循环数（`ok/11` 的"逐卡确认：N 次点击"）。
    setClicks((value) => value + (onlyIndex === null ? 1 : plan.included));
    setBusyIndex(onlyIndex);
    confirmMutation.mutate(payload);
  };

  /** 记下**人工**的展开/折叠意图。`open` 由 `CardList` 按当前显示状态取反传来。 */
  const onToggle = (index: number, open: boolean) => {
    setManualOpen((current) => {
      const next = new Map(current);
      next.set(index, open);
      return next;
    });
  };

  const onEditField = (index: number, fieldName: string, value: string) => {
    if (!draft) return;
    setDraft(withFieldValue(draft, index, fieldName, value));
    setClicks((count) => count + 1);
  };

  const onVisitDate = (index: number, value: string) => {
    if (!draft) return;
    setDraft(withVisitDate(draft, index, value));
  };

  const onExclude = (index: number, excluded: boolean) => {
    if (!draft) return;
    setDraft(withCardExcluded(draft, index, excluded));
    setClicks((count) => count + 1);
  };

  /** 选定/清除结构化地点（`ok/15` q4）。**不计数** —— 它不属于 A5 的"改字段"。 */
  const onPickPlace = (index: number, place: PlaceRead | null) => {
    if (!draft) return;
    setDraft(withStructuredPlace(draft, index, place));
  };

  const onPickOffice = (index: number, office: PostOfficeRead | null) => {
    if (!draft) return;
    setDraft(withStructuredOffice(draft, index, office));
  };

  const recognizeError = recognizeMutation.isError ? toApiError(recognizeMutation.error) : null;
  /** 503 分两种，消息必须照搬后端那句 —— 一种是改配置，另一种是装包。 */
  const disabledHint = recognizeError?.status === 503;

  return (
    <div className="page page--wide">
      <header className="page__header">
        <div className="page__heading">
          <h1 className="page__title">扫描识别</h1>
          <div className="page__subtitle">
            上传扫描件 → 识别 → 逐项确认 → 落库。识别**不写库**，落库只发生在你确认之后。
          </div>
        </div>
        <div className="page__actions">
          <Link className="btn" to="/cards">
            返回卡片列表
          </Link>
        </div>
      </header>

      <div className="panel">
        <div className="panel__head">
          <div className="panel__title">1 · 扫描件</div>
          <div className="small muted">
            上传先去 <code className="mono">POST /api/assets</code>（内容寻址，重复投放不会重复入库），
            再拿 asset_id 去识别
          </div>
        </div>
        <div className="panel__body">
          <AssetUploader
            assetId={assetId}
            onChange={onAssetChange}
            title="整页扫描件（也可以是一张单卡）"
            hint="拖拽、点选、或 Ctrl+V 粘贴。上传完成后自动开始识别。"
            capturePaste
          />

          <div className="confirm-options">
            <label className="checkbox" title="识别默认关闭：可选依赖不该在升级后自动开始跑">
              <input type="checkbox" checked={useVlm} disabled={!vlmEnabled} onChange={(event) => setUseVlm(event.target.checked)} />
              <span>用云端识别（戳型 + 文字字段）</span>
            </label>
            <button
              type="button"
              className="btn btn--sm"
              disabled={assetId === '' || recognizeMutation.isPending}
              onClick={() => runRecognize(assetId, useVlm)}
            >
              {recognizeMutation.isPending ? <InlineSpinner /> : null}
              重新识别
            </button>

            <label className="confirm-options__field">
              <span className="small muted">来源</span>
              <select
                className="select"
                value={source}
                onChange={(event) => setSource(event.target.value as CardSource)}
              >
                {(vocabQuery.data?.card_sources ?? []).map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="confirm-options__field">
              <span className="small muted">状态</span>
              <select
                className="select"
                value={status}
                onChange={(event) => setStatus(event.target.value as CardStatus)}
              >
                {(vocabQuery.data?.card_statuses ?? []).map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <span className="small faint">
              默认来源/状态是求戳信的取值（<code className="mono">mail_request</code> /
              <code className="mono">collected</code>），可按这一批改
            </span>
          </div>

          {!vlmEnabled ? (
            <div className="small faint" style={{ marginTop: 6 }}>
              云端当前不可用（服务端 <code className="mono">vlm_enabled</code> 是关的），
              所以只能走本地。本地在地名上实测很差（0/6），这是已知的：见
              <code className="mono">ok/11</code> §3.3b。
            </div>
          ) : null}
        </div>
      </div>

      {recognizeError ? (
        <Alert kind="error" title={disabledHint ? '识别不可用（503）' : '识别失败'}>
          <div>{recognizeError.message}</div>
          {disabledHint ? (
            <div className="small" style={{ marginTop: 4 }}>
              503 有两种：**未启用**（改 <code className="mono">PMTRIP_OCR_ENGINE=rapidocr</code>）与
              **依赖没装**（装 recognition extra）。后端把两者分开了，照着上面那句话做就行 ——
              不要看到 503 就去装包。
            </div>
          ) : null}
        </Alert>
      ) : null}

      {page && draft && plan ? (
        <>
          <div className="panel">
            <div className="panel__head">
              <div className="panel__title">2 · 页面上看到了什么</div>
              <div className="small muted">
                四边形是**检测到的实际边界**（卡会倾斜，轴对齐的框画不准）；圆是定位到的邮戳外圆
              </div>
            </div>
            <div className="panel__body">
              <Stage
                page={page}
                assetId={draft.assetId}
                chartWidth={chartWidth}
                onChartWidth={setChartWidth}
              />
            </div>
          </div>

          <div className="panel">
            <div className="panel__head">
              <div className="panel__title">3 · 逐张确认（默认折叠，可疑项自动展开）</div>
              <div className="small muted">
                跑戳日由识别日期预填。**空字段不算异常**，所以它不会让卡自动展开 ——
                底栏单独数了"空卡"来对冲这一点
              </div>
            </div>
            <div className="panel__body panel__body--flush">
              <CardList
                page={page}
                draft={draft}
                manualOpen={manualOpen}
                busyIndex={busyIndex}
                submitDisabled={submitDisabled}
                onToggle={onToggle}
                onEditField={onEditField}
                onVisitDate={onVisitDate}
                onExclude={onExclude}
                onConfirmOne={(index) => {
                  void submit(index);
                }}
                onPickPlace={onPickPlace}
                onPickOffice={onPickOffice}
              />
            </div>
          </div>

          <div className="confirm-bar">
            <span className="confirm-bar__clicks">
              点击 <b>{clicks}</b> 次
            </span>
            <span className="confirm-bar__plan">
              将入库 <b>{plan.included}</b> 张 / 共 {plan.total} 张
              {plan.excluded > 0 ? <> · 已屏蔽 <b>{plan.excluded}</b> 张</> : null}
              {' · '}
              <b>{plan.expand}</b> 张有带值的可疑项（默认展开）
              {' · '}
              <b>{plan.clean}</b> 张可直接确认
              {plan.empty > 0 ? (
                <>
                  {' · '}
                  <b>{plan.empty}</b> 张机器什么都没读到
                </>
              ) : null}
              {plan.rejectedFields > 0 ? <> · 未采信字段 <b>{plan.rejectedFields}</b> 个</> : null}
              {plan.missingVisitDate > 0 ? (
                <>
                  {' · '}
                  <b className="confirm-bar__warn">{plan.missingVisitDate} 张缺跑戳日</b>
                </>
              ) : null}
              {plan.missingInscription > 0 ? (
                <>
                  {' · '}
                  <b className="confirm-bar__warn">{plan.missingInscription} 张缺戳面原文</b>
                </>
              ) : null}
            </span>
            <div className="confirm-bar__spacer" />
            <span className="small faint">
              改一个字段算 1 次；整页确认算 1 次；逐卡确认算 N 次（两边字段修改数相同）
            </span>
            <button
              type="button"
              className="btn btn--primary"
              disabled={
                submitDisabled ||
                plan.included === 0 ||
                plan.missingVisitDate > 0 ||
                plan.missingInscription > 0
              }
              onClick={() => {
                void submit(null);
              }}
              title={
                plan.missingVisitDate > 0
                  ? '有卡缺跑戳日 —— 先补上（服务端不会替你编日期）'
                  : plan.missingInscription > 0
                    ? '有卡缺戳面原文 —— 它是这枚戳的证据，先补上'
                    : '把这一页所有未屏蔽的卡一起入库'
              }
            >
              {confirmMutation.isPending && busyIndex === null ? <InlineSpinner /> : null}
              确认并入库（{plan.included} 张）
            </button>
          </div>

          {plan.missingVisitDate > 0 ? (
            <Alert kind="warn" title={`有 ${plan.missingVisitDate} 张卡缺跑戳日`}>
              识别没给出日期，而 `Card.visit_date` 是 NOT NULL、且它是**人的确认值**。
              服务端不会替你编一个日期 —— 那会污染按日期的统计与索引。
              在这些卡的展开区里填上就能提交。
            </Alert>
          ) : null}

          {plan.missingInscription > 0 ? (
            <Alert kind="warn" title={`有 ${plan.missingInscription} 张卡缺戳面原文`}>
              识别没读到戳面原文，而它是**这枚戳的证据**（库里
              `Postmark.inscription` 不能为空）。服务端不会替你编一个 ——
              在这些卡的展开区里填上就能提交。各戳型的取值：日戳 = 局名+戳号
              （`建溪1`）、文化戳 = 下弧戳名（`三清山三龙出海`）、
              风景戳 = 图案里的文字（`春风得意马蹄疾`）。
            </Alert>
          ) : null}
        </>
      ) : recognizeMutation.isPending ? (
        <div className="panel">
          <div className="panel__body">
            <div className="state-box">
              <span className="spinner spinner--lg" aria-hidden="true" />
              <div>正在识别…（切分 → 正交校正 → 圆定位 → OCR，必要时还要等云端往返）</div>
            </div>
          </div>
        </div>
      ) : (
        <EmptyState
          icon="📮"
          title="先上传一张扫描件"
          text="识别需要引擎处于打开状态（PMTRIP_OCR_ENGINE=rapidocr）。识别结果只是预览，不会写库。"
        />
      )}
    </div>
  );
}
