/**
 * 识别确认页的**纯逻辑层**。
 *
 * 为什么把它从组件里分出来：这一层里全是**判定规则**，而每一条规则都对应
 * 一次实测踩坑（`docs/ok/12` §二、`docs/frontDemo/README.md`）。写在组件里
 * 就只能靠人工点界面来验证；抽出来之后 `apps/web/tests/recognize.test.mjs`
 * 能直接把它们钉住 —— 那套测试走的是仓库已有的 tsc + node:test，**零新依赖**
 * （与 `draft.ts` 同一条路子，见 `package.json` 的 `test:pure`）。
 *
 * 这个文件**不 import 任何 React、不 import axios**，只 import type。
 * 那样它才能被单独编译成自包含 JS 跑在 Node 里。
 */

import type {
  ConfirmCardIn,
  ConfirmDefaults,
  ConfirmFieldIn,
  ConfirmSubmission,
  Decision,
  PlaceRead,
  PostOfficeRead,
  RecognizedCardRead,
  RecognizedFieldRead,
  RecognizedPageRead,
} from '../../shared/types';

/** 一页识别结果 + 它对应的资产 id。`assetId` 是**预览与提交都要用**的。 */
export interface PageView extends RecognizedPageRead {
  assetId: string;
}

// --------------------------------------------------------------------------- //
//  字段名约定
// --------------------------------------------------------------------------- //

/** 三个"要看一眼"的字段。折叠摘要就显示这三个（局名会拼上戳号）。 */
export const SUMMARY_FIELDS = ['date', 'place', 'office'] as const;

/** 展开区里字段的显示顺序。不在这里的字段排在后面。 */
export const FIELD_ORDER = [
  //: 戳面原文排第一：它是这枚戳的**证据**，也是录入时最该先看的东西
  //: （卡片页的字段顺序也是"戳面原文 → 日期 → 地点/邮局 → 其余"）
  'inscription',
  'date',
  'date_text',
  'time_code',
  'place',
  'office',
  'office_no',
  'postmark_type',
  'stamp_region',
] as const;

/**
 * 会进 `ConfirmFieldIn[]` 的字段。
 *
 * 这就是后端 `review.build_bundle` 能产出的全部字段名。**故意用白名单而不是
 * "把响应里的字段全发回去"**：`observation` 是"机器说的是什么"的档案，
 * 里面混进一个前端自己编的字段名（比如界面用的 `stamp_region`）就再也
 * 分不清哪些是机器读的。
 */
export const CONFIRM_FIELDS: readonly string[] = [
  //: ⚠️ `inscription` 从 2026-10-11 起**必填**（`docs/ok/16` §4.3）：
  //: 后端拿它建 `postmark`（`inscription` 是 NOT NULL 的证据列），
  //: 空值会被 422 拒绝 —— 所以它必须在这份白名单里，否则前端根本送不出去。
  'inscription',
  'date',
  'date_text',
  'time_code',
  'place',
  'office',
  'office_no',
  'postmark_type',
];

/** 摘要 chip 与展开表格用的中文名。 */
export const FIELD_LABELS: Record<string, string> = {
  inscription: '戳面原文',
  date: '日期',
  date_text: '日期原文',
  time_code: '时码',
  place: '地名',
  office: '局名',
  office_no: '戳号',
  postmark_type: '戳种',
  stamp_region: '邮票区',
};

export function fieldLabel(name: string): string {
  return FIELD_LABELS[name] ?? name;
}

export const DECISION_LABEL: Record<Decision, string> = {
  auto_accept: '可自动采纳',
  needs_review: '待确认',
  reject: '未采信',
};

export const DECISION_ICON: Record<Decision, string> = {
  auto_accept: '✓',
  needs_review: '!',
  reject: '×',
};

// --------------------------------------------------------------------------- //
//  草稿（本地状态）
// --------------------------------------------------------------------------- //

/** 一个字段在当前这一页上的编辑状态。`finalValue` 是显示给人看的那个值。 */
export interface FieldDraft {
  field_name: string;
  /** 机器值。**从识别响应原样拷来，任何编辑都不许动它** —— 它要进 observation。 */
  machineValue: string;
  machineConfidence: number | null;
  machineDecision: Decision;
  reason: string;
  pipelineVersion: string;
  /** 人确认后的值。`null` = 这一项不采纳（与空串"采纳为空"不同） */
  finalValue: string | null;
  /** 人在这个页面上改过它 */
  edited: boolean;
}

