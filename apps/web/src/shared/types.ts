/**
 * 后端契约的 TypeScript 镜像。
 *
 * 来源：`apps/api/src/postmarktrip/domain/schemas.py`
 * 原则：**字段名逐字照抄**，不要在这里做重命名或"更友好"的变形 —— 变形放在 api.ts。
 *
 * 三个容易踩的序列化细节：
 *  1. 所有 id 都是 UUID，JSON 里是 **string**。
 *  2. `Decimal`（面值 face_value、经纬度 lat/lng）在 JSON 模式下被序列化为 **string**，
 *     以免精度丢失。**不要 parseFloat 之后再参与计算。**
 *  3. `date` 是 "YYYY-MM-DD"，`datetime` 是 ISO 8601 字符串。
 */

export type UUID = string;

// --------------------------------------------------------------------------- //
//  词表取值
// --------------------------------------------------------------------------- //

/**
 * 戳种。刻意用 `string` 而不是联合类型：后端明确说明这个枚举会持续扩充
 * （docs/ok/03 §1.3 —— 中国不存在覆盖全部戳种的官方分类标准），
 * 前端跟着后端走，取值一律从 `/api/vocab` 取。
 */
export type PostmarkTypeValue = string;

export type CardSource = 'field_trip' | 'mail_request' | 'exchange' | 'purchase' | 'other';

export type CardStatus = 'collected' | 'pending' | 'wishlist';

export type Provenance = 'real_scan' | 'generated' | 'third_party';

export type CatalogSystem = 'china' | 'scott' | 'michel' | 'other';

export type TagCategory = 'theme' | 'quality' | 'event' | 'source';

export type AssetKind = 'scan_raw' | 'photo' | 'postmark_cut' | 'stamp_cut' | 'thumbnail' | 'derived';

// --------------------------------------------------------------------------- //
//  通用
// --------------------------------------------------------------------------- //

export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

export interface OkResponse {
  ok: boolean;
  message?: string | null;
}

// --------------------------------------------------------------------------- //
//  资产
// --------------------------------------------------------------------------- //

export interface AssetRead {
  id: UUID;
  sha256: string;
  mime: string;
  bytes: number;
  width: number | null;
  height: number | null;
  dpi: number | null;
  kind: AssetKind;
  /** 原始件为 true —— 系统只写不改（docs/ok/01 §2 的 L0 层） */
  is_immutable: boolean;
  original_filename: string | null;
  created_at: string;
}

// --------------------------------------------------------------------------- //
//  行政区划
// --------------------------------------------------------------------------- //

export interface PlaceRead {
  id: UUID;
  parent_id: UUID | null;
  name: string;
  name_en: string | null;
  /** 层级序号，从 0（国家）起。UI 只显示 level_label，不显示"第 N 级"。 */
  level: number;
  /** "国家" | "省级" | "地级市" | "县级" | "乡级" | "村级" */
  level_label: string | null;
  country_code: string | null;
  admin_code: string | null;
  path: string | null;
  /** Decimal → string */
  lat: string | null;
  lng: string | null;
}

export interface PlaceNode extends PlaceRead {
  children: PlaceNode[];
  post_office_count: number;
}

export interface PlaceCreate {
  name: string;
  parent_id?: UUID | null;
  name_en?: string | null;
  level: number;
  level_label?: string | null;
  country_code?: string | null;
  admin_code?: string | null;
  lat?: string | null;
  lng?: string | null;
}

export interface PlaceUpdate {
  name?: string;
  name_en?: string | null;
  level_label?: string | null;
  admin_code?: string | null;
  lat?: string | null;
  lng?: string | null;
}

// --------------------------------------------------------------------------- //
//  邮局
// --------------------------------------------------------------------------- //

export interface PostOfficeRead {
  id: UUID;
  name: string;
  place_id: UUID | null;
  postal_code: string | null;
  is_theme: boolean;
  theme_name: string | null;
  aliases: string[];
  lat: string | null;
  lng: string | null;
  place: PlaceRead | null;
}

export interface PostOfficeCreate {
  name: string;
  place_id?: UUID | null;
  postal_code?: string | null;
  is_theme?: boolean;
  theme_name?: string | null;
  aliases?: string[];
  lat?: string | null;
  lng?: string | null;
}

export interface PostOfficeUpdate {
  name?: string;
  place_id?: UUID | null;
  postal_code?: string | null;
  is_theme?: boolean;
  theme_name?: string | null;
  aliases?: string[];
  lat?: string | null;
  lng?: string | null;
}

// --------------------------------------------------------------------------- //
//  标签
// --------------------------------------------------------------------------- //

export interface TagRead {
  id: UUID;
  name: string;
  category: TagCategory | null;
  color: string | null;
}

