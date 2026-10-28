/**
 * `recognize.ts`（识别确认页的纯逻辑层）的测试。
 *
 * **跑法与 `draft.test.mjs` 完全一致**（见那里的说明）：`tsc` 把那个文件单独
 * 编译成自包含 JS，再用 Node 内置的 `node:test` 断言，**零新依赖**。
 * `npm run test:pure` 会把两个文件都编上、然后依次跑两个测试。
 *
 * **测的是哪一类问题**：这一层里每一条规则都对应一次实测踩坑，而它们
 * **写错了都不会崩** —— 只会让界面上的颜色、展开、或提交的载荷悄悄变错。
 * 尤其是这三类：
 *
 * 1. **`machine_value` 送错**（用了界面上改过的值）→ 后端把"人改的"也当成
 *    "机器读到的"，`correction` 这条长期标注资产就废了，而且**不会报错**。
 * 2. **`needsExpanding` 把"空"当成"可疑"** → 每张有待填空字段的卡都展开，
 *    等于没折叠（原型第一版就是这样）。
 * 3. **把屏蔽掉的卡从数组里滤掉** → 后端用数组长度算 `excluded_cards`，
 *    那个计数会永远是 0。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));

// 必须走 pathToFileURL：Windows 上 path.join 得到的是 `D:\...`，
// 而 ESM 的 import() 只接受 file: / data: / node: 三种 scheme。
const rc = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'features', 'recognize', 'recognize.js')).href
);

// --------------------------------------------------------------------------- //
//  夹具：与后端 `RecognizedPageRead` 同形
// --------------------------------------------------------------------------- //

function field(over = {}) {
  return {
    field_name: 'date',
    value: '2026-09-25',
    confidence: 0.98,
    decision: 'auto_accept',
    reason: '置信度 0.98 ≥ 0.55',
    pipeline_version: 'p2-0.1.0',
    ...over,
  };
}

/** 一张字段齐全的卡（全绿）。 */
function cleanCard(over = {}) {
  return {
    index: 0,
    bbox: [10, 20, 240, 400],
    quad: [
      [12, 22],
      [250, 20],
      [252, 418],
      [10, 420],
    ],
    card_size: [240, 400],
    size_suspect: false,
    postmark_type: 'daily',
    postmark_reason: '外圈连续',
    circle: { cx: 130, cy: 220, r: 55, radius_over_card_width: 0.229 },
    fields: [
      field(),
      field({ field_name: 'place', value: '福建南平', confidence: 0.95, reason: '在词表里' }),
      field({ field_name: 'office', value: '建溪', confidence: 0.9, reason: '在词表里' }),
      field({
        field_name: 'office_no',
        value: '1',
        confidence: 0.9,
        reason: '戳号：与局名同一条 OCR 行拆出',
      }),
      field({ field_name: 'postmark_type', value: 'daily', confidence: 0.9, reason: '外圈连续' }),
    ],
    queue: [],
    ocr_lines: [['福建南平 建溪1', 0.93]],
    field_source: 'local',
    //: 几何来源（ok/20）。与 `field_source` 是两件事，默认本地。
    geometry_source: 'local',
    notes: [],
    ...over,
  };
}

function page(cards, over = {}) {
  return {
    pipeline_version: 'p2-0.1.0',
    work_scale: 0.5,
    work_size: [1200, 900],
    card_count: cards.length,
    cards,
    stage_notes: ['dpi=600（用户给定）'],
    used_vlm: false,
    geometry_source: 'local',
    geometry_mode: 'local_first',
    ...over,
  };
}

const DEFAULTS = { source: 'mail_request', status: 'collected' };

// --------------------------------------------------------------------------- //
//  officeLabel：局名 + 戳号是一个标识
// --------------------------------------------------------------------------- //

test('officeLabel 把戳号拼在局名后面', () => {
  // 「中亭街」与「中亭街1」是两个不同的邮局 —— 少一个数字就是错的记录
  assert.equal(rc.officeLabel(cleanCard()), '建溪1');
});

test('officeLabel 在没有戳号时只给局名', () => {
  const card = cleanCard({
    fields: [field({ field_name: 'office', value: '建溪' })],
  });
  assert.equal(rc.officeLabel(card), '建溪');
});

test('officeLabel 在局名为空时给空串（不是孤零零一个戳号）', () => {
  const card = cleanCard({
    fields: [
      field({ field_name: 'office', value: '' }),
      field({ field_name: 'office_no', value: '1' }),
    ],
  });
  assert.equal(rc.officeLabel(card), '');
});

