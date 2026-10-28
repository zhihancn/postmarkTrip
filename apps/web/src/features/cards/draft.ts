/**
 * 卡片编辑的"草稿"层。
 *
 * 为什么要有这一层：编辑一张卡涉及 1 个卡片 + N 个邮戳 + M 个邮票，
 * 如果每改一个字段就立刻发一次请求，会有三个问题：
 *   ① 新建卡片时邮戳还不存在于服务端（没有 card_id）—— 必须先建卡；
 *   ② 半填状态会被写进库里，报错信息也会打断输入；
 *   ③ Ctrl+S 就没有意义了。
 *
 * 所以：**全部改动先落在本地草稿上，一次保存时再统一 diff 提交**。
 * 草稿里所有字段都是 string（'' 表示空），提交时才转成后端的 null / number。
 */

import type {
  CardCreate,
  CardRead,
  CardUpdate,
  PostOfficeRead,
  PlaceRead,
  PostmarkCreate,
  PostmarkRead,
  Provenance,
  StampCreate,
  StampRead,
} from '../../shared/types';

export function newKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `k-${Math.random().toString(36).slice(2)}-${Date.now()}`;
}

export interface PostmarkDraft {
  key: string;
  /** null = 尚未在服务端创建 */
  id: string | null;
  postmark_type: string;
  inscription: string;
  postmark_date: string;
  postmark_date_text: string;
  time_code: string;
  postmark_no: string;
  post_office_id: string;
  place_id: string;
  is_color: boolean;
  /** 输入框用字符串，提交时转 number | null */
  color_layers: string;
  provenance: Provenance;
  /** null = 未评价 */
  is_clean: boolean | null;
  image_asset_id: string;
  note: string;
  tag_ids: string[];
  /** 仅用于显示（编辑已有记录时后端会带上；新建时由选择器给出） */
  place: PlaceRead | null;
  post_office: PostOfficeRead | null;
}

export interface StampDraft {
  key: string;
  id: string | null;
  /** 关联的标准票（M3C）；'' = 未关联 */
  catalog_id: string;
  catalog_ref: string;
  catalog_system: string;
  name: string;
  /** ★ 保持字符串：后端是 Decimal，转 number 会丢精度 */
  face_value: string;
  currency: string;
  issue_date: string;
  image_asset_id: string;
  note: string;
}

export interface CardDraft {
  /** null = 尚未在服务端创建 */
  id: string | null;
  visit_date: string;
  received_date: string;
  source: string;
  status: string;
  title: string;
  note: string;
  image_asset_id: string;
  tag_ids: string[];
  postmarks: PostmarkDraft[];
  stamps: StampDraft[];
}

export const DEFAULT_CURRENCY = 'CNY';

export function emptyPostmark(postmarkType = 'daily'): PostmarkDraft {
  return {
    key: newKey(),
    id: null,
    postmark_type: postmarkType,
    inscription: '',
    postmark_date: '',
    postmark_date_text: '',
    time_code: '',
    postmark_no: '',
    post_office_id: '',
    place_id: '',
    is_color: false,
    color_layers: '',
    provenance: 'real_scan',
    is_clean: null,
    image_asset_id: '',
    note: '',
    tag_ids: [],
    place: null,
    post_office: null,
  };
}

export function emptyStamp(): StampDraft {
  return {
    key: newKey(),
    id: null,
    catalog_id: '',
    catalog_ref: '',
    catalog_system: 'china',
    name: '',
    face_value: '',
    currency: DEFAULT_CURRENCY,
    issue_date: '',
    image_asset_id: '',
    note: '',
  };
}

function postmarkToDraft(postmark: PostmarkRead): PostmarkDraft {
  return {
    key: postmark.id,
    id: postmark.id,
    postmark_type: postmark.postmark_type,
    inscription: postmark.inscription,
    postmark_date: postmark.postmark_date ?? '',
    postmark_date_text: postmark.postmark_date_text ?? '',
    time_code: postmark.time_code ?? '',
    postmark_no: postmark.postmark_no ?? '',
    post_office_id: postmark.post_office_id ?? '',
    place_id: postmark.place_id ?? '',
    is_color: postmark.is_color,
    color_layers: postmark.color_layers === null ? '' : String(postmark.color_layers),
    provenance: postmark.provenance,
    is_clean: postmark.is_clean,
    image_asset_id: postmark.image_asset_id ?? '',
    note: postmark.note ?? '',
    tag_ids: postmark.tag_ids ?? [],
    place: postmark.place,
    post_office: postmark.post_office,
  };
}