export interface TagCreate {
  name: string;
  category?: TagCategory | null;
  color?: string | null;
}

// --------------------------------------------------------------------------- //
//  邮戳
// --------------------------------------------------------------------------- //

export interface PostmarkRead {
  id: UUID;
  card_id: UUID;
  postmark_type: PostmarkTypeValue;
  /** 戳面原文 —— 证据，原样录入。唯一必填的文本字段。 */
  inscription: string;
  postmark_date: string | null;
  /** 日期原文。实测有 3 种变体：2026.09.25.15 / 2026.10.01.09. / 2026.09.29 */
  postmark_date_text: string | null;
  time_code: string | null;
  /** 戳号，位于底弧、跟在局名之后（"建溪 1" 里的 "1"） */
  postmark_no: string | null;
  post_office_id: UUID | null;
  place_id: UUID | null;
  is_color: boolean;
  /** 套色戳的色层数 */
  color_layers: number | null;
  provenance: Provenance;
  is_clean: boolean | null;
  image_asset_id: UUID | null;
  note: string | null;
  tag_ids: UUID[];
  place: PlaceRead | null;
  post_office: PostOfficeRead | null;
  tags: TagRead[];
  created_at: string;
  updated_at: string;
}

export interface PostmarkCreate {
  postmark_type: PostmarkTypeValue;
  inscription: string;
  postmark_date?: string | null;
  postmark_date_text?: string | null;
  time_code?: string | null;
  postmark_no?: string | null;
  post_office_id?: UUID | null;
  place_id?: UUID | null;
  is_color?: boolean;
  color_layers?: number | null;
  provenance?: Provenance;
  is_clean?: boolean | null;
  image_asset_id?: UUID | null;
  note?: string | null;
  tag_ids?: UUID[];
}

export interface PostmarkUpdate {
  postmark_type?: PostmarkTypeValue;
  inscription?: string;
  postmark_date?: string | null;
  postmark_date_text?: string | null;
  time_code?: string | null;
  postmark_no?: string | null;
  post_office_id?: UUID | null;
  place_id?: UUID | null;
  is_color?: boolean;
  color_layers?: number | null;
  provenance?: Provenance;
  is_clean?: boolean | null;
  image_asset_id?: UUID | null;
  note?: string | null;
  tag_ids?: UUID[];
}

// --------------------------------------------------------------------------- //
//  邮票
// --------------------------------------------------------------------------- //

export interface StampIdentifyCandidate {
  c_id: number;
  p_id: number | null;
  name: string | null;
  similarity: number | null;
  url: string | null;
  image: string | null;
  query_sha256: string | null;
}