// --------------------------------------------------------------------------- //
//  worstDecision
// --------------------------------------------------------------------------- //

test('worstDecision 取最坏的那一档', () => {
  assert.equal(rc.worstDecision(cleanCard()), 'auto_accept');
  assert.equal(
    rc.worstDecision(
      cleanCard({ fields: [field(), field({ field_name: 'place', decision: 'needs_review' })] }),
    ),
    'needs_review',
  );
  assert.equal(
    rc.worstDecision(
      cleanCard({ fields: [field(), field({ field_name: 'place', decision: 'reject' })] }),
    ),
    'reject',
  );
});

test('worstDecision 把校对队列与尺寸异常算进待确认', () => {
  assert.equal(
    rc.worstDecision(cleanCard({ queue: [{ field_name: 'place', value: '福建南火', confidence: 0.9, why: '不在词表里' }] })),
    'needs_review',
  );
  assert.equal(rc.worstDecision(cleanCard({ size_suspect: true })), 'needs_review');
});

test('worstDecision 对云端结果至少给待确认', () => {
  // 云端结果不可复现（ok/11 §3.3c），全绿也要标出来
  assert.equal(rc.worstDecision(cleanCard({ field_source: 'vlm' })), 'needs_review');
});

test('worstDecision 认得出细分后的两条云端通道（vlm_json / vlm_fallback）', () => {
  // 2026-10-11 起 field_source 细分成两条通道（ok/17 §二 A3）——
  // 判据必须用前缀，否则细分出来的取值会静默地不再标"待确认"
  assert.equal(rc.worstDecision(cleanCard({ field_source: 'vlm_json' })), 'needs_review');
  assert.equal(rc.worstDecision(cleanCard({ field_source: 'vlm_fallback' })), 'needs_review');
  assert.equal(rc.worstDecision(cleanCard({ field_source: 'local' })), 'auto_accept');
});

// --------------------------------------------------------------------------- //
//  needsExpanding —— 用户选定的"方案 3"
// --------------------------------------------------------------------------- //

test('带值的可疑项要人展开', () => {
  const card = cleanCard({
    fields: [field({ field_name: 'place', value: '福建南火', confidence: 0.9, decision: 'needs_review' })],
  });
  assert.equal(rc.needsExpanding(card), true);
});

test('空字段**不算**异常 —— 否则等于没折叠', () => {
  // 这是方案 3 的核心：空 = 机器没读到，人不需要现在做决定；
  // 可疑 = 机器给了个值但可能错，人**必须**判断。混在一起会让每张卡都展开。
  const card = cleanCard({
    fields: [
      field({ field_name: 'date', value: '', confidence: 0, decision: 'reject', reason: '该字段未抽到' }),
      field({ field_name: 'place', value: '', confidence: 0, decision: 'reject', reason: '该字段未抽到' }),
      field({ field_name: 'office', value: '', confidence: 0, decision: 'reject', reason: '该字段未抽到' }),
    ],
  });
  assert.equal(rc.needsExpanding(card), false);
});

test('有值的 reject（置信度过低）也要人看一眼', () => {
  const card = cleanCard({
    fields: [field({ field_name: 'place', value: '西玉山', confidence: 0.2, decision: 'reject' })],
  });
  assert.equal(rc.needsExpanding(card), true);
});

test('尺寸异常要展开 —— 它是几何问题，字段值上看不出来', () => {
  assert.equal(rc.needsExpanding(cleanCard({ size_suspect: true })), true);
});

// --------------------------------------------------------------------------- //
//  几何来源（ok/20）：与「字段来源」是两件事
// --------------------------------------------------------------------------- //

test('几何来自云端时要展开 —— 云端几何不可复现，且错了会让整张卡的读数一起歪', () => {
  assert.equal(rc.needsExpanding(cleanCard({ geometry_source: 'vlm' })), true);
  assert.equal(rc.needsExpanding(cleanCard({ geometry_source: 'local' })), false);
});

test('geometryFromCloud 只看几何，不被 field_source 带偏', () => {
  //: 字段来自云端、几何仍是本地的 —— B 模式下这就是**常态**，不该标"几何·云端"
  assert.equal(
    rc.geometryFromCloud(cleanCard({ field_source: 'vlm_json', geometry_source: 'local' })),
    false,
  );
  //: 反过来：几何来自云端、字段是本地读的
  assert.equal(
    rc.geometryFromCloud(cleanCard({ field_source: 'local', geometry_source: 'vlm' })),
    true,
  );
});