export interface CardDraft {
  index: number;
  excluded: boolean;
  /** 跑戳日。**由识别日期预填**，但它是人的确认值 —— 服务端不会替你编 */
  visitDate: string;
  fields: FieldDraft[];
  /**
   * 这张卡的四边形（**工作尺度**坐标）—— 随确认载荷送给服务端，
   * 它据此在**原始分辨率**上裁出"切好的那张卡"（`ok/18` §3.5）。
   */
  quad: [number, number][];
  /**
   * 戳圆 **(cx, cy, r)**（**工作尺度**）—— 随确认载荷送给服务端，
   * 它据此从整页原图抠出戳贴片（`ok/19` §三 3.1）。识别没定位到圆时为空。
   */
  circle: [number, number, number] | null;
  /**
   * 人**选定**的结构化地点/邮局（`ok/15` q4）。
   *
   * 为什么要有它：识别只给**文本**（`福建南平`、`建溪`），而库里要的是 id。
   * 服务端会按"与识别判定同一条规则"把文本解析成 id（`ok/16` §5.3），
   * 但**人改过文本、或想指定另一家同名邮局**时，只有 id 能表达那个意图。
   * 空串 = "没指定，交给服务端解析"。
   */
  placeId: string;
  place: PlaceRead | null;
  officeId: string;
  office: PostOfficeRead | null;
}

export interface PageDraft {
  assetId: string;
  pipelineVersion: string;
  workSize: [number, number];
  /** 识别时的工作尺度：把 `quad`（工作尺度）换算回原图时要用（`ok/18` §3.5）。 */
  workScale: number;
  cards: CardDraft[];
}

function toFieldDraft(field: RecognizedFieldRead): FieldDraft {
  return {
    field_name: field.field_name,
    machineValue: field.value,
    machineConfidence: field.confidence,
    machineDecision: field.decision,
    reason: field.reason,
    pipelineVersion: field.pipeline_version,
    finalValue: field.value,
    edited: false,
  };
}

/**
 * 从识别出来的日期字段里取出 `YYYY-MM-DD`。
 *
 * ⚠️ **不能直接拿日期字段的值当跑戳日**：实测 `date` 的值带时间，
 * 形如 `"2026-10-01 17:00"`（时间码是戳面的一部分），而
 * `ConfirmCardIn.visit_date` 是 `date` 类型 —— 原样送过去会被 Pydantic
 * 判成 422，而报错信息说的是"不是合法日期"，跟"多了一个时间"没有字面关联。
 *
 * 取不到 `YYYY-MM-DD` 就返回空串（交给用户填），**不猜**：
 * 后端明确拒绝替用户编一个日期（会污染按 `visit_date` 的统计）。
 */
export function visitDateFromFieldValue(value: string): string {
  const match = /(\d{4}-\d{2}-\d{2})/.exec(value);
  return match ? match[1] : '';
}

/**
 * 识别响应 → 可编辑草稿。
 *
 * **跑戳日由识别日期预填**（`ok/12` 决定 6），但只取其中的 `YYYY-MM-DD`
 * —— 见 `visitDateFromFieldValue`。识别没给出日期时保持空，后端会拒绝，
 * 那是有意的。
 */
export function pageDraftFromResponse(assetId: string, page: RecognizedPageRead): PageDraft {
  return {
    assetId,
    pipelineVersion: page.pipeline_version,
    workSize: page.work_size,
    workScale: page.work_scale,
    cards: page.cards.map((card) => {
      const fields = card.fields.map(toFieldDraft);
      const date = fields.find((f) => f.field_name === 'date');
      return {
        index: card.index,
        excluded: false,
        visitDate: visitDateFromFieldValue(date ? date.machineValue : ''),
        fields,
        //: 四边形（工作尺度）随草稿带着 —— 确认时服务端用它裁出"切好的那张卡"
        quad: card.quad ?? [],
        circle: card.circle
          ? [card.circle.cx, card.circle.cy, card.circle.r]
          : null,
        //: **自动挂**（用户 2026-10-12）：识别响应里的 `links` 就是服务端按
        //: 文本解析出来的 id —— 直接预填，于是它随确认载荷送出去，
        //: 人在界面上只需核对（而不是像 A5 那样手填 14 次）。
        //: `place` / `office` 两个对象留给"人自己选定"那条路（那时 picker 给全对象）。
        placeId: card.links?.place_id ?? '',
        place: null,
        officeId: card.links?.office_id ?? '',
        office: null,
      };
    }),
  };
}