export interface StampRead {
  id: UUID;
  card_id: UUID;
  /** 关联的标准票（M3C）；null = 还没补录 */
  catalog_id: UUID | null;
  /** M3C-5 识别状态 */
  identify_status: 'idle' | 'queued' | 'running' | 'ok' | 'error';
  identify_error: string | null;
  identify_at: string | null;
  identify_candidates: StampIdentifyCandidate[];
  /** 志号，如 "普30"、"2023-1" */
  catalog_ref: string | null;
  catalog_system: CatalogSystem | null;
  name: string | null;
  /** ★ string（后端 Decimal），不要转 number */
  face_value: string | null;
  /** ISO 4217，如 "CNY" */
  currency: string | null;
  issue_date: string | null;
  image_asset_id: UUID | null;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface StampCreate {
  catalog_ref?: string | null;
  catalog_system?: CatalogSystem | null;
  name?: string | null;
  face_value?: string | null;
  currency?: string | null;
  issue_date?: string | null;
  image_asset_id?: UUID | null;
  note?: string | null;
  /** 关联的标准票（M3C） */
  catalog_id?: UUID | null;
}

export interface StampUpdate {
  catalog_ref?: string | null;
  catalog_system?: CatalogSystem | null;
  name?: string | null;
  face_value?: string | null;
  currency?: string | null;
  issue_date?: string | null;
  image_asset_id?: UUID | null;
  note?: string | null;
  catalog_id?: UUID | null;
}

// --------------------------------------------------------------------------- //
//  标准邮票库（M3C）
// --------------------------------------------------------------------------- //

export interface StampCatalogRead {
  id: UUID;
  standard_code: string;
  colnect_c_id: number | null;
  colnect_url: string | null;
  issue_url: string | null;
  issue_key: string | null;
  catalog_system: CatalogSystem | null;
  catalog_ref: string | null;
  design_no: string | null;
  design_name: string | null;
  issue_name: string | null;
  /** Decimal → 字符串 */
  face_value: string | null;
  currency: string | null;
  issue_date: string | null;
  details: Record<string, unknown>;
  source_state: 'draft' | 'partial' | 'verified' | 'stale';
  /** 抓取任务状态（M3C-3）；与 source_state 分开 */
  fetch_status: 'idle' | 'queued' | 'running' | 'ok' | 'error';
  fetch_error: string | null;
  last_fetched_at: string | null;
  last_verified_at: string | null;
  raw_snapshot_path: string | null;
  raw_sha256: string | null;
  parse_version: string | null;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface CatalogParsedDesign {
  design_no: string | null;
  design_name: string | null;
  /** Decimal → 字符串 */
  face_value: string | null;
  currency: string | null;
}

export interface CatalogLastParse {
  schema_version?: number;
  parse_version: string;
  source_url: string;
  fetched_at?: string;
  final_url?: string;
  snapshot_sha256?: string;
  snapshot_path?: string;
  issue_name: string | null;
  catalog_ref: string | null;
  issue_date: string | null;
  details: Record<string, string>;
  designs: CatalogParsedDesign[];
  field_sources: Record<string, { source: string; confidence: number; fetched_at?: string }>;
}

export interface StampIdentifyApplyResult {
  stamp: StampRead;
  catalog: StampCatalogRead;
  created: boolean;
}

export interface StampCatalogCreate {
  /** 不填由后端生成 sc-<uuid8> */
  standard_code?: string | null;
  colnect_c_id?: number | null;
  colnect_url?: string | null;
  issue_url?: string | null;
  catalog_system?: CatalogSystem | null;
  catalog_ref?: string | null;
  design_no?: string | null;
  design_name?: string | null;
  issue_name?: string | null;
  face_value?: string | null;
  currency?: string | null;
  issue_date?: string | null;
  details?: Record<string, unknown>;
  note?: string | null;
}

export interface StampCatalogUpdate {
  colnect_c_id?: number | null;
  colnect_url?: string | null;
  issue_url?: string | null;
  catalog_system?: CatalogSystem | null;
  catalog_ref?: string | null;
  design_no?: string | null;
  design_name?: string | null;
  issue_name?: string | null;
  face_value?: string | null;
  currency?: string | null;
  issue_date?: string | null;
  details?: Record<string, unknown> | null;
  note?: string | null;
  source_state?: 'draft' | 'partial' | 'verified' | 'stale' | null;
}

export interface UnlinkedStampRead {
  stamp_id: UUID;
  card_id: UUID;
  card_visit_date: string;
  card_title: string | null;
  catalog_ref: string | null;
  name: string | null;
  face_value: string | null;
  currency: string | null;
  issue_date: string | null;
}

// --------------------------------------------------------------------------- //
//  M3C-4：回填 / 合并 / 导出
// --------------------------------------------------------------------------- //

export interface BackfillGroup {
  key: string;
  catalog_system: string | null;
  catalog_ref: string;
  stamp_count: number;
  action: 'link' | 'create' | 'manual';
  catalog_id: UUID | null;
  standard_code: string | null;
  design_name: string | null;
  face_value: string | null;
  currency: string | null;
  issue_date: string | null;
  reason: string | null;
}

export interface BackfillPreview {
  total_stamps: number;
  groups: BackfillGroup[];
}

export interface BackfillApplyResult {
  created: number;
  linked: number;
  skipped: number;
  entries: StampCatalogRead[];
}

export interface DuplicateGroup {
  key: string;
  reason: string;
  entries: StampCatalogRead[];
}

// --------------------------------------------------------------------------- //
//  卡片
// --------------------------------------------------------------------------- //

export interface CardRead {
  id: UUID;
  visit_date: string;
  received_date: string | null;
  source: CardSource;
  status: CardStatus;
  title: string | null;
  note: string | null;
  image_asset_id: UUID | null;
  tag_ids: UUID[];
  postmarks: PostmarkRead[];
  stamps: StampRead[];
  tags: TagRead[];
  created_at: string;
  updated_at: string;
}

export interface CardCreate {
  visit_date: string;
  received_date?: string | null;
  source?: CardSource;
  status?: CardStatus;
  title?: string | null;
  note?: string | null;
  image_asset_id?: UUID | null;
  tag_ids?: UUID[];
  postmarks?: PostmarkCreate[];
  stamps?: StampCreate[];
}

export interface CardUpdate {
  visit_date?: string;
  received_date?: string | null;
  source?: CardSource;
  status?: CardStatus;
  title?: string | null;
  note?: string | null;
  image_asset_id?: UUID | null;
  tag_ids?: UUID[];
}

export interface CardListItem {
  id: UUID;
  visit_date: string;
  received_date: string | null;
  source: CardSource;
  status: CardStatus;
  title: string | null;
  image_asset_id: UUID | null;
  /** 戳面原文，列表直接显示 */
  postmark_summary: string | null;
  /** 票名 · 志号 · 面值 */
  stamp_summary: string | null;
  is_color: boolean;
  postmark_type: PostmarkTypeValue | null;
  /** 从粗到细 */
  place_names: string[];
}

// --------------------------------------------------------------------------- //
//  识别（P3 · D2 的输入）
// --------------------------------------------------------------------------- //

/** 字段决策。**UI 上色的唯一依据** —— 不是 confidence（见 `RecognizedFieldRead`）。 */
export type Decision = 'auto_accept' | 'needs_review' | 'reject';

/**
 * 一个字段的识别结果**加上它的决策**。
 *
 * ⚠️ 上色只能看 `decision`，不能看 `confidence`：实测 OCR 给「福建南火」的
 * 置信度是 **0.90**，真值却是「福建南平」。那个决策是后端
 * `recognition.review.decide` 算出来的，它额外查了词表 —— **前端不要重算**，
 * 否则会与校对队列的判断不一致。
 */
export interface RecognizedFieldRead {
  field_name: string;
  /** 空串表示"机器没读到"。**不要把它当成有效值**。 */
  value: string;
  confidence: number;
  decision: Decision;
  /** 为什么是这个决策（**直接显示给用户**） */
  reason: string;
  pipeline_version: string;
  /**
   * 命中区划时**从树里补出来的层级链**（从粗到细，如
   * `['中华人民共和国','福建省','南平市']`）。
   *
   * ⚠️ 后端**一直在返回它**，但这个类型以前没声明 —— 于是界面白白丢掉了
   * "这个地名属于哪个省"这条信息（`ok/15` q4 就是冲着它来的）。
   * 只有最后一级是识别读到的，上面几级是数据库补的。
   */
  place_path?: string[];
}

/** 校对队列里的一项（后端已按"越不确定越靠前"排好）。 */
export interface ReviewItemRead {
  field_name: string;
  value: string;
  confidence: number;
  why: string;
}

/** 定位到的邮戳外圆。`cx/cy/r` 与 `quad` 一样是**页面坐标**（管线已变换）。 */
export interface RecognizedCircleRead {
  cx: number;
  cy: number;
  r: number;
  /** 半径 / 卡宽。正常邮戳约 0.23；明显更大说明圆抓错了东西。 */
  radius_over_card_width: number;
}

export interface RecognizedCardRead {
  index: number;
  /** [x, y, w, h]，页面坐标 */
  bbox: [number, number, number, number];
  /** 检测到的四边形（页面坐标，4 个点）。卡会倾斜，所以画它而不是 bbox。 */
  quad: [number, number][];
  card_size: [number, number];
  /** 尺寸与期望卡型差很远。**只标注，不丢弃** —— 异形卡是合法的。 */
  size_suspect: boolean;
  postmark_type: string;
  postmark_reason: string;
  circle: RecognizedCircleRead | null;
  /** 全部字段（含被 reject 的） */
  fields: RecognizedFieldRead[];
  queue: ReviewItemRead[];
  /** OCR 原文行（[文本, 置信度]），供校对时对照 */
  ocr_lines: [string, number][];
  /**
   * 这一张的字段**来自哪条通道**：
   *
   * * `local` —— 本地 OCR + 字段抽取
   * * `vlm_json` —— 整卡一次云端 JSON 调用（主路径）
   * * `vlm_fallback` —— 整卡 JSON 与本地都没拿到日期时的**回退**调用
   *
   * ⚠️ 2026-10-11 之前只有 `vlm` 一个值，两条云端通道共用一个标签，
   * 于是诊断只能靠字段置信度（0.90 / 0.75）去猜（`ok/17` §二 A3）。
   * 判"是不是云端"请用 `startsWith('vlm')`。
   */
  field_source: string;
  /**
   * 这一张的**几何**来自哪条通道（`ok/20`）：
   *
   * * `local` —— 本地切分找到的框
   * * `vlm` —— 云端"每张卡的四个角"给的框（A 方案下每页都会走这条）
   *
   * ⚠️ 与 `field_source` 是**两件事**，刻意分开：完全可能"几何本地给的、
   * 字段云端读的"。界面标这个标记的唯一目的是回答"这张要不要多看一眼"，
   * 而那个问题的答案取决于**几何**是谁给的 —— 一张歪的卡图不会报错，
   * 只会让后面所有读数都跟着歪。
   */
  geometry_source: string;
  notes: string[];
  /**
   * 结构化地点/邮局的**自动解析结果**（显示 + 预填，见 `CardLinksRead`）。
   *
   * 后端一直在算（确认时就按同一条规则落库），2026-10-12 才把它**提前告诉界面**。
   */
  links?: CardLinksRead;
}

/**
 * 这张卡**会自动挂到哪个 id**（识别页显示 + 预填；确认时按同一条规则落库）。
 *
 * 用户 2026-10-12："尽管不填【结构化】，只要能在【局名】【地名】命中，
 * 就能补充到结构化字段上面去"。不做这个，人就以为必须手填
 * —— A5 实测里为 7 张卡手填了 14 次结构化选择，大多本来就解析得出来。
 */
export interface CardLinksRead {
  place_id: string | null;
  place_name: string | null;
  office_id: string | null;
  office_name: string | null;
  /** 局所属地点（界面显示成"福屿邮政所（福州市）"） */
  office_place_name: string | null;
  /** 人话说明：挂到哪、或为什么没挂上 */
  note: string;
}

/** 一页（一张扫描图）的识别结果。识别端点**不写库**，这是纯预览。 */
export interface RecognizedPageRead {
  pipeline_version: string;
  work_scale: number;
  /** 工作尺度图像尺寸。**quad / circle 就在这个坐标空间里**，叠加要用它当 viewBox。 */
  work_size: [number, number];
  card_count: number;
  cards: RecognizedCardRead[];
  stage_notes: string[];
  used_vlm: boolean;
  /**
   * 这一页的**几何**最终来自哪条通道：`local` 或 `vlm`。
   *
   * ⚠️ `vlm` = 云端角点**被触发并且真的用上了**，不是"调过云端" ——
   * 调用失败时几何仍然是本地的，那时报 `vlm` 会让人不敢信这一页。
   */
  geometry_source: string;
  /**
   * 当前生效的**几何模式**（后端 `split.GEOMETRY_MODE`，`ok/20` §七 的回滚点）：
   *
   * * `local_first`（B）—— 本地是主路径，只有可疑页才问云端
   * * `cloud_first`（A）—— 每页都问云端，几何以云端为准
   *
   * 回传它是为了让界面/排障**直接看到现在跑的是哪条路** ——
   * 两条路的成本与"该不该信这页几何"完全不同。
   */
  geometry_mode: string;
}

// --------------------------------------------------------------------------- //
//  确认落库（P3 · A6/A7/A8 的落点）
// --------------------------------------------------------------------------- //

/** 每张卡的默认归类。`Card.source` / `Card.status` 是 NOT NULL，且**来自人的选择**。 */
export interface ConfirmDefaults {
  source: CardSource;
  status: CardStatus;
}

/**
 * 一个字段的**机器值**与**最终值**。
 *
 * 两者都给，是因为：机器值 → 写 L2 `observation`；最终值 ≠ 机器值 → 额外写
 * L3 `correction`。所以"人改了什么"是**请求本身**说明的，服务端不去比对新旧行
 * （A7 允许多批 observation 并存，比对会误判）。
 */
export interface ConfirmFieldIn {
  field_name: string;
  /**
   * 机器读到的值（`null` = 机器没读到）。
   *
   * ⚠️ 一旦送出去就会被原样记成"机器说的是什么"，所以**必须用识别响应里的
   * 原值**，不要用界面上改过的值 —— 否则 `correction` 这条长期标注资产就废了。
   */
  machine_value: string | null;
  machine_confidence: number | null;
  machine_decision: string;
  /** 人确认后的值。`null` = 这一项不采纳（与空串不同） */
  final_value: string | null;
  pipeline_version: string;
}

export interface ConfirmCardIn {
  index: number;
  /** 屏蔽（不录入）。被屏蔽的卡**完全不动库** —— 识别端点本来就没写东西。 */
  excluded: boolean;
  /** **跑戳日**。识别给出日期时预填，但它仍是**人的确认值**；为空后端拒绝。 */
  visit_date: string | null;
  fields: ConfirmFieldIn[];
  /**
   * 人**选定**的结构化地点/邮局（`ok/15` q4）。`null` = 交给服务端按文本解析
   * （它与识别判定用**同一条**词表规则，见 `ok/16` §5.3）。
   */
  place_id?: UUID | null;
  post_office_id?: UUID | null;
  /** 要挂到**这枚戳**上的 tag。「主题邮局」由服务端自动判，不用传。 */
  tag_ids?: UUID[];
  /**
   * 这张卡的四边形（**工作尺度**坐标）。服务端据此裁出"切好的那张卡"
   * 并落成派生资产 —— 卡片页于是显示该卡自身，而不是整页扫描件。
   */
  quad?: [number, number][] | null;
  /**
   * 戳圆 **(cx, cy, r)**（**工作尺度**）。服务端据此从整页原图抠出戳贴片
   * （圆外透明）挂到 `Postmark.image_asset_id` —— 画廊叠层要用它的 alpha。
   */
  circle?: [number, number, number] | null;
}

/** 确认提交。**这是唯一会写库的识别相关请求。** */
export interface ConfirmSubmission {
  asset_id: UUID;
  pipeline_version: string;
  defaults: ConfirmDefaults;
  cards: ConfirmCardIn[];
  /** 该资产下已有卡时必须显式 true（挡"双击提交"） */
  confirm_again: boolean;
  /** 识别时的工作尺度：把载荷里的 `quad` 换算回原图要用它。 */
  work_scale?: number | null;
}

export interface ConfirmResult {
  created_cards: number;
  excluded_cards: number;
  /** 写入的 L2 observation 行数 */
  observations: number;
  /** 写入的 L3 correction 行数（**只在人真改了的时候才有**） */
  corrections: number;
  /** 写入的 L3 **`postmark` 行数**（一卡一戳）。 */
  postmarks: number;
  /**
   * 解析结果的人话说明（地点/局名匹配到没、自动打了什么 tag）。
   *
   * ⚠️ 它们**不会**写进 `postmark.note`（那一列的语义是"人写给人看的备注，
   * 机器永不自动填"），所以界面必须把它们显示出来 —— 否则信息就丢了。
   */
  notes: string[];
  card_ids: UUID[];
}

// --------------------------------------------------------------------------- //
//  展览集 / 画廊（v1.1 M3，只读）
// --------------------------------------------------------------------------- //

/**
 * 时间轴的一行 = 一张卡（`GET /api/gallery/timeline`）。
 * 字段与 `apps/api/.../schemas.py` 的 `GalleryTimelineItem` 逐字对应。
 */
export interface GalleryTimelineItem {
  card_id: UUID;
  visit_date: string;
  title: string | null;
  /** 实拍卡图；缺图时前端显示占位，**不隐藏整行** */
  thumb_asset_id: UUID | null;
  /** 从粗到细，如 ["中华人民共和国", "福建省", "南平市"] */
  place_path: string[];
  office_names: string[];
  postmark_count: number;
  /** 戳面原文（证据，原样返回） */
  inscriptions: string[];
  postmark_types: PostmarkTypeValue[];
  tag_names: string[];
  /** 是否已有电子卡（`card.scene` 非空） */
  has_scene: boolean;
  source: CardSource;
}

/**
 * 时间轴分页返回。
 *
 * `without_cut`（有记录没卡片图）与 `without_postmark`（一张戳都没建）是
 * **显式计数**（`docs/spec/m3/00` §3.8）—— 它们对应的记录仍然在 `items` 里，
 * 只是数量必须被看见。
 */
export interface GalleryTimeline {
  total: number;
  with_cut: number;
  without_cut: number;
  without_postmark: number;
  items: GalleryTimelineItem[];
  page: number;
  size: number;
}

export interface GalleryTimelineParams {
  year?: string | null;
  visit_date_from?: string | null;
  visit_date_to?: string | null;
  place_id?: string | null;
  tag_id?: string | null;
  source?: string | null;
  /** `real_scan` = 只看真实跑戳；`all` = 不加 provenance 条件 */
  provenance?: 'real_scan' | 'all';
  page?: number;
  size?: number;
}

/**
 * 戳墙的一块瓦片 = 一枚戳（`GET /api/gallery/postmarks`）。
 *
 * ⚠️ `asset_id=null` = **没有抠图**（技术性），不是"实物缺票"。
 */
export interface GalleryPostmarkItem {
  postmark_id: UUID;
  card_id: UUID;
  /** 戳贴片；null = 这枚戳没有抠图 */
  asset_id: UUID | null;
  inscription: string;
  visit_date: string;
  place_name: string | null;
  office_name: string | null;
  is_color: boolean;
  color_layers: number | null;
  tag_names: string[];
  postmark_type: PostmarkTypeValue;
}

export interface GalleryPostmarkGroup {
  key: string;
  label: string;
  /** 本页里这一组的瓦片数（不是全库计数） */
  count: number;
  items: GalleryPostmarkItem[];
}

export interface GalleryPostmarkWall {
  total: number;
  with_cut: number;
  /** 有戳记录但没有抠图（技术性） */
  without_cut: number;
  groups: GalleryPostmarkGroup[];
  page: number;
  size: number;
}

export type GalleryPostmarkGroupBy = 'type' | 'color' | 'tag' | 'province' | 'none';
export type GalleryPostmarkSort = 'visit_desc' | 'visit_asc';

export interface GalleryPostmarkParams {
  year?: string | null;
  visit_date_from?: string | null;
  visit_date_to?: string | null;
  place_id?: string | null;
  tag_id?: string | null;
  /** 后端查询串里的名字是 `type`（不是 `postmark_type`） */
  type?: string | null;
  is_color?: 'true' | 'false' | null;
  provenance?: 'real_scan' | 'all';
  group_by?: GalleryPostmarkGroupBy;
  sort?: GalleryPostmarkSort;
  page?: number;
  size?: number;
}

/** 票墙的一块瓦片 = 一枚票（`GET /api/gallery/stamps`）。 */
export interface GalleryStampItem {
  stamp_id: UUID;
  card_id: UUID;
  asset_id: UUID | null;
  catalog_ref: string | null;
  catalog_system: string | null;
  name: string | null;
  /** Decimal → 字符串；不要 parseFloat */
  face_value: string | null;
  currency: string | null;
  /** 后端已按币种/数值整理好："40分" / "1.20元" / "面值未知" */
  face_label: string;
  issue_date: string | null;
  visit_date: string;
  tag_names: string[];
  /** 瀑布流预留宽高比，避免图片加载后跳版 */
  asset_width: number | null;
  asset_height: number | null;
}

export interface GalleryStampGroup {
  key: string;
  label: string;
  /** 本页里这一组的瓦片数（不是全库计数） */
  count: number;
  items: GalleryStampItem[];
}

export interface GalleryStampWall {
  total: number;
  with_cut: number;
  /** 有票记录但没有贴片图（技术性） */
  stamps_without_cut: number;
  /** 有戳记录但没有任何票记录（票未录） */
  cards_without_stamp: number;
  /** 最近一张缺票的卡；没有则为 null */
  first_card_without_stamp: UUID | null;
  groups: GalleryStampGroup[];
  page: number;
  size: number;
}

export type GalleryStampGroupBy = 'series' | 'face' | 'year' | 'tag' | 'none';
export type GalleryStampSeries =
  | 'pu'
  | 'ji'
  | 'te'
  | 'yearly'
  | 'other'
  | 'uncatalogued';

export interface GalleryStampParams {
  year?: string | null;
  visit_date_from?: string | null;
  visit_date_to?: string | null;
  place_id?: string | null;
  tag_id?: string | null;
  series?: GalleryStampSeries | null;
  catalog_system?: string | null;
  face_min?: string | null;
  face_max?: string | null;
  provenance?: 'real_scan' | 'all';
  group_by?: GalleryStampGroupBy;
  page?: number;
  size?: number;
}

export type StampSeries = GalleryStampSeries;

/**
 * 省份图的一格 = 一个省级行政区（`GET /api/gallery/map`，M3d 形态 B）。
 *
 * 计数口径**按卡去重**：一张卡多个跨省戳会在每个省各出现一次，
 * 所以 `sum(card_count)` 可能大于 `total_cards`，不要拿两个数相等做断言。
 */
export interface GalleryMapProvince {
  place_id: UUID;
  name: string;
  admin_code: string | null;
  /** SVG 的映射键（`admin_code` 前两位）；null = 画浅灰，不崩 */
  code2: string | null;
  card_count: number;
  /** 只数 level=2 的市级节点；直辖市的区不算市 */
  city_count: number;
  postmark_count: number;
}

/**
 * 地图（形态 B）。**没有坐标** —— 图上没有一个打点。
 *
 * `without_place` 是"没有任何带地点戳的卡"（含一张戳都没建的），
 * 不是"没有坐标"；`points` 恒为 null，是给形态 A 预留的加法口。
 */
export interface GalleryMap {
  total_cards: number;
  without_place: number;
  provinces: GalleryMapProvince[];
  points: null;
}

export interface GalleryMapParams {
  year?: string | null;
  visit_date_from?: string | null;
  visit_date_to?: string | null;
  place_id?: string | null;
  tag_id?: string | null;
  source?: string | null;
  /** `real_scan` = 只看真实跑戳；`all` = 不加 provenance 条件 */
  provenance?: 'real_scan' | 'all';
}

// --------------------------------------------------------------------------- //
//  统计
// --------------------------------------------------------------------------- //

export interface StatsOverview {
  cards: number;
  postmarks: number;
  stamps: number;
  post_offices: number;
  /** { "省级": 12, "县级": 41 } */
  places_by_level: Record<string, number>;
  first_visit_date: string | null;
  last_visit_date: string | null;
}

export interface CountBucket {
  key: string;
  label: string | null;
  count: number;
}

export interface StatsByYear {
  buckets: CountBucket[];
}

export interface StatsByPlace {
  level_label: string | null;
  buckets: CountBucket[];
}

// --------------------------------------------------------------------------- //
//  认证 / 系统
// --------------------------------------------------------------------------- //

export interface MeResponse {
  username: string;
  last_login_at: string | null;
}

export interface LoginRequest {
  username: string;
  password: string;
}

/** `POST /api/auth/password` */
export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface HealthResponse {
  ok: boolean;
  database: boolean;
  assets_writable: boolean;
  assets_dir: string;
  inbox_present: boolean;
  inbox_dir: string;
  inbox_enabled: boolean;
  ocr_engine: string;
  vlm_enabled: boolean;
  agent_configured: boolean;
  default_scan_dpi: number;
  /** 以图搜票是否启用：依赖本仓库之外的服务，默认关。老后端无此字段。 */
  stamp_identify?: { enabled: boolean };
}

// --------------------------------------------------------------------------- //
//  词表
// --------------------------------------------------------------------------- //

export interface VocabOption {
  value: string;
  label: string;
}

export interface FaceValueSuggestion {
  value: string;
  currency: string;
}

export interface VocabResponse {
  postmark_types: VocabOption[];
  /** v0.1 的 UI 只主推这两种（D18 / N4） */
  primary_postmark_types: string[];
  card_sources: VocabOption[];
  card_statuses: VocabOption[];
  catalog_systems: VocabOption[];
  provenances: VocabOption[];
  tag_categories: VocabOption[];
  /** 索引即 level：0=国家、1=省级 … */
  level_labels: string[];
  face_value_suggestions: FaceValueSuggestion[];
}

// --------------------------------------------------------------------------- //
//  电子卡 / 场景图（v1.1 M1，契约见 contract/scene.schema.json）
// --------------------------------------------------------------------------- //

/** 与 JSON Schema 的 `$defs/Fit` 对齐，语义同 CSS object-fit。 */
export type Fit = 'contain' | 'cover' | 'fill' | 'none';
export type SlotKind = 'image' | 'postmark' | 'stamp' | 'text';

export interface SceneCanvas {
  width_mm: number;
  height_mm: number;
  /** 导出目标 DPI。只影响栅格化，不影响布局（布局全部用 mm）。 */
  dpi?: number;
}

export type SceneBackground =
  | { type: 'solid'; color: string }
  | { type: 'image'; asset: string; fit?: Fit; opacity?: number };

export interface SceneSlot {
  id: string;
  kind: SlotKind;
  x: number;
  y: number;
  w: number;
  h: number;
  rotation?: number;
  /** 图层顺序，越大越靠上（"戳压票"就是靠它）。 */
  z?: number;
  opacity?: number;
  fit?: Fit;
  label?: string | null;
  locked?: boolean;
  /** `sha256:<64 hex>`。**可以缺**：模板未实例化、或这张卡没有贴片。 */
  asset?: string;
  /** 仅 `source='generated'` 时用：非日期图层（v1.2 的生成器）。 */
  template_asset?: string;
  source?: 'scan_cut' | 'generated';
  text?: string | null;
  field?: string | null;
  font_size_mm?: number;
  font_family?: string;
  color?: string;
  align?: 'left' | 'center' | 'right';
  valign?: 'top' | 'middle' | 'bottom';
  writing_mode?: 'horizontal' | 'vertical';
}

export interface CardScene {
  schema_version: number;
  /** 内置模板形如 `builtin:blank-card`；用户模板是 UUID。 */
  template_id?: string | null;
  name?: string | null;
  canvas: SceneCanvas;
  background?: SceneBackground;
  slots: SceneSlot[];
}

export interface TemplateRead {
  id: string;
  name: string;
  scope: 'builtin' | 'user';
  canvas: Record<string, unknown>;
  slot_count: number;
}

export interface TemplateDetail extends TemplateRead {
  scene: CardScene;
}

/** `GET /api/cards/{id}/scene`。`scene=null` 表示**还没有电子卡**。 */
export interface CardSceneRead {
  card_id: UUID;
  scene: CardScene | null;
  template_id: UUID | null;
}

export interface CardSceneWrite {
  scene: CardScene;
}

export interface ApplyTemplateIn {
  template_id: string;
  /** 默认预览，不写库。 */
  dry_run?: boolean;
}

export interface ApplyTemplateOut {
  scene: CardScene;
  warnings: string[];
  saved: boolean;
}

export interface StampCutIn {
  /** 归一化 0–1（相对整卡图）：`[x, y, w, h]` */
  box: [number, number, number, number];
  stamp_id?: UUID | null;
}

export interface StampCutOut {
  stamp_id: UUID;
  asset_id: UUID;
  sha256: string;
  width: number;
  height: number;
  /** true = 这次新建了一条 stamp（V1.0 的确认从不建票）。 */
  stamp_created: boolean;
}