test('几何来源用精确匹配，不用前缀 —— 免得未来某个取值被静默吞进来', () => {
  //: 几何只有 local / vlm 两个取值。写成 startsWith('vlm') 会把这类值也吞掉，
  //: 而"吞掉"的表现只是多展开一张卡，不报错，所以必须钉住。
  assert.equal(rc.geometryFromCloud(cleanCard({ geometry_source: 'vlm_something' })), false);
  assert.equal(rc.geometryFromCloud(cleanCard({ geometry_source: '' })), false);
});

test('戳型 unknown 不展开 —— "不知道"是诚实输出，不该逼人做决定', () => {
  const card = cleanCard({
    fields: [
      field(),
      field({ field_name: 'place', value: '福建南平' }),
      field({ field_name: 'office', value: '建溪' }),
      field({ field_name: 'postmark_type', value: '', confidence: 0, decision: 'reject' }),
    ],
  });
  assert.equal(rc.needsExpanding(card), false);
});

// --------------------------------------------------------------------------- //
//  resolveCardOpen —— 人工意图优先，机器规则兜底（bug1）
// --------------------------------------------------------------------------- //

test('没被人点过的卡，按机器规则决定是否展开', () => {
  assert.equal(rc.resolveCardOpen(cleanCard(), new Map()), false, '干净卡默认折叠');
  const susp = cleanCard({
    fields: [field({ field_name: 'place', value: '西玉山', confidence: 0.2, decision: 'needs_review' })],
  });
  assert.equal(rc.resolveCardOpen(susp, new Map()), true, '带值的可疑项默认展开');
});

test('人工展开能覆盖"机器认为干净" —— 这就是那个点不开的 bug', () => {
  // 旧实现 open = needsExpanding(card) && !collapsed.has(index)：
  // 干净卡点一下只是往 collapsed 里塞索引，open 本来就是 false，**毫无反应**。
  assert.equal(rc.resolveCardOpen(cleanCard(), new Map([[0, true]])), true);
});

test('人工折叠能覆盖"机器认为可疑"（反方向也要成立）', () => {
  const susp = cleanCard({
    fields: [field({ field_name: 'place', value: '西玉山', decision: 'needs_review' })],
  });
  assert.equal(rc.resolveCardOpen(susp, new Map([[0, false]])), false);
});

test('人工意图只作用于那张卡（按 index 查，不按数组位置）', () => {
  const manual = new Map([[1, true]]);
  assert.equal(rc.resolveCardOpen(cleanCard({ index: 0 }), manual), false);
  assert.equal(rc.resolveCardOpen(cleanCard({ index: 1 }), manual), true);
});

// --------------------------------------------------------------------------- //
//  zoomViewFor —— 放大视图裁哪一块、放大多少倍（ok/15 q1）
// --------------------------------------------------------------------------- //

test('整卡视图裁的就是 bbox', () => {
  const card = cleanCard();
  const view = rc.zoomViewFor(card, { mode: 'card', viewWidth: 720, workScale: 0.25 });
  assert.deepEqual(view.box, [10, 20, 240, 400]);
});

test('邮戳视图以圆心为中心、比戳大 30%（贴边裁会切掉外圈）', () => {
  const card = cleanCard(); // circle: cx=130 cy=220 r=55
  const view = rc.zoomViewFor(card, { mode: 'postmark', viewWidth: 720, workScale: 0.25 });
  const side = 55 * 2.6;
  assert.deepEqual(view.box, [130 - side / 2, 220 - side / 2, side, side]);
});

test('没定位到圆时邮戳视图给 null（让界面退回整卡）', () => {
  const card = cleanCard({ circle: null });
  assert.equal(rc.zoomViewFor(card, { mode: 'postmark', viewWidth: 720, workScale: 0.25 }), null);
  // 整卡视图不受影响 —— 没圆也还能看整张卡
  assert.ok(rc.zoomViewFor(card, { mode: 'card', viewWidth: 720, workScale: 0.25 }));
});

test('放大倍率封顶在**原始分辨率**（再放大只是插值）', () => {
  const card = cleanCard();
  // work 尺度 0.25 → 原图是 work 的 4 倍；邮戳框 side=143，要 720/143=5.03 倍
  const capped = rc.zoomViewFor(card, { mode: 'postmark', viewWidth: 720, workScale: 0.25 });
  assert.equal(capped.capped, true, '要 5.03 倍而原图只到 4 倍，必须封顶');
  assert.equal(capped.scale, 4);
  // 原图更大（work_scale 更小）时封顶线更高，就能拿到请求的倍率
  const free = rc.zoomViewFor(card, { mode: 'postmark', viewWidth: 720, workScale: 0.1 });
  assert.equal(free.capped, false);
  assert.ok(Math.abs(free.scale - 720 / (55 * 2.6)) < 1e-9);
});