// --------------------------------------------------------------------------- //
//  判定规则（原型 `needsExpanding()` / `worstDecision()` 的正式版）
// --------------------------------------------------------------------------- //

export function fieldOf(card: RecognizedCardRead, name: string): RecognizedFieldRead | null {
  return card.fields.find((f) => f.field_name === name) ?? null;
}

export function draftFieldOf(card: CardDraft, name: string): FieldDraft | null {
  return card.fields.find((f) => f.field_name === name) ?? null;
}

/**
 * **当前值**：人的确认值优先，没改过就是机器值。
 *
 * 这是界面上"这个字段现在是什么"的**唯一**取法。区分它和 `fieldOf(card, …)`：
 *
 * * `fieldOf(card, …)` = **机器当初的看法**（L2 证据，永不改）—— 显示理由、
 *   决策色、以及"机器值是什么"时用它；
 * * `currentFieldValue(…)` = **现在生效的值** —— 主行 chip、输入框初值用它。
 *
 * ⚠️ 主行 chip 曾经一律读前者，于是"改完展开区更正了、主行没变"
 * （`ok/18` §3.2 用户观察 #3）。
 *
 * 注意是 `??` 而不是 `||`：**清空是有效值**（`''` 表示"采纳为空"），
 * 不能被机器值回落覆盖。
 */
export function currentFieldValue(
  card: RecognizedCardRead,
  draft: CardDraft,
  name: string,
): string {
  return draftFieldOf(draft, name)?.finalValue ?? fieldOf(card, name)?.value ?? '';
}

/**
 * 局名 + 戳号合成一个标识。
 *
 * 戳号存在时**必须拼上** ——「中亭街」与「中亭街1」是两个不同的邮局，
 * 少一个数字就是错的记录（`ok/12` §六提到戳号曾经"处处不可见"，已修）。
 *
 * ⚠️ 这是**唯一**的合成处：`officeLabel`（识别响应）与主行 chip（人的当前值）
 * 都走它 —— 曾经主行自己拼一遍、展开区又分开显示，于是同一张卡上出现
 * `三清山1` 与 `三清山` 两个"局名"（`ok/18` §3.2）。
 */
export function officeLabelOf(office: string, officeNo: string): string {
  return office ? office + officeNo : '';
}

export function officeLabel(card: RecognizedCardRead): string {
  const office = fieldOf(card, 'office');
  const no = fieldOf(card, 'office_no');
  return officeLabelOf(office?.value ?? '', no?.value ?? '');
}

/**
 * 这张卡**最坏**的决策 —— 决定行边框与摘要色。
 *
 * `field_source` 以 `vlm` 开头时**至少是 needs_review**：云端结果不可复现
 * （`ok/11` §3.3c），标出来是为了让人知道"这一张重跑不会一样"。
 *
 * ⚠️ 取值在 2026-10-11 细分了（`ok/17` §二 A3）：`vlm_json`（整卡一次 JSON 调用）
 * 与 `vlm_fallback`（本地/JSON 都没拿到日期时的回退）。所以这里判**前缀** ——
 * 否则细分的那个取值会静默地不再触发"至少待确认"。
 */
export function worstDecision(card: RecognizedCardRead): Decision {
  const decisions = card.fields.map((f) => f.decision);
  if (decisions.includes('reject')) return 'reject';
  if (decisions.includes('needs_review') || card.queue.length > 0 || card.size_suspect) {
    return 'needs_review';
  }
  if (card.field_source.startsWith('vlm')) return 'needs_review';
  return 'auto_accept';
}

/**
 * **是否要人展开看** —— 用户选定的"方案 3"（`ok/11` §3.3d 决定 2）。
 *
 * 判据不是"全绿"，而是"**有没有带值的可疑项**"：
 *
 * | 情况 | 展开 |
 * | --- | --- |
 * | 三字段**有值但 needs_review** | ✅ 可能是错的，最需要人看 |
 * | 三字段**有值但 reject**（置信度过低） | ✅ 红标的值也要看一眼 |
 * | **字段为空** | ❌ 空不是异常，到展开区再填 |
 * | `size_suspect`（尺寸异常） | ✅ 几何问题，字段值上看不出来 |
 * | 戳型 unknown | ❌ 次要字段，"不知道"是诚实输出 |
 *
 * 为什么要区分"空"与"可疑"：**它们是两种不同的东西**。空 = 机器没读到 →
 * 人不需要现在做决定；可疑 = 机器给了一个值但可能错 → 人**必须**判断。
 * 把两者混在一起会让每一张有待填空字段的卡都展开，于是全部展开、等于没折叠。
 *
 * ⚠️ 方案 3 的代价（如实记下）：**"读不到"不再引人注意**。补救是
 * `pagePlan()` 里的 `empty` / `rejectedFields` 两个计数与展开区里的
 * `（空）` / `未采信` 标记 —— 它们把"读不到"变成一个**页面上有数**的量，
 * 而不是零。
 */