function stampToDraft(stamp: StampRead): StampDraft {
  return {
    key: stamp.id,
    id: stamp.id,
    catalog_id: stamp.catalog_id ?? '',
    catalog_ref: stamp.catalog_ref ?? '',
    catalog_system: stamp.catalog_system ?? 'china',
    name: stamp.name ?? '',
    face_value: stamp.face_value ?? '',
    currency: stamp.currency ?? DEFAULT_CURRENCY,
    issue_date: stamp.issue_date ?? '',
    image_asset_id: stamp.image_asset_id ?? '',
    note: stamp.note ?? '',
  };
}

export function cardToDraft(card: CardRead): CardDraft {
  return {
    id: card.id,
    visit_date: card.visit_date,
    received_date: card.received_date ?? '',
    source: card.source,
    status: card.status,
    title: card.title ?? '',
    note: card.note ?? '',
    image_asset_id: card.image_asset_id ?? '',
    tag_ids: card.tag_ids ?? [],
    postmarks: card.postmarks.map(postmarkToDraft),
    stamps: card.stamps.map(stampToDraft),
  };
}

export function emptyCard(): CardDraft {
  return {
    id: null,
    visit_date: '',
    received_date: '',
    source: 'field_trip',
    status: 'collected',
    title: '',
    note: '',
    image_asset_id: '',
    tag_ids: [],
    // 默认给一个邮戳 —— 99.99999% 的卡就是一个戳，省一次点击
    postmarks: [emptyPostmark()],
    stamps: [],
  };
}

// --------------------------------------------------------------------------- //
//  草稿 → 请求体
// --------------------------------------------------------------------------- //