test('work_scale 缺失/为 0 时不封顶（异常输入宁可放大也不要不显示）', () => {
  const view = rc.zoomViewFor(cleanCard(), {
    mode: 'postmark',
    viewWidth: 720,
    workScale: 0,
  });
  assert.equal(view.capped, false);
  assert.ok(Number.isFinite(view.scale) && view.scale > 0);
});

// --------------------------------------------------------------------------- //
//  结构化地点/邮局（ok/15 q4）
// --------------------------------------------------------------------------- //

test('placePathOf 把词表层级链拼出来（后端一直在返回，界面以前没显示）', () => {
  const card = cleanCard({
    fields: [
      field({ field_name: 'place', value: '江西玉山', place_path: ['中华人民共和国', '江西省', '上饶市', '玉山县'] }),
    ],
  });
  assert.equal(rc.placePathOf(card), '中华人民共和国 / 江西省 / 上饶市 / 玉山县');
});

test('placePathOf 没命中词表时给空串（不编一条链出来）', () => {
  const card = cleanCard({
    fields: [field({ field_name: 'place', value: '福建南火', place_path: [] })],
  });
  assert.equal(rc.placePathOf(card), '');
});

test('选定的结构化地点/邮局进载荷；没选就送 null（交给服务端解析）', () => {
  const p = page([cleanCard()]);
  let draft = rc.pageDraftFromResponse('a-1', p);

  //: 没选 → null，服务端按文本解析（同一条词表规则）
  let payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards[0].place_id, null);
  assert.equal(payload.cards[0].post_office_id, null);

  const place = { id: 'pl-1', name: '玉山县' };
  const office = { id: 'of-1', name: '三清山邮政所' };
  draft = rc.withStructuredPlace(draft, 0, place);
  draft = rc.withStructuredOffice(draft, 0, office);
  payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards[0].place_id, 'pl-1');
  assert.equal(payload.cards[0].post_office_id, 'of-1');
});

test('清掉结构化选择 → 回到"交给服务端解析"', () => {
  const p = page([cleanCard()]);
  let draft = rc.pageDraftFromResponse('a-1', p);
  draft = rc.withStructuredPlace(draft, 0, { id: 'pl-1', name: '玉山县' });
  draft = rc.withStructuredPlace(draft, 0, null);
  assert.equal(draft.cards[0].placeId, '');
  assert.equal(draft.cards[0].place, null);
  assert.equal(rc.buildConfirmSubmission(draft, DEFAULTS).cards[0].place_id, null);
});

// --------------------------------------------------------------------------- //
//  自动挂：识别响应 links → 预填草稿（ok/18 §3.3，用户 2026-10-12）
// --------------------------------------------------------------------------- //

const LINKS = {
  place_id: 'pl-1',
  place_name: '福州市',
  office_id: 'of-1',
  office_name: '福屿邮政所',
  office_place_name: '福州市',
  note: "地点：'福州' → 福建省 / 福州市；局名：'福屿' → 福屿邮政所（在「福州市」子树内唯一）",
};

test('识别结果里的 links 会被**预填**成结构化 id（人不必手填）', () => {
  const card = cleanCard({ links: LINKS });
  const draft = rc.pageDraftFromResponse('a-1', page([card]));
  assert.equal(draft.cards[0].placeId, 'pl-1');
  assert.equal(draft.cards[0].officeId, 'of-1');
  //: 预填的 id 必须随确认载荷送出去（这才是"自动挂"落到底）
  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards[0].place_id, 'pl-1');
  assert.equal(payload.cards[0].post_office_id, 'of-1');
});

test('响应里没有 links 时保持"交给服务端解析"（不编 id）', () => {
  const draft = rc.pageDraftFromResponse('a-1', page([cleanCard()]));
  assert.equal(draft.cards[0].placeId, '');
  assert.equal(draft.cards[0].officeId, '');
  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards[0].place_id, null);
});

test('改了地名文本 → 预填的地点 id **当场作废**（否则会挂到按旧文本解析的结果）', () => {
  const card = cleanCard({ links: LINKS });
  const before = rc.pageDraftFromResponse('a-1', page([card]));
  const after = rc.withFieldValue(before, 0, 'place', '福建南平');
  assert.equal(after.cards[0].placeId, '', '文本改了，id 必须作废');
  assert.equal(after.cards[0].place, null);
  assert.equal(
    rc.buildConfirmSubmission(after, DEFAULTS).cards[0].place_id,
    null,
    '作废之后交给服务端按新文本解析',
  );
  //: 局名不受影响 —— 人只改了地名
  assert.equal(after.cards[0].officeId, 'of-1');
});

