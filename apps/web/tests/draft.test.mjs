/**
 * `draft.ts`（草稿 ⇄ 请求体）的测试。
 *
 * **为什么用这个方式跑**：`apps/web` 没有测试运行器，而本项目的开发环境网络
 * 受限，装不了 vitest 之类的东西 —— 装不上、也就验证不了，把没验证过的测试
 * 脚手架写进 CI 是更糟的选择。
 *
 * 所以走一条**零新依赖**的路子：`draft.ts` 的导入全是 `import type`，
 * 用仓库里已有的 `tsc` 把它单独编译成自包含的 JS，再用 Node 内置的
 * `node:assert` 断言。`npm run test:pure` 会把这两步串起来。
 *
 * **测的是哪一类问题**：草稿层是"用户输入"与"后端约束"之间的翻译层。
 * 它写错了不会崩，只会把错的载荷发出去 —— 例如把面值从字符串转成 number
 * （后端是 Decimal，浮点会丢精度）、或忘了把 `''` 转成 `null`（后端会收到
 * 空串而不是"未填"）。这类问题在界面上表现为"某个字段保存后变了样"。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));

// 必须走 pathToFileURL：Windows 上 path.join 得到的是 `D:\...`，
// 而 ESM 的 import() 只接受 file: / data: / node: 三种 scheme。
// 直接传路径会报 ERR_UNSUPPORTED_ESM_URL_SCHEME。
const draft = await import(
  pathToFileURL(path.join(here, '..', '.test-build', 'features', 'cards', 'draft.js')).href
);

// --------------------------------------------------------------------------- //
//  夹具：与后端 PostmarkRead / StampRead / CardRead 同形
// --------------------------------------------------------------------------- //

const place = { id: 'pl-1', name: '福建省', level: 1, level_label: '省级', path: '/CN/pl-1/' };
const office = { id: 'of-1', name: '建阳区邮政支局', place_id: 'pl-1' };

function postmarkRead(over = {}) {
  return {
    id: 'pm-1',
    card_id: 'c-1',
    postmark_type: 'daily',
    inscription: '福建南平 建溪1',
    postmark_date: '2026-09-25',
    postmark_date_text: '2026.09.25.15',
    time_code: '15',
    postmark_no: '1',
    post_office_id: 'of-1',
    place_id: 'pl-1',
    is_color: false,
    color_layers: null,
    provenance: 'real_scan',
    is_clean: null,
    image_asset_id: null,
    note: null,
    tag_ids: ['t-1'],
    tags: [],
    place,
    post_office: office,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...over,
  };
}

function stampRead(over = {}) {
  return {
    id: 'st-1',
    card_id: 'c-1',
    catalog_id: null,
    catalog_ref: '普30',
    catalog_system: 'china',
    name: '保护森林',
    face_value: '0.10',
    currency: 'CNY',
    issue_date: '2002-02-01',
    image_asset_id: null,
    note: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...over,
  };
}

function cardRead(over = {}) {
  return {
    id: 'c-1',
    visit_date: '2026-09-25',
    received_date: null,
    source: 'field_trip',
    status: 'collected',
    title: null,
    note: null,
    image_asset_id: null,
    tag_ids: [],
    tags: [],
    postmarks: [postmarkRead()],
    stamps: [stampRead()],
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...over,
  };
}

// --------------------------------------------------------------------------- //
//  服务端 → 草稿
// --------------------------------------------------------------------------- //

test('cardToDraft: null 变成空串，草稿里所有字段都是 string', () => {
  const d = draft.cardToDraft(cardRead());
  assert.equal(d.received_date, '');
  assert.equal(d.title, '');
  assert.equal(d.note, '');
  assert.equal(d.image_asset_id, '');
  assert.deepEqual(d.tag_ids, []);
  for (const key of ['visit_date', 'received_date', 'source', 'status', 'title', 'note']) {
    assert.equal(typeof d[key], 'string', `${key} 应当是 string`);
  }
});

test('cardToDraft: color_layers 的 null 与数字都要变成 string', () => {
  const a = draft.cardToDraft(cardRead({ postmarks: [postmarkRead({ color_layers: null })] }));
  assert.equal(a.postmarks[0].color_layers, '');
  const b = draft.cardToDraft(cardRead({ postmarks: [postmarkRead({ color_layers: 3 })] }));
  assert.equal(b.postmarks[0].color_layers, '3');
});

test('cardToDraft: 已有记录的 key 用它的 id（保证列表渲染稳定）', () => {
  const d = draft.cardToDraft(cardRead());
  assert.equal(d.postmarks[0].key, 'pm-1');
  assert.equal(d.postmarks[0].id, 'pm-1');
  assert.equal(d.stamps[0].key, 'st-1');
  assert.equal(d.stamps[0].catalog_id, '');
});

test('cardToDraft: 面值原样保留为字符串', () => {
  const d = draft.cardToDraft(cardRead({ stamps: [stampRead({ face_value: '1.20' })] }));
  assert.equal(d.stamps[0].face_value, '1.20');
});

// --------------------------------------------------------------------------- //
//  草稿 → 请求体
// --------------------------------------------------------------------------- //

test('postmarkPayload: 空串变成 null，而不是空串', () => {
  const p = draft.postmarkPayload(draft.emptyPostmark());
  assert.equal(p.postmark_date_text, null);
  assert.equal(p.time_code, null);
  assert.equal(p.postmark_no, null);
  assert.equal(p.note, null);
  assert.equal(p.post_office_id, null);
  assert.equal(p.place_id, null);
  assert.equal(p.image_asset_id, null);
  assert.equal(p.postmark_date, null);
});

test('postmarkPayload: 戳面原文去掉首尾空白（但不过度处理内部空格）', () => {
  const d = { ...draft.emptyPostmark(), inscription: '  福建南平 建溪1  ' };
  assert.equal(draft.postmarkPayload(d).inscription, '福建南平 建溪1');
});

test('postmarkPayload: color_layers 从字符串转成 number | null', () => {
  const cases = [
    ['', null],
    ['3', 3],
    ['12', 12],
    ['abc', null],
    ['3.5', 3.5],
  ];
  for (const [input, expected] of cases) {
    const d = { ...draft.emptyPostmark(), color_layers: input };
    assert.equal(draft.postmarkPayload(d).color_layers, expected, `color_layers=${input}`);
  }
});

test('postmarkPayload: is_clean 的三态要原样传（null 表示未评价）', () => {
  const yes = { ...draft.emptyPostmark(), is_clean: true };
  const no = { ...draft.emptyPostmark(), is_clean: false };
  const none = { ...draft.emptyPostmark(), is_clean: null };
  assert.equal(draft.postmarkPayload(yes).is_clean, true);
  assert.equal(draft.postmarkPayload(no).is_clean, false);
  assert.equal(draft.postmarkPayload(none).is_clean, null);
});

test('stampPayload: ★ 面值必须是**字符串**（后端是 Decimal，转 number 会丢精度）', () => {
  const d = { ...draft.emptyStamp(), face_value: '0.10' };
  const p = draft.stampPayload(d);
  assert.equal(typeof p.face_value, 'string');
  assert.equal(p.face_value, '0.10');
  // 0.1 + 0.2 那类误差不该出现：字符串进、字符串出
  assert.equal(draft.stampPayload({ ...draft.emptyStamp(), face_value: '1.10' }).face_value, '1.10');
});

test('stampPayload: 币种去空白并大写', () => {
  const d = { ...draft.emptyStamp(), currency: ' cny ' };
  assert.equal(draft.stampPayload(d).currency, 'CNY');
});

test('stampPayload: 币种留空则传 null，而不是空串', () => {
  const d = { ...draft.emptyStamp(), currency: '   ' };
  assert.equal(draft.stampPayload(d).currency, null);
});

test('stampPayload: 标准票关联原样传递；未关联时传 null', () => {
  const linked = { ...draft.emptyStamp(), catalog_id: 'cat-1' };
  assert.equal(draft.stampPayload(linked).catalog_id, 'cat-1');
  const empty = { ...draft.emptyStamp(), catalog_id: '' };
  assert.equal(draft.stampPayload(empty).catalog_id, null);
});

test('stampPayload: catalog_system 留空则传 null', () => {
  const d = { ...draft.emptyStamp(), catalog_system: '' };
  assert.equal(draft.stampPayload(d).catalog_system, null);
});

test('draftToCardCreate 带上邮戳与邮票；draftToCardUpdate **不带**', () => {
  const d = draft.cardToDraft(cardRead());
  const create = draft.draftToCardCreate(d);
  const update = draft.draftToCardUpdate(d);

  assert.equal(create.postmarks.length, 1);
  assert.equal(create.stamps.length, 1);
  // 更新时邮戳与邮票走各自的端点，卡片的 PATCH 里不能有它们
  assert.equal('postmarks' in update, false);
  assert.equal('stamps' in update, false);
});

test('draftToCardCreate 丢掉戳面原文为空的邮戳', () => {
  const d = {
    ...draft.cardToDraft(cardRead()),
    postmarks: [
      { ...draft.emptyPostmark(), inscription: '有内容' },
      { ...draft.emptyPostmark(), inscription: '   ' },
    ],
  };
  assert.equal(draft.draftToCardCreate(d).postmarks.length, 1);
});

// --------------------------------------------------------------------------- //
//  校验（必须与后端 schemas.py 的约束一致）
// --------------------------------------------------------------------------- //

test('validateDraft: 逐项拦下不合法的输入', () => {
  const good = { ...draft.cardToDraft(cardRead()), visit_date: '2026-09-25' };
  assert.equal(draft.validateDraft(good), null);

  assert.match(draft.validateDraft({ ...good, visit_date: '' }), /跑戳日期/);
  assert.match(draft.validateDraft({ ...good, postmarks: [] }), /至少需要一条邮戳/);
  assert.match(
    draft.validateDraft({
      ...good,
      postmarks: [{ ...draft.emptyPostmark(), inscription: ' ' }],
    }),
    /戳面原文/,
  );
});

test('validateDraft: 套色层数必须是 1–12 的整数（与后端 ge=1 le=12 对齐）', () => {
  const base = { ...draft.cardToDraft(cardRead()), visit_date: '2026-09-25' };
  const withLayers = (v) => ({
    ...base,
    postmarks: [{ ...draft.emptyPostmark(), inscription: 'x', color_layers: v }],
  });
  for (const ok of ['1', '12', '']) {
    assert.equal(draft.validateDraft(withLayers(ok)), null, `color_layers=${ok} 应当通过`);
  }
  for (const bad of ['0', '13', '3.5', 'abc', '-1']) {
    assert.match(draft.validateDraft(withLayers(bad)), /套色层数/, `color_layers=${bad} 应当被拦下`);
  }
});

test('validateDraft: 面值只接受纯数值（不要带「元」「分」）', () => {
  const base = { ...draft.cardToDraft(cardRead()), visit_date: '2026-09-25' };
  const withFace = (v) => ({ ...base, stamps: [{ ...draft.emptyStamp(), face_value: v }] });
  for (const ok of ['', '0.10', '1', '120']) {
    assert.equal(draft.validateDraft(withFace(ok)), null, `面值 ${ok} 应当通过`);
  }
  for (const bad of ['1元', '0.1.2', 'abc', '-1', '1,20']) {
    assert.match(draft.validateDraft(withFace(bad)), /面值/, `面值 ${bad} 应当被拦下`);
  }
});

test('validateDraft: 币种必须是 3 位字母（与后端 min/max_length=3 对齐）', () => {
  const base = { ...draft.cardToDraft(cardRead()), visit_date: '2026-09-25' };
  const withCur = (v) => ({ ...base, stamps: [{ ...draft.emptyStamp(), currency: v }] });
  for (const ok of ['', 'CNY', 'usd']) {
    assert.equal(draft.validateDraft(withCur(ok)), null, `币种 ${ok} 应当通过`);
  }
  for (const bad of ['CN', 'CNYY', '12', '人民币']) {
    assert.match(draft.validateDraft(withCur(bad)), /币种/, `币种 ${bad} 应当被拦下`);
  }
});

// --------------------------------------------------------------------------- //
//  往返：服务端 → 草稿 → 请求体，可写字段应当一模一样
//
//  这是最有价值的一条 —— 它锁住"草稿层不会悄悄改数据"这个性质。
// --------------------------------------------------------------------------- //

test('往返：邮戳的可写字段经过草稿层后不变', () => {
  const pm = postmarkRead({ color_layers: 3, note: '边角有污渍', is_clean: true });
  const back = draft.postmarkPayload(draft.cardToDraft(cardRead({ postmarks: [pm] })).postmarks[0]);

  assert.equal(back.postmark_type, pm.postmark_type);
  assert.equal(back.inscription, pm.inscription);
  assert.equal(back.postmark_date, pm.postmark_date);
  assert.equal(back.postmark_date_text, pm.postmark_date_text);
  assert.equal(back.time_code, pm.time_code);
  assert.equal(back.postmark_no, pm.postmark_no);
  assert.equal(back.post_office_id, pm.post_office_id);
  assert.equal(back.place_id, pm.place_id);
  assert.equal(back.is_color, pm.is_color);
  assert.equal(back.color_layers, pm.color_layers);
  assert.equal(back.provenance, pm.provenance);
  assert.equal(back.is_clean, pm.is_clean);
  assert.equal(back.image_asset_id, pm.image_asset_id);
  assert.equal(back.note, pm.note);
  assert.deepEqual(back.tag_ids, pm.tag_ids);
});

test('往返：null 的可选字段不会变成空串', () => {
  const pm = postmarkRead({
    postmark_date: null,
    postmark_date_text: null,
    time_code: null,
    postmark_no: null,
    place_id: null,
    post_office_id: null,
    note: null,
  });
  const back = draft.postmarkPayload(draft.cardToDraft(cardRead({ postmarks: [pm] })).postmarks[0]);
  for (const key of ['postmark_date', 'postmark_date_text', 'time_code', 'postmark_no', 'place_id', 'post_office_id', 'note']) {
    assert.equal(back[key], null, `${key} 应当是 null 而不是空串`);
  }
});

test('往返：卡片 PATCH 的可写字段经过草稿层后不变', () => {
  const card = cardRead({ received_date: '2026-10-02', title: '南平跑戳', note: '顺路', tag_ids: ['t-9'] });
  const back = draft.draftToCardUpdate(draft.cardToDraft(card));
  assert.equal(back.visit_date, card.visit_date);
  assert.equal(back.received_date, card.received_date);
  assert.equal(back.source, card.source);
  assert.equal(back.status, card.status);
  assert.equal(back.title, card.title);
  assert.equal(back.note, card.note);
  assert.equal(back.image_asset_id, card.image_asset_id);
  assert.deepEqual(back.tag_ids, card.tag_ids);
});

// --------------------------------------------------------------------------- //
//  其它
// --------------------------------------------------------------------------- //

test('serializeDraft: 相同内容得到相同字符串，改一个字段就不同（脏检查靠它）', () => {
  const a = draft.cardToDraft(cardRead());
  const b = draft.cardToDraft(cardRead());
  assert.equal(draft.serializeDraft(a), draft.serializeDraft(b));
  assert.notEqual(
    draft.serializeDraft(a),
    draft.serializeDraft({ ...a, title: '改过了' }),
  );
});

test('emptyCard 默认带一个邮戳（省一次点击），但不带邮票', () => {
  const d = draft.emptyCard();
  assert.equal(d.postmarks.length, 1);
  assert.equal(d.stamps.length, 0);
  assert.equal(d.id, null);
});

test('newKey 每次不同', () => {
  const seen = new Set();
  for (let i = 0; i < 50; i += 1) seen.add(draft.newKey());
  assert.equal(seen.size, 50);
});

test('applyTemplate: 只搬"环境性"字段，**不搬戳面原文与日期**', () => {
  const source = cardRead({
    source: 'mail_request',
    status: 'pending',
    tag_ids: ['t-7'],
    postmarks: [postmarkRead({ inscription: '别人的戳', postmark_date: '2020-01-01' })],
  });
  const target = draft.emptyCard();
  const out = draft.applyTemplate(target, source);

  assert.equal(out.source, 'mail_request');
  assert.equal(out.status, 'pending');
  assert.deepEqual(out.tag_ids, ['t-7']);
  assert.equal(out.postmarks[0].place_id, 'pl-1');
  assert.equal(out.postmarks[0].post_office_id, 'of-1');
  // 这些绝不能被搬过来
  assert.equal(out.postmarks[0].inscription, '');
  assert.equal(out.postmarks[0].postmark_date, '');
  // target 本身不被修改
  assert.equal(target.source, 'field_trip');
});

// --------------------------------------------------------------------------- //
//  换地点时的邮局一致性
//
//  这是一条**数据完整性**规则：邮局必须属于所选地点。写在组件里就只能靠
//  人工点界面验证，所以把它抽成了纯函数（applyPlaceChange）放在这里锁住。
// --------------------------------------------------------------------------- //

const placeA = { id: 'pl-A', name: '建阳区', level: 3, level_label: '县级', path: '/CN/pl-1/pl-A/' };
const placeB = { id: 'pl-B', name: '武夷山市', level: 3, level_label: '县级', path: '/CN/pl-1/pl-B/' };

function postmarkWithOffice(placeId, officePlaceId) {
  return {
    ...draft.emptyPostmark(),
    inscription: 'x',
    place_id: placeId,
    place: placeId === placeA.id ? placeA : placeB,
    post_office_id: 'of-1',
    post_office: { id: 'of-1', name: '建阳区邮政支局', place_id: officePlaceId, aliases: [] },
  };
}

test('applyPlaceChange: 邮局确实属于新地点时保留它', () => {
  const before = postmarkWithOffice(placeA.id, placeA.id);
  const after = draft.applyPlaceChange(before, placeA);
  assert.equal(after.post_office_id, 'of-1');
  assert.equal(after.post_office?.id, 'of-1');
});

test('applyPlaceChange: ★ 换成另一个地点时清掉不再匹配的邮局', () => {
  const before = postmarkWithOffice(placeA.id, placeA.id);
  const after = draft.applyPlaceChange(before, placeB);

  assert.equal(after.place_id, placeB.id);
  assert.equal(after.place?.id, placeB.id);
  // 不清掉的话，库里会留下"地点在武夷山、邮局在建阳"的自相矛盾记录，
  // 而后端不校验两个外键的从属关系。
  assert.equal(after.post_office_id, '', '邮局属于旧地点，必须被清掉');
  assert.equal(after.post_office, null);
});

test('applyPlaceChange: 清空地点时也清掉从属于某地的邮局', () => {
  const before = postmarkWithOffice(placeA.id, placeA.id);
  const after = draft.applyPlaceChange(before, null);
  assert.equal(after.place_id, '');
  assert.equal(after.place, null);
  assert.equal(after.post_office_id, '');
  assert.equal(after.post_office, null);
});

test('applyPlaceChange: 两边都没有地点时视为一致，保留邮局', () => {
  const before = postmarkWithOffice('', null);
  before.post_office_id = 'of-2';
  before.post_office = { id: 'of-2', name: '某主题邮局', place_id: null, aliases: [] };
  const after = draft.applyPlaceChange(before, null);
  assert.equal(after.post_office_id, 'of-2');
});

test('applyPlaceChange: 没选邮局时不出错', () => {
  const before = { ...draft.emptyPostmark(), place_id: placeA.id, place: placeA };
  const after = draft.applyPlaceChange(before, placeB);
  assert.equal(after.post_office_id, '');
  assert.equal(after.post_office, null);
});

test('applyPlaceChange: 不动其它字段（戳面原文、日期、标签…）', () => {
  const before = {
    ...postmarkWithOffice(placeA.id, placeA.id),
    inscription: '福建南平 建溪1',
    postmark_date: '2026-09-25',
    tag_ids: ['t-1', 't-2'],
    note: '边角有污渍',
  };
  const after = draft.applyPlaceChange(before, placeB);
  assert.equal(after.inscription, '福建南平 建溪1');
  assert.equal(after.postmark_date, '2026-09-25');
  assert.deepEqual(after.tag_ids, ['t-1', 't-2']);
  assert.equal(after.note, '边角有污渍');
  // 原对象不被就地修改
  assert.equal(before.place_id, placeA.id);
});