export function needsExpanding(card: RecognizedCardRead): boolean {
  if (card.size_suspect) return true;
  //: 几何来自云端 → **也展开**（`ok/20`）。理由与 `size_suspect` 同一条：
  //: 那是**几何**问题，字段值上看不出来，而几何错会让整张卡的读数一起歪。
  //: 云端几何还多一层：它不可复现（重跑框会差几个像素），所以"这一张值得看"
  //: 这件事不该只体现在角落那个小徽标上。
  if (geometryFromCloud(card)) return true;
  return SUMMARY_FIELDS.some((name) => {
    const field = fieldOf(card, name);
    if (!field) return false;
    if (!field.value) return false; // 空不是异常
    return field.decision === 'needs_review' || field.decision === 'reject';
  });
}

/**
 * 这张卡的**几何**是不是云端给的（`ok/20`）。
 *
 * ⚠️ 与 `field_source.startsWith('vlm')` 是**两件事**，不许互相替代：
 * 字段来自云端时几何完全可能仍是本地切的（B 模式下这就是常态）。
 *
 * 为什么用 `=== 'vlm'` 而不是前缀判断：几何来源只有 `local` / `vlm` 两个取值
 * （字段来源才细分成 `vlm_json` / `vlm_fallback`）。用前缀判断会把未来某个
 * 叫 `vlmx` 的取值也吞进来 —— 而"吞进来"在这里的表现是**多展开**，
 * 不会报错，所以宁可写死。
 */
export function geometryFromCloud(card: RecognizedCardRead): boolean {
  return card.geometry_source === 'vlm';
}

/**
 * 一张卡**此刻是否展开**。
 *
 * 规则：**人工意图优先，机器规则兜底**。
 *
 * * `manualOpen` 里有这张卡 → 就用它（点过 = 人的决定，两个方向都算）
 * * 没有 → 用 `needsExpanding()`（方案 3：只有"带值的可疑项"逼人看）
 *
 * ⚠️ 这里修掉一个很实在的缺陷（2026-10-11，见 `docs/ok/15` bug1）：
 * 原来只有"**人工折叠**"这一个集合，于是
 *
 * ```ts
 * const open = needsExpanding(card) && !collapsed.has(card.index)
 * ```
 *
 * —— 机器认为"干净"的卡**永远打不开**：点它只是往 `collapsed` 里塞了个索引，
 * 而 `open` 本来就是 false，视觉上毫无反应（caret 一直停在 ▸，看着像点击没生效）。
 *
 * 所以修法不是"再加一个集合"（两个集合要各自判谁优先，容易错），而是
 * **把人工意图做成一个三态映射**：`无记录 = 自动`、`true = 人工展开`、
 * `false = 人工折叠`。这样"人工覆盖机器"两个方向由同一行代码保证。
 */
export function resolveCardOpen(
  card: RecognizedCardRead,
  manualOpen: ReadonlyMap<number, boolean>,
): boolean {
  return manualOpen.get(card.index) ?? needsExpanding(card);
}

/** 摘要行用的状态色：`reject` > `needs_review` > `auto_accept`。 */
export function summaryDecision(card: RecognizedCardRead): Decision {
  return worstDecision(card);
}

/** 放大视图要裁的那一块（`ok/15` q1）。 */
export interface ZoomView {
  /** work 尺度坐标下的裁切框 `[x, y, w, h]` */
  box: [number, number, number, number];
  /** 显示倍率（CSS 像素 / work 尺度像素） */
  scale: number;
  /** 是否已经**顶到原始分辨率**（再放大只是插值，没有新信息） */
  capped: boolean;
}