test('改了局名文本 → 局 id 作废；但改**戳号**不作废（戳号不是局名的一部分）', () => {
  const card = cleanCard({ links: LINKS });
  const before = rc.pageDraftFromResponse('a-1', page([card]));
  assert.equal(rc.withFieldValue(before, 0, 'office', '西洪').cards[0].officeId, '');
  assert.equal(
    rc.withFieldValue(before, 0, 'office_no', '2').cards[0].officeId,
    'of-1',
    '戳号变化不影响"这是哪一家局"',
  );
});

test('结构化选择只作用于那张卡（纯函数、不改原草稿）', () => {
  const p = page([cleanCard(), cleanCard({ index: 1 })]);
  const draft = rc.pageDraftFromResponse('a-1', p);
  const next = rc.withStructuredOffice(draft, 1, { id: 'of-9', name: '建溪邮政支局' });
  assert.equal(next.cards[1].officeId, 'of-9');
  assert.equal(next.cards[0].officeId, '');
  assert.equal(draft.cards[1].officeId, '', '原草稿不许被就地改');
});

// --------------------------------------------------------------------------- //
//  currentFieldValue / officeLabelOf —— 主行跟着修改走（ok/18 §3.2）
// --------------------------------------------------------------------------- //

test('currentFieldValue：没改过就是机器值，改过就是人的值', () => {
  const card = cleanCard({ fields: [field({ field_name: 'place', value: '福建南火' })] });
  const draft = rc.pageDraftFromResponse('a-1', page([card])).cards[0];
  assert.equal(rc.currentFieldValue(card, draft, 'place'), '福建南火');

  const edited = rc.withFieldValue(
    rc.pageDraftFromResponse('a-1', page([card])),
    0,
    'place',
    '福建南平',
  ).cards[0];
  assert.equal(rc.currentFieldValue(card, edited, 'place'), '福建南平');
});

test('currentFieldValue：清空是有效值，**不许**回落到机器值', () => {
  // 用 `??` 而不是 `||` 的原因就在这条 —— 清空代表"我确认这里没有值"
  const card = cleanCard({ fields: [field({ field_name: 'office', value: '建溪' })] });
  const draft = rc.withFieldValue(
    rc.pageDraftFromResponse('a-1', page([card])),
    0,
    'office',
    '',
  ).cards[0];
  assert.equal(rc.currentFieldValue(card, draft, 'office'), '');
});

test('currentFieldValue：响应里没有这个字段时给空串（不炸）', () => {
  const card = cleanCard();
  const draft = rc.pageDraftFromResponse('a-1', page([card])).cards[0];
  assert.equal(rc.currentFieldValue(card, draft, 'inscription'), '');
});

test('officeLabelOf 是"局名+戳号"的唯一合成处（主行与展开区不再各拼一遍）', () => {
  assert.equal(rc.officeLabelOf('三清山', '1'), '三清山1');
  assert.equal(rc.officeLabelOf('三清山', ''), '三清山');
  // 没有局名时，孤零零的戳号没有意义
  assert.equal(rc.officeLabelOf('', '1'), '');
  // 与"从响应里取"那条路给同一个结果
  const card = cleanCard({
    fields: [
      field({ field_name: 'office', value: '中亭街' }),
      field({ field_name: 'office_no', value: '1' }),
    ],
  });
  assert.equal(rc.officeLabel(card), rc.officeLabelOf('中亭街', '1'));
});

// --------------------------------------------------------------------------- //
//  pageDraftFromResponse
// --------------------------------------------------------------------------- //

test('跑戳日由识别日期预填', () => {
  const draft = rc.pageDraftFromResponse('a-1', page([cleanCard()]));
  assert.equal(draft.assetId, 'a-1');
  assert.equal(draft.cards[0].visitDate, '2026-09-25');
  assert.equal(draft.cards[0].excluded, false);
});

test('日期字段带时间时，跑戳日只取 YYYY-MM-DD', () => {
  // 实测 date 的值形如 "2026-10-01 17:00"（时间码是戳面的一部分）。
  // 原样送去 ConfirmCardIn.visit_date 会被 Pydantic 判成 422，而报错说的是
  // "不是合法日期"，跟"多了一个时间"没有字面关联 —— 这条测试就是钉它。
  const card = cleanCard({ fields: [field({ value: '2026-10-01 17:00' })] });
  const draft = rc.pageDraftFromResponse('a-1', page([card]));
  assert.equal(draft.cards[0].visitDate, '2026-10-01');

  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards[0].visit_date, '2026-10-01');
});