function text(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

function optionalDate(value: string): string | null {
  return value === '' ? null : value;
}

export function postmarkPayload(draft: PostmarkDraft): PostmarkCreate {
  const layers = draft.color_layers.trim() === '' ? null : Number(draft.color_layers);
  return {
    postmark_type: draft.postmark_type,
    inscription: draft.inscription.trim(),
    postmark_date: optionalDate(draft.postmark_date),
    postmark_date_text: text(draft.postmark_date_text),
    time_code: text(draft.time_code),
    postmark_no: text(draft.postmark_no),
    post_office_id: draft.post_office_id === '' ? null : draft.post_office_id,
    place_id: draft.place_id === '' ? null : draft.place_id,
    is_color: draft.is_color,
    color_layers: layers !== null && Number.isFinite(layers) ? layers : null,
    provenance: draft.provenance,
    is_clean: draft.is_clean,
    image_asset_id: draft.image_asset_id === '' ? null : draft.image_asset_id,
    note: text(draft.note),
    tag_ids: draft.tag_ids,
  };
}

export function stampPayload(draft: StampDraft): StampCreate {
  return {
    catalog_id: draft.catalog_id === '' ? null : draft.catalog_id,
    catalog_ref: text(draft.catalog_ref),
    catalog_system: draft.catalog_system === '' ? null : (draft.catalog_system as StampCreate['catalog_system']),
    name: text(draft.name),
    face_value: text(draft.face_value),
    currency: draft.currency.trim() === '' ? null : draft.currency.trim().toUpperCase(),
    issue_date: optionalDate(draft.issue_date),
    image_asset_id: draft.image_asset_id === '' ? null : draft.image_asset_id,
    note: text(draft.note),
  };
}

export function draftToCardCreate(draft: CardDraft): CardCreate {
  return {
    visit_date: draft.visit_date,
    received_date: optionalDate(draft.received_date),
    source: draft.source as CardCreate['source'],
    status: draft.status as CardCreate['status'],
    title: text(draft.title),
    note: text(draft.note),
    image_asset_id: draft.image_asset_id === '' ? null : draft.image_asset_id,
    tag_ids: draft.tag_ids,
    postmarks: draft.postmarks
      .filter((item) => item.inscription.trim() !== '')
      .map(postmarkPayload),
    stamps: draft.stamps.map(stampPayload),
  };
}

export function draftToCardUpdate(draft: CardDraft): CardUpdate {
  return {
    visit_date: draft.visit_date,
    received_date: optionalDate(draft.received_date),
    source: draft.source as CardUpdate['source'],
    status: draft.status as CardUpdate['status'],
    title: text(draft.title),
    note: text(draft.note),
    image_asset_id: draft.image_asset_id === '' ? null : draft.image_asset_id,
    tag_ids: draft.tag_ids,
  };
}

/** 用于脏检查：草稿是纯数据，序列化比较足够可靠。 */
export function serializeDraft(draft: CardDraft): string {
  return JSON.stringify(draft);
}

/** 至少要有一个邮戳，且每一条的戳面原文都不能为空。 */
export function validateDraft(draft: CardDraft): string | null {
  if (draft.visit_date === '') return '请填写跑戳日期';
  if (draft.postmarks.length === 0) return '至少需要一条邮戳记录';

  for (let index = 0; index < draft.postmarks.length; index += 1) {
    const item = draft.postmarks[index];
    if (item.inscription.trim() === '') {
      return `第 ${index + 1} 条邮戳缺少「戳面原文」—— 它是必填的证据字段`;
    }
    // 后端的 color_layers 有 ge=1, le=12，前端先挡一道，避免白跑一次 422
    if (item.color_layers.trim() !== '') {
      const layers = Number(item.color_layers);
      if (!Number.isInteger(layers) || layers < 1 || layers > 12) {
        return `第 ${index + 1} 条邮戳的套色层数必须是 1–12 的整数`;
      }
    }
  }

  for (let index = 0; index < draft.stamps.length; index += 1) {
    const item = draft.stamps[index];
    const faceValue = item.face_value.trim();
    if (faceValue !== '' && !/^\d+(\.\d+)?$/.test(faceValue)) {
      return `第 ${index + 1} 枚邮票的面值不是合法数字（只填数值，不要带「元」「分」）`;
    }
    // 后端 currency 是 min_length=3 / max_length=3
    const currency = item.currency.trim();
    if (currency !== '' && !/^[A-Za-z]{3}$/.test(currency)) {
      return `第 ${index + 1} 枚邮票的币种必须是 3 位 ISO 4217 代码，如 CNY`;
    }
  }

  return null;
}

/** 「复制上次的字段组合」时使用的部分复制：只搬"环境性"字段，不搬戳面原文。 */
export function applyTemplate(target: CardDraft, source: CardRead): CardDraft {
  const sourcePostmark = source.postmarks[0] ?? null;
  return {
    ...target,
    source: source.source,
    status: source.status,
    tag_ids: [...source.tag_ids],
    postmarks: target.postmarks.map((draft, index) => {
      if (index !== 0 || sourcePostmark === null) return draft;
      return {
        ...draft,
        postmark_type: sourcePostmark.postmark_type,
        place_id: sourcePostmark.place_id ?? '',
        post_office_id: sourcePostmark.post_office_id ?? '',
        is_color: sourcePostmark.is_color,
        provenance: sourcePostmark.provenance,
        place: sourcePostmark.place,
        post_office: sourcePostmark.post_office,
      };
    }),
  };
}

/**
 * 用户在地点选择器里换了一个地点（或清空）之后，这条邮戳草稿应当变成什么样。
 *
 * **为什么抽成纯函数**：这里藏着一条数据完整性约束 ——
 * 「邮局必须属于所选地点，否则要清掉」。写在组件里就只能靠人工点界面来验证；
 * 抽出来之后 `apps/web/tests/draft.test.mjs` 能直接把它锁住。
 *
 * 规则：只有当已选邮局**确实属于新地点**时才保留。
 * 两边都没有地点时视为一致（保留）—— 邮局本来就可以不属于任何地点。
 *
 * 不这么做的后果是"自相矛盾的一条记录"：地点被改成武夷山市，邮局还留着
 * 建阳区的那个。后端不校验两个外键的从属关系，所以它会安安静静地存进去 ——
 * CSV 导出的 `admin_path` 与 `post_office` 两列会互相打架，统计与将来的
 * 地图也会把它归到错的地点下。
 */
export function applyPlaceChange(
  draft: PostmarkDraft,
  place: PlaceRead | null,
): PostmarkDraft {
  const nextPlaceId = place ? place.id : '';
  const officeStillFits = (draft.post_office?.place_id ?? '') === nextPlaceId;
  return {
    ...draft,
    place_id: nextPlaceId,
    place,
    ...(officeStillFits ? {} : { post_office_id: '', post_office: null }),
  };
}