/**
 * 算出"放大看这一张"该裁哪一块、放大多少倍。
 *
 * ## 三条规则（都有理由）
 *
 * 1. **裁切坐标是 work 尺度** —— 响应里的 `bbox` / `circle` 都是那个空间，
 *    而 `/raw` 是原始件；把 work 尺度按 `1/work_scale` 放大就回到原图像素
 *    （`_resize_for_work` 的逆运算）。
 * 2. **封顶在原始分辨率** —— 超过 `1/work_scale` 的倍率只是插值放大，
 *    看起来更"大"却没有更多信息，反而让人以为看清了。到顶就告诉用户。
 * 3. **邮戳那框比戳大 30%**（`side = 2.6r`）—— 贴边裁会把外圈切掉，
 *    而"戳径对不对"正是要靠外圈判断的事。
 */
export function zoomViewFor(
  card: RecognizedCardRead,
  options: { mode: 'card' | 'postmark'; viewWidth: number; workScale: number },
): ZoomView | null {
  const { mode, viewWidth, workScale } = options;
  let box: [number, number, number, number];
  if (mode === 'card') {
    box = card.bbox;
  } else {
    const circle = card.circle;
    if (!circle) return null;
    const side = Math.max(8, circle.r * 2.6);
    box = [circle.cx - side / 2, circle.cy - side / 2, side, side];
  }
  //: work_scale 为 0/缺失时**不封顶**（宁可放大也不要不显示）—— 那是异常输入
  const nativeMax = workScale > 0 ? 1 / workScale : Number.POSITIVE_INFINITY;
  const wanted = viewWidth / Math.max(1, box[2]);
  const scale = Math.min(wanted, nativeMax);
  return { box, scale, capped: wanted > nativeMax };
}

/**
 * 页面计划（底栏那行字）。
 *
 * `empty` 单独数出来，是为了对冲方案 3 的代价：折叠之后"机器什么都没读到"
 * 最容易被漏掉，所以让它变成一个有数的量。
 */
export interface PagePlan {
  total: number;
  excluded: number;
  /** 将入库的卡数 */
  included: number;
  /** 有带值的可疑项、默认展开的卡数 */
  expand: number;
  /** 可以直接确认的卡数 */
  clean: number;
  /** 机器什么都没读到的卡数（`needsExpanding` 故意不为它展开） */
  empty: number;
  /** 被机器判为未采信（reject）的字段数 */
  rejectedFields: number;
  /** 缺跑戳日、因而**不能**提交的卡数 */
  missingVisitDate: number;
  /**
   * 缺**戳面原文**、因而不能提交的卡数（`docs/ok/16` §4.3）。
   *
   * 为什么让它挡住提交：戳面原文是这枚戳的**证据**，而 `Postmark.inscription`
   * 是 NOT NULL。模型什么都没读到时，那一行只能由人敲 —— 服务端不替人编
   * （与 `visit_date` 同一条哲学）。
   */
  missingInscription: number;
}

export function pagePlan(page: RecognizedPageRead, draft: PageDraft): PagePlan {
  const draftByIndex = new Map(draft.cards.map((c) => [c.index, c]));
  const plan: PagePlan = {
    total: page.cards.length,
    excluded: 0,
    included: 0,
    expand: 0,
    clean: 0,
    empty: 0,
    rejectedFields: 0,
    missingVisitDate: 0,
    missingInscription: 0,
  };

  for (const card of page.cards) {
    const cardDraft = draftByIndex.get(card.index);
    const isExcluded = cardDraft?.excluded ?? false;
    if (isExcluded) plan.excluded += 1;
    else plan.included += 1;

    const readAnything = card.fields.some((f) => f.value !== '');
    if (!readAnything) plan.empty += 1;

    if (needsExpanding(card)) plan.expand += 1;
    else plan.clean += 1;

    plan.rejectedFields += card.fields.filter((f) => f.decision === 'reject').length;

    if (!isExcluded && cardDraft && cardDraft.visitDate === '') plan.missingVisitDate += 1;
    if (!isExcluded && cardDraft) {
      const inscription = draftFieldOf(cardDraft, 'inscription');
      if (!inscription || (inscription.finalValue ?? '').trim() === '') {
        plan.missingInscription += 1;
      }
    }
  }

  return plan;
}

// --------------------------------------------------------------------------- //
//  草稿 → 确认请求体
// --------------------------------------------------------------------------- //