test('visitDateFromFieldValue 取不出日期就给空串（不猜）', () => {
  assert.equal(rc.visitDateFromFieldValue('2026-10-01 17:00'), '2026-10-01');
  // 用点号分隔的戳面原文（实测有 3 种变体）取不出 —— 交给用户填
  assert.equal(rc.visitDateFromFieldValue('2026.10.01.09.'), '');
  assert.equal(rc.visitDateFromFieldValue(''), '');
  // 这里**只验证"取出了这段字面量"**，不验证它是不是一个真实存在的日期：
  // 日期字段的机械校验在后端（正则 + 月/日范围），读不出合法日期的字段
  // 根本不会拿到 auto_accept。前端再做一套只会是第二个真相来源。
  assert.equal(rc.visitDateFromFieldValue('2026-13-45'), '2026-13-45');
});

test('识别没给出日期时跑戳日留空（**不编一个**）', () => {
  // 替用户编日期会污染 ix_card_visit_date_status 背后的统计
  const card = cleanCard({ fields: [field({ value: '', confidence: 0, decision: 'reject' })] });
  const draft = rc.pageDraftFromResponse('a-1', page([card]));
  assert.equal(draft.cards[0].visitDate, '');
});

test('草稿保留机器值，不许被界面上的显示值污染', () => {
  const draft = rc.pageDraftFromResponse('a-1', page([cleanCard()]));
  const f = rc.draftFieldOf(draft.cards[0], 'place');
  assert.equal(f.machineValue, '福建南平');
  assert.equal(f.finalValue, '福建南平');
  assert.equal(f.edited, false);
});

// --------------------------------------------------------------------------- //
//  withFieldValue：改回机器值 = 没改过
// --------------------------------------------------------------------------- //

test('改一个字段后 edited 为真、finalValue 变了、machineValue 不变', () => {
  const draft = rc.pageDraftFromResponse('a-1', page([cleanCard()]));
  const next = rc.withFieldValue(draft, 0, 'place', '福建南平市');
  const f = rc.draftFieldOf(next.cards[0], 'place');
  assert.equal(f.finalValue, '福建南平市');
  assert.equal(f.edited, true);
  assert.equal(f.machineValue, '福建南平', '机器值是要进 observation 的证据，编辑不许动它');
});

test('改回机器值就不再算"已改"', () => {
  // 否则"✎ 已改"会一直挂着，而人以为自己撤销了
  const draft = rc.pageDraftFromResponse('a-1', page([cleanCard()]));
  const changed = rc.withFieldValue(draft, 0, 'place', '福建南平市');
  const reverted = rc.withFieldValue(changed, 0, 'place', '福建南平');
  const f = rc.draftFieldOf(reverted.cards[0], 'place');
  assert.equal(f.edited, false);
  assert.equal(f.finalValue, '福建南平');
});

test('原草稿不被就地修改（纯函数）', () => {
  const draft = rc.pageDraftFromResponse('a-1', page([cleanCard()]));
  rc.withFieldValue(draft, 0, 'place', 'X');
  assert.equal(rc.draftFieldOf(draft.cards[0], 'place').finalValue, '福建南平');
});

// --------------------------------------------------------------------------- //
//  pagePlan
// --------------------------------------------------------------------------- //

test('pagePlan 把空卡单独数出来（对冲方案 3 的代价）', () => {
  const empty = cleanCard({
    index: 1,
    fields: [
      field({ value: '', confidence: 0, decision: 'reject' }),
      field({ field_name: 'place', value: '', confidence: 0, decision: 'reject' }),
      field({ field_name: 'office', value: '', confidence: 0, decision: 'reject' }),
    ],
  });
  const p = page([cleanCard(), empty]);
  const plan = rc.pagePlan(p, rc.pageDraftFromResponse('a-1', p));
  assert.equal(plan.total, 2);
  assert.equal(plan.empty, 1);
  assert.equal(plan.clean, 2, '两张都不展开；"空"不是"可疑"');
  assert.equal(plan.rejectedFields, 3);
});

test('pagePlan 数出缺跑戳日的卡 —— 它们提交不了', () => {
  const noDate = cleanCard({
    index: 1,
    fields: [field({ value: '', confidence: 0, decision: 'reject' })],
  });
  const p = page([cleanCard(), noDate]);
  const plan = rc.pagePlan(p, rc.pageDraftFromResponse('a-1', p));
  assert.equal(plan.missingVisitDate, 1);
});

test('pagePlan 数出**缺戳面原文**的卡 —— 它从 2026-10-11 起是必填的', () => {
  // 为什么必填：`Postmark.inscription` 是 NOT NULL，而戳面原文就是这枚戳的
  // 证据本身（ok/16 §4.3）。空着提交会被后端 422，所以界面要先拦住。
  const withIns = cleanCard({
    fields: [...cleanCard().fields, field({ field_name: 'inscription', value: '建溪1' })],
  });
  const emptyIns = cleanCard({
    index: 1,
    fields: [...cleanCard().fields, field({ field_name: 'inscription', value: '' })],
  });
  const p = page([withIns, emptyIns]);
  const plan = rc.pagePlan(p, rc.pageDraftFromResponse('a-1', p));
  assert.equal(plan.missingInscription, 1);
});

test('屏蔽掉的卡不计入"缺戳面原文" —— 它不打算入库', () => {
  const emptyIns = cleanCard({
    fields: [...cleanCard().fields, field({ field_name: 'inscription', value: '' })],
  });
  const p = page([emptyIns]);
  const draft = rc.withCardExcluded(rc.pageDraftFromResponse('a-1', p), 0, true);
  assert.equal(rc.pagePlan(p, draft).missingInscription, 0);
});

test('pagePlan 把屏蔽的卡排除在"将入库"之外', () => {
  const p = page([cleanCard(), cleanCard({ index: 1 })]);
  const draft = rc.withCardExcluded(rc.pageDraftFromResponse('a-1', p), 1, true);
  const plan = rc.pagePlan(p, draft);
  assert.equal(plan.included, 1);
  assert.equal(plan.excluded, 1);
});

// --------------------------------------------------------------------------- //
//  buildConfirmSubmission —— 载荷不能错的地方
// --------------------------------------------------------------------------- //

test('machine_value 用识别原值，final_value 用界面上的值', () => {
  // 这条错了**不会报错**：后端会把"人改的"也当成"机器读到的"，
  // correction（长期标注资产）就废了
  const p = page([cleanCard()]);
  const draft = rc.withFieldValue(rc.pageDraftFromResponse('a-1', p), 0, 'place', '福建南平市');
  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);

  const place = payload.cards[0].fields.find((f) => f.field_name === 'place');
  assert.equal(place.machine_value, '福建南平');
  assert.equal(place.final_value, '福建南平市');
  assert.equal(place.machine_confidence, 0.95);
  assert.equal(place.machine_decision, 'auto_accept');
});

test('只提交白名单里的字段名', () => {
  // 前端自造的字段名混进 observation 档案之后，就再也分不清哪些是机器读的了
  const card = cleanCard({
    fields: [
      ...cleanCard().fields,
      field({ field_name: 'stamp_region', value: '右上角', confidence: 0.5, decision: 'needs_review' }),
    ],
  });
  const p = page([card]);
  const payload = rc.buildConfirmSubmission(rc.pageDraftFromResponse('a-1', p), DEFAULTS);
  const names = payload.cards[0].fields.map((f) => f.field_name);
  assert.ok(!names.includes('stamp_region'), `不该提交 stamp_region，实际：${names.join(',')}`);
  assert.deepEqual(names.sort(), ['date', 'office', 'office_no', 'place', 'postmark_type']);
});

test('戳面原文 / 日期原文 / 时码都在载荷里 —— 后端要拿它们建戳', () => {
  // ok/16 §5.2 的映射：inscription→inscription、date_text→postmark_date_text、
  // time_code→time_code。白名单漏掉任何一个，那一列就会静默永远是空。
  const card = cleanCard({
    fields: [
      ...cleanCard().fields,
      field({ field_name: 'inscription', value: '建溪1' }),
      field({ field_name: 'date_text', value: '2026.09.25.15' }),
      field({ field_name: 'time_code', value: '15' }),
    ],
  });
  const p = page([card]);
  const payload = rc.buildConfirmSubmission(rc.pageDraftFromResponse('a-1', p), DEFAULTS);
  const byName = Object.fromEntries(
    payload.cards[0].fields.map((f) => [f.field_name, f.final_value]),
  );
  assert.equal(byName.inscription, '建溪1');
  assert.equal(byName.date_text, '2026.09.25.15');
  assert.equal(byName.time_code, '15');
});