function toConfirmField(field: FieldDraft, fallbackPipelineVersion: string): ConfirmFieldIn {
  return {
    field_name: field.field_name,
    // **机器值是识别响应里的原值**，不是 `finalValue` —— 后者会毁掉 correction
    machine_value: field.machineValue === '' ? null : field.machineValue,
    machine_confidence: field.machineConfidence,
    machine_decision: field.machineDecision,
    final_value: field.finalValue,
    pipeline_version: field.pipelineVersion || fallbackPipelineVersion,
  };
}

/**
 * 草稿 → `POST /api/cards/confirm` 的载荷。
 *
 * 三件事必须在这里做对：
 *
 * 1. **屏蔽的卡照样送出去**，只是 `excluded=true`。后端用
 *    `payload.cards` 的长度算 `excluded_cards`，把它们从数组里滤掉会让那个
 *    计数变成 0（"我屏蔽了 3 张"在返回里看不见）。而且被屏蔽的卡不进
 *    `included`，所以"不录入"确实不需要删任何东西。
 * 2. **`machine_value` 用识别原值**，`final_value` 用界面上的值 —— 两者不等
 *    时后端才写 `correction`（A8 的标注资产）。
 * 3. **只送 `CONFIRM_FIELDS` 白名单里的字段**，避免前端自造字段名混进
 *    `observation` 档案。
 */
export function buildConfirmSubmission(
  draft: PageDraft,
  defaults: ConfirmDefaults,
  options: { confirmAgain?: boolean } = {},
): ConfirmSubmission {
  const cards: ConfirmCardIn[] = draft.cards.map((card) => ({
    index: card.index,
    excluded: card.excluded,
    visit_date: card.visitDate === '' ? null : card.visitDate,
    //: 人选定过的结构化地点/邮局 → 服务端**优先用它们**（`ok/16` §5.3）；
    //: 没选就给 null，让服务端按文本解析（同一条词表规则）。
    place_id: card.placeId === '' ? null : card.placeId,
    post_office_id: card.officeId === '' ? null : card.officeId,
    fields: card.fields
      .filter((f) => CONFIRM_FIELDS.includes(f.field_name))
      .map((f) => toConfirmField(f, draft.pipelineVersion)),
    //: 几何：服务端据此裁出"切好的那张卡"并落成派生资产（`ok/18` §3.5）。
    //: 没有几何（老数据）就送 null —— 服务端照常落库，只是卡片页仍显示整页。
    quad: card.quad.length > 0 ? card.quad : null,
    //: 戳圆（工作尺度）→ 服务端抠戳贴片（`ok/19` §三 3.1）
    circle: card.circle,
  }));

  return {
    asset_id: draft.assetId,
    pipeline_version: draft.pipelineVersion,
    defaults,
    cards,
    confirm_again: options.confirmAgain ?? false,
    work_scale: draft.workScale > 0 ? draft.workScale : null,
  };
}

/**
 * 只确认一张卡：其余全部标成 `excluded`。
 *
 * 为什么要这个：用户选定的主路径是**整页批量确认**（A5 的点击预算），
 * 但 A6 说"无一例外经人工确认"，所以逐卡路径必须存在。它的做法不是
 * "发一个只有一张卡的请求"（那样 `excluded_cards` 就丢了），而是
 * **把这一页的其余卡全部屏蔽着一起送**。
 */
export function buildSingleCardSubmission(
  draft: PageDraft,
  cardIndex: number,
  defaults: ConfirmDefaults,
  options: { confirmAgain?: boolean } = {},
): ConfirmSubmission {
  const onlyThis: PageDraft = {
    ...draft,
    cards: draft.cards.map((card) => ({
      ...card,
      excluded: card.index === cardIndex ? card.excluded : true,
    })),
  };
  return buildConfirmSubmission(onlyThis, defaults, options);
}

// --------------------------------------------------------------------------- //
//  草稿的更新（纯函数，返回新草稿）
// --------------------------------------------------------------------------- //

/**
 * 改一个字段的值。
 *
 * 两条规则，都有理由：
 *
 * * **改回机器值 = 没改过**。否则"✎ 已改"会一直挂着，而人以为自己撤销了 ——
 *   后端那边也会因为 `final_value === machine_value` 而不写 `correction`，
 *   于是界面与库里的说法不一致。
 *
 * 只改**值**，不动 `machineValue` / `machineConfidence` / `machineDecision`：
 * 那三个是"机器说的是什么"的证据，要原样进 `observation`。
 * 显示上"人改过"就够表达"它是人给的"，不需要去改写机器的判断 ——
 * 那会让展开区里的"机器值"与"置信度"自相矛盾。
 */