test('人改过的戳面原文：机器值仍送原文（correction 要留痕）', () => {
  const card = cleanCard({
    fields: [
      ...cleanCard().fields,
      field({ field_name: 'inscription', value: '建淡1', confidence: 0.9 }),
    ],
  });
  const p = page([card]);
  let draft = rc.pageDraftFromResponse('a-1', p);
  draft = rc.withFieldValue(draft, 0, 'inscription', '建溪1');
  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  const ins = payload.cards[0].fields.find((f) => f.field_name === 'inscription');
  assert.equal(ins.machine_value, '建淡1', '机器值不许被界面上的值污染');
  assert.equal(ins.final_value, '建溪1');
});

test('机器没读到的值送 null 而不是空串', () => {
  // 空串会被记成"机器读到空"，与"机器什么都没读到"不是一回事
  const card = cleanCard({
    fields: [
      field({ value: '', confidence: 0, decision: 'reject' }),
      field({ field_name: 'place', value: '福建南平' }),
      field({ field_name: 'office', value: '建溪' }),
    ],
  });
  const p = page([card]);
  const payload = rc.buildConfirmSubmission(rc.pageDraftFromResponse('a-1', p), DEFAULTS);
  const date = payload.cards[0].fields.find((f) => f.field_name === 'date');
  assert.equal(date.machine_value, null);
  assert.equal(date.final_value, '');
});

test('屏蔽的卡**照样送出去**，只是 excluded=true', () => {
  // 后端用 payload.cards 的长度算 excluded_cards；把它们滤掉那个计数会永远是 0
  const p = page([cleanCard(), cleanCard({ index: 1 })]);
  const draft = rc.withCardExcluded(rc.pageDraftFromResponse('a-1', p), 1, true);
  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards.length, 2);
  assert.equal(payload.cards[0].excluded, false);
  assert.equal(payload.cards[1].excluded, true);
});

test('缺跑戳日的卡送 visit_date = null —— 让服务端去拒绝，不要自己编', () => {
  const p = page([cleanCard()]);
  const draft = rc.withVisitDate(rc.pageDraftFromResponse('a-1', p), 0, '');
  const payload = rc.buildConfirmSubmission(draft, DEFAULTS);
  assert.equal(payload.cards[0].visit_date, null);
});

test('默认值按求戳信（mail_request / collected）走', () => {
  const p = page([cleanCard()]);
  const payload = rc.buildConfirmSubmission(rc.pageDraftFromResponse('a-1', p), DEFAULTS);
  assert.deepEqual(payload.defaults, { source: 'mail_request', status: 'collected' });
  assert.equal(payload.confirm_again, false);
  assert.equal(payload.pipeline_version, 'p2-0.1.0');
  assert.equal(payload.asset_id, 'a-1');
});

test('buildSingleCardSubmission 把其余卡全部屏蔽着一起送', () => {
  // 逐卡路径不能"只发一张" —— 那样 excluded_cards 就丢了
  const p = page([cleanCard(), cleanCard({ index: 1 }), cleanCard({ index: 2 })]);
  const payload = rc.buildSingleCardSubmission(rc.pageDraftFromResponse('a-1', p), 1, DEFAULTS);
  assert.equal(payload.cards.length, 3);
  assert.deepEqual(
    payload.cards.map((c) => c.excluded),
    [true, false, true],
  );
});

test('buildSingleCardSubmission 不改原草稿', () => {
  const p = page([cleanCard(), cleanCard({ index: 1 })]);
  const draft = rc.pageDraftFromResponse('a-1', p);
  rc.buildSingleCardSubmission(draft, 1, DEFAULTS);
  assert.deepEqual(
    draft.cards.map((c) => c.excluded),
    [false, false],
  );
});

test('confirm_again 按调用方给的值透传', () => {
  const p = page([cleanCard()]);
  const payload = rc.buildConfirmSubmission(rc.pageDraftFromResponse('a-1', p), DEFAULTS, {
    confirmAgain: true,
  });
  assert.equal(payload.confirm_again, true);
});

// --------------------------------------------------------------------------- //
//  计数
// --------------------------------------------------------------------------- //

test('editedCountOf 只数这张卡上真改过的字段', () => {
  const p = page([cleanCard(), cleanCard({ index: 1 })]);
  let draft = rc.pageDraftFromResponse('a-1', p);
  assert.equal(rc.editedCountOf(draft.cards[0]), 0);
  draft = rc.withFieldValue(draft, 0, 'place', 'X');
  draft = rc.withFieldValue(draft, 0, 'office', 'Y');
  assert.equal(rc.editedCountOf(draft.cards[0]), 2);
  assert.equal(rc.editedCountOf(draft.cards[1]), 0, '另一张卡不受影响');
  draft = rc.withFieldValue(draft, 0, 'place', '福建南平'); // 改回去
  assert.equal(rc.editedCountOf(draft.cards[0]), 1);
});