export function withFieldValue(
  draft: PageDraft,
  cardIndex: number,
  fieldName: string,
  value: string,
): PageDraft {
  return {
    ...draft,
    cards: draft.cards.map((card) => {
      if (card.index !== cardIndex) return card;
      const fields = card.fields.map((field) => {
        if (field.field_name !== fieldName) return field;
        const reverted = value === field.machineValue;
        return {
          ...field,
          finalValue: value,
          edited: !reverted,
        };
      });
      return {
        ...card,
        fields,
        //: ⚠️ **改了地名/局名的文本，就把自动解析出来的 id 作废**（`ok/18` §3.3 D-③）。
        //:
        //: 不加这一条会出一个很隐蔽的错：服务端按**文本**解析出 id 预填进来，
        //: 人随后把文本改对了（比如 `福建南火` → `福建南平`），而那个 id 还是
        //: 按**旧文本**解析的 —— 于是"文本显示新值、库里挂旧 id"。
        //: 作废之后 `place_id` 变空，确认时服务端会按**新的文本**重新解析。
        //:
        //: 戳号（`office_no`）不清：它不是局名的一部分（`中亭街` + `1`），
        //: 改戳号不影响"这家局是哪一家"。
        ...(fieldName === 'place' ? { placeId: '', place: null } : {}),
        ...(fieldName === 'office' ? { officeId: '', office: null } : {}),
      };
    }),
  };
}

/** 屏蔽/恢复一张卡（`excluded` 只是客户端标记，提交时才起作用）。 */
export function withCardExcluded(
  draft: PageDraft,
  cardIndex: number,
  excluded: boolean,
): PageDraft {
  return {
    ...draft,
    cards: draft.cards.map((card) =>
      card.index === cardIndex ? { ...card, excluded } : card,
    ),
  };
}

/** 改跑戳日（每张卡一个）。 */
export function withVisitDate(
  draft: PageDraft,
  cardIndex: number,
  visitDate: string,
): PageDraft {
  return {
    ...draft,
    cards: draft.cards.map((card) =>
      card.index === cardIndex ? { ...card, visitDate } : card,
    ),
  };
}

/**
 * 选定/清除**结构化地点**（`ok/15` q4）。
 *
 * 为什么是"人选定"而不是"前端猜"：同名地点在区划树里可能有多条，而**哪一条**
 * 是戳上那家所属于的，只有人（或服务端的解析规则）能定。传 `null` = 清掉
 * 手动指定，回到"让服务端按文本解析"。
 */
export function withStructuredPlace(
  draft: PageDraft,
  cardIndex: number,
  place: PlaceRead | null,
): PageDraft {
  return {
    ...draft,
    cards: draft.cards.map((card) =>
      card.index === cardIndex
        ? { ...card, placeId: place ? place.id : '', place: place ?? null }
        : card,
    ),
  };
}

/** 选定/清除**结构化邮局**。语义同 `withStructuredPlace`。 */
export function withStructuredOffice(
  draft: PageDraft,
  cardIndex: number,
  office: PostOfficeRead | null,
): PageDraft {
  return {
    ...draft,
    cards: draft.cards.map((card) =>
      card.index === cardIndex
        ? { ...card, officeId: office ? office.id : '', office: office ?? null }
        : card,
    ),
  };
}

/**
 * 识别给出的**层级链**（从粗到细），没有就空串。
 *
 * 数据一直在响应里（`place_path`），以前只是类型没声明、界面没显示 ——
 * 用户要的"带出相关信息"最省事的一半就是它。
 */
export function placePathOf(card: RecognizedCardRead): string {
  const field = fieldOf(card, 'place');
  const path = field?.place_path ?? [];
  return path.length > 0 ? path.join(' / ') : '';
}

// --------------------------------------------------------------------------- //
//  计数
// --------------------------------------------------------------------------- //

/**
 * 一张卡上被改过的字段数 —— 摘要行上的「改 N」标记。
 *
 * A5 的总点击计数由页面自己维护（它还要数上传、屏蔽、确认），所以这里只给
 * 单卡的计数，**不做**一个全页 `editedFieldCount()`：那会是同一件事的第二个
 * 算法，而且没人用。
 */
export function editedCountOf(card: CardDraft): number {
  return card.fields.filter((f) => f.edited).length;
}
