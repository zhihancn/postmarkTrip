/**
 * 唯一的 API 客户端。所有网络调用都从这里出入 —— 组件里不要直接 import axios。
 *
 * 两个约定：
 *  1. **baseURL 是相对路径 `/api`**。dev 由 vite 代理到 localhost:9999，
 *     生产由 nginx 反代到 http://api:8000 —— 同一份代码两边都不用改。
 *  2. **认证走 httpOnly cookie**，所以只需要 `withCredentials: true`，
 *     前端不持有、也不该持有任何 token。
 */

import axios, { AxiosError, type AxiosInstance } from 'axios';

import type {
  ApplyTemplateOut,
  AssetRead,
  BackfillApplyResult,
  BackfillPreview,
  CardCreate,
  CardListItem,
  CardRead,
  CardScene,
  CardSceneRead,
  CardUpdate,
  ChangePasswordRequest,
  ConfirmResult,
  ConfirmSubmission,
  DuplicateGroup,
  GalleryMap,
  GalleryMapParams,
  GalleryPostmarkParams,
  GalleryPostmarkWall,
  GalleryStampParams,
  GalleryStampWall,
  GalleryTimeline,
  GalleryTimelineParams,
  HealthResponse,
  LoginRequest,
  MeResponse,
  OkResponse,
  Paged,
  PlaceCreate,
  PlaceNode,
  PlaceRead,
  PlaceUpdate,
  PostOfficeCreate,
  PostOfficeRead,
  PostOfficeUpdate,
  PostmarkCreate,
  PostmarkRead,
  PostmarkUpdate,
  RecognizedPageRead,
  StampCreate,
  StampCatalogCreate,
  StampCatalogRead,
  StampCatalogUpdate,
  StampCutOut,
  StampIdentifyApplyResult,
  StampRead,
  StampUpdate,
  StatsByPlace,
  StatsByYear,
  StatsOverview,
  TagCreate,
  TagRead,
  TemplateDetail,
  TemplateRead,
  UnlinkedStampRead,
  UUID,
  VocabResponse,
} from './types';

// --------------------------------------------------------------------------- //
//  错误
// --------------------------------------------------------------------------- //

export interface ValidationDetail {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface ApiError {
  /** HTTP 状态码；网络不可达时为 0 */
  status: number;
  message: string;
  /** FastAPI 的 422 明细 */
  details?: ValidationDetail[];
  isNetworkError: boolean;
}

export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === 'object' &&
    value !== null &&
    'status' in value &&
    'message' in value &&
    'isNetworkError' in value
  );
}

/** 把 axios / 任意异常统一成 ApiError，组件里只需处理一种形状。 */
export function toApiError(err: unknown): ApiError {
  if (isApiError(err)) return err;

  if (err instanceof AxiosError) {
    const response = err.response;
    if (!response) {
      const timedOut = err.code === 'ECONNABORTED';
      return {
        status: 0,
        message: timedOut
          ? '请求超时。若正在上传大图，请稍后重试或检查服务是否卡住。'
          : '无法连接到服务，请确认后端已启动。',
        isNetworkError: true,
      };
    }

    const data: unknown = response.data;
    let message = `请求失败（HTTP ${response.status}）`;
    let details: ValidationDetail[] | undefined;

    if (typeof data === 'string' && data.trim() !== '') {
      message = data;
    } else if (typeof data === 'object' && data !== null && 'detail' in data) {
      const detail: unknown = (data as { detail: unknown }).detail;
      if (typeof detail === 'string') {
        message = detail;
      } else if (Array.isArray(detail)) {
        details = detail as ValidationDetail[];
        const parts = details
          .map((d) => {
            const field = d.loc.filter((p) => p !== 'body').join('.');
            return field ? `${field}: ${d.msg}` : d.msg;
          })
          .filter(Boolean);
        if (parts.length > 0) message = parts.join('；');
      }
    }

    return { status: response.status, message, details, isNetworkError: false };
  }

  if (err instanceof Error) {
    return { status: 0, message: err.message, isNetworkError: false };
  }
  return { status: 0, message: '发生未知错误', isNetworkError: false };
}

/** 401 是"未登录"，调用方据此跳登录页 —— 不该当成普通错误弹提示。 */
export function isUnauthorized(err: unknown): boolean {
  return isApiError(err) && err.status === 401;
}

// --------------------------------------------------------------------------- //
//  实例
// --------------------------------------------------------------------------- //

export const http: AxiosInstance = axios.create({
  baseURL: '/api',
  withCredentials: true,
  // 默认 120 秒（nginx 的 proxy_read_timeout 是 300 秒）。需要更久的逐个覆盖：
  // 上传 300 秒、识别 300 秒 —— 客户端不该比服务端先放弃。
  timeout: 120_000,
  headers: { Accept: 'application/json' },
});

// --------------------------------------------------------------------------- //
//  查询参数
// --------------------------------------------------------------------------- //

export type ParamValue = string | number | boolean | null | undefined;

/**
 * 丢掉 null / undefined / 空字符串。
 * axios 默认只丢弃 undefined，`status=` 这种空串会被后端当成"筛选空值"。
 */
export function cleanParams(input: Record<string, ParamValue>): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  for (const [key, value] of Object.entries(input)) {
    if (value === null || value === undefined) continue;
    if (typeof value === 'string' && value.trim() === '') continue;
    out[key] = value;
  }
  return out;
}

export function buildQueryString(input: Record<string, ParamValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(cleanParams(input))) {
    search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : '';
}

// --------------------------------------------------------------------------- //
//  参数类型
// --------------------------------------------------------------------------- //

export interface CardListParams {
  q?: string | null;
  visit_date_from?: string | null;
  visit_date_to?: string | null;
  place_id?: string | null;
  place_descendants?: boolean;
  post_office_id?: string | null;
  postmark_type?: string | null;
  source?: string | null;
  /** 后端查询串里的名字是 status（不是 status_） */
  status?: string | null;
  is_color?: boolean | null;
  provenance?: string | null;
  tag?: string | null;
  include_deleted?: boolean;
  order_by?: 'visit_date' | 'created_at' | 'title';
  order_desc?: boolean;
  page?: number;
  size?: number;
}

export interface PostOfficeListParams {
  q?: string | null;
  place_id?: string | null;
  place_descendants?: boolean;
  is_theme?: boolean | null;
  page?: number;
  size?: number;
}

export interface PlaceListParams {
  parent_id?: UUID | null;
  country_code?: string | null;
  level?: number | null;
  q?: string | null;
  limit?: number;
}

export interface StampCatalogListParams {
  q?: string | null;
  source_state?: StampCatalogRead['source_state'] | null;
  page?: number;
  size?: number;
}

/**
 * `POST /api/recognize` 的查询参数。
 *
 * 入口二选一（**首选 `asset_id`**）：前者让原始件有档案（L0 证据、内容寻址去重），
 * 后者只用于开发排障 —— 本项目已经因为"两份等价逻辑"栽过（`ok/06` 缺陷 #9/#10）。
 */
export interface RecognizeParams {
  /** 首选。与 `file` 二选一。 */
  assetId?: UUID | null;
  /** 开发/排障用。给了 `assetId` 就不要再给这个。 */
  file?: File | null;
  /** 不给就由切分器反推；给了就作为物理量的基准（原图尺度，见 `ok/09` E-14） */
  dpi?: number | null;
  /** 工作尺度长边上限 */
  longSide?: number;
  /**
   * 是否用云端识别。**默认关**：云端花钱且结果不可复现，必须显式打开；
   * 服务端 `vlm_enabled` 也打开才真正生效（响应里回 `used_vlm` 说明到底用了没）。
   */
  useVlm?: boolean;
}

// --------------------------------------------------------------------------- //
//  端点
// --------------------------------------------------------------------------- //

export const api = {
  // ---------------- 认证 ----------------
  auth: {
    async login(payload: LoginRequest): Promise<MeResponse> {
      const { data } = await http.post<MeResponse>('/auth/login', payload);
      return data;
    },
    async logout(): Promise<OkResponse> {
      const { data } = await http.post<OkResponse>('/auth/logout');
      return data;
    },
    async me(): Promise<MeResponse> {
      const { data } = await http.get<MeResponse>('/auth/me');
      return data;
    },
    /**
     * 改口令。载荷是 `{ current_password, new_password }`，与后端的
     * `PasswordChangeRequest` 一致（`apps/api/tests/contract` 会核对字段名）。
     *
     * ⚠️ 这里曾经有一段注释写着"username = 新口令，password = 旧口令" —— 那是
     *    后端修掉"复用 LoginRequest 导致字段语义错位"之前的说法。注释没跟着改，
     *    照着它写就会传错载荷。留此说明以免又被写回去。
     */
    async changePassword(payload: ChangePasswordRequest): Promise<OkResponse> {
      const { data } = await http.post<OkResponse>('/auth/password', payload);
      return data;
    },
  },

  // ---------------- 系统 ----------------
  async health(): Promise<HealthResponse> {
    const { data } = await http.get<HealthResponse>('/health');
    return data;
  },

  async vocab(): Promise<VocabResponse> {
    const { data } = await http.get<VocabResponse>('/vocab');
    return data;
  },

  // ---------------- 标签 ----------------
  tags: {
    async list(category?: string | null): Promise<TagRead[]> {
      const { data } = await http.get<TagRead[]>('/tags', {
        params: cleanParams({ category }),
      });
      return data;
    },
    async create(payload: TagCreate): Promise<TagRead> {
      const { data } = await http.post<TagRead>('/tags', payload);
      return data;
    },
    async remove(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/tags/${id}`);
      return data;
    },
  },

  // ---------------- 行政区划 ----------------
  places: {
    async list(p: PlaceListParams = {}): Promise<PlaceRead[]> {
      const { data } = await http.get<PlaceRead[]>('/places', {
        params: cleanParams({
          parent_id: p.parent_id,
          country_code: p.country_code,
          level: p.level,
          q: p.q,
          limit: p.limit,
        }),
      });
      return data;
    },
    async tree(rootId?: UUID | null, depth = 2, countryCode?: string | null): Promise<PlaceNode[]> {
      const { data } = await http.get<PlaceNode[]>('/places/tree', {
        params: cleanParams({ root_id: rootId, depth, country_code: countryCode }),
      });
      return data;
    },
    async create(payload: PlaceCreate): Promise<PlaceRead> {
      const { data } = await http.post<PlaceRead>('/places', payload);
      return data;
    },
    async update(id: UUID, payload: PlaceUpdate): Promise<PlaceRead> {
      const { data } = await http.patch<PlaceRead>(`/places/${id}`, payload);
      return data;
    },
    async remove(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/places/${id}`);
      return data;
    },
  },

  // ---------------- 邮局 ----------------
  postOffices: {
    async list(p: PostOfficeListParams = {}): Promise<Paged<PostOfficeRead>> {
      const { data } = await http.get<Paged<PostOfficeRead>>('/post-offices', {
        params: cleanParams({
          q: p.q,
          place_id: p.place_id,
          place_descendants: p.place_descendants,
          is_theme: p.is_theme,
          page: p.page,
          size: p.size,
        }),
      });
      return data;
    },
    /** 按 id 取单个邮局。用于"只有 id、没有对象"时反查名称。 */
    async get(id: UUID): Promise<PostOfficeRead> {
      const { data } = await http.get<PostOfficeRead>(`/post-offices/${id}`);
      return data;
    },
    async create(payload: PostOfficeCreate): Promise<PostOfficeRead> {
      const { data } = await http.post<PostOfficeRead>('/post-offices', payload);
      return data;
    },
    async update(id: UUID, payload: PostOfficeUpdate): Promise<PostOfficeRead> {
      const { data } = await http.patch<PostOfficeRead>(`/post-offices/${id}`, payload);
      return data;
    },
    async remove(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/post-offices/${id}`);
      return data;
    },
  },

  // ---------------- 卡片 ----------------
  cards: {
    async list(p: CardListParams = {}): Promise<Paged<CardListItem>> {
      const { data } = await http.get<Paged<CardListItem>>('/cards', {
        params: cleanParams({
          q: p.q,
          visit_date_from: p.visit_date_from,
          visit_date_to: p.visit_date_to,
          place_id: p.place_id,
          place_descendants: p.place_descendants,
          post_office_id: p.post_office_id,
          postmark_type: p.postmark_type,
          source: p.source,
          status: p.status,
          is_color: p.is_color,
          provenance: p.provenance,
          tag: p.tag,
          include_deleted: p.include_deleted,
          order_by: p.order_by,
          order_desc: p.order_desc,
          page: p.page,
          size: p.size,
        }),
      });
      return data;
    },
    async get(id: UUID): Promise<CardRead> {
      const { data } = await http.get<CardRead>(`/cards/${id}`);
      return data;
    },
    async create(payload: CardCreate): Promise<CardRead> {
      const { data } = await http.post<CardRead>('/cards', payload);
      return data;
    },
    async update(id: UUID, payload: CardUpdate): Promise<CardRead> {
      const { data } = await http.patch<CardRead>(`/cards/${id}`, payload);
      return data;
    },
    /** 软删除 → 回收站 */
    async remove(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/cards/${id}`);
      return data;
    },
    async restore(id: UUID): Promise<CardRead> {
      const { data } = await http.post<CardRead>(`/cards/${id}/restore`);
      return data;
    },
    /** 彻底删除。后端刻意不删除任何资产（L0 证据只进不出）。 */
    async purge(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/cards/${id}/purge`);
      return data;
    },
  },

  // ---------------- 邮戳 ----------------
  postmarks: {
    async create(cardId: UUID, payload: PostmarkCreate): Promise<PostmarkRead> {
      const { data } = await http.post<PostmarkRead>(`/cards/${cardId}/postmarks`, payload);
      return data;
    },
    async update(id: UUID, payload: PostmarkUpdate): Promise<PostmarkRead> {
      const { data } = await http.patch<PostmarkRead>(`/postmarks/${id}`, payload);
      return data;
    },
    async remove(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/postmarks/${id}`);
      return data;
    },
  },

  // ---------------- 邮票 ----------------
  stamps: {
    async get(id: UUID): Promise<StampRead> {
      const { data } = await http.get<StampRead>(`/stamps/${id}`);
      return data;
    },
    async identify(id: UUID, force = false): Promise<StampRead> {
      const { data } = await http.post<StampRead>(`/stamps/${id}/identify`, { force });
      return data;
    },
    async applyIdentify(
      id: UUID,
      cId: number,
      replaceExisting = false,
    ): Promise<StampIdentifyApplyResult> {
      const { data } = await http.post<StampIdentifyApplyResult>(
        `/stamps/${id}/identify/apply`,
        { c_id: cId, replace_existing: replaceExisting },
      );
      return data;
    },
    async create(cardId: UUID, payload: StampCreate): Promise<StampRead> {
      const { data } = await http.post<StampRead>(`/cards/${cardId}/stamps`, payload);
      return data;
    },
    async update(id: UUID, payload: StampUpdate): Promise<StampRead> {
      const { data } = await http.patch<StampRead>(`/stamps/${id}`, payload);
      return data;
    },
    async remove(id: UUID): Promise<OkResponse> {
      const { data } = await http.delete<OkResponse>(`/stamps/${id}`);
      return data;
    },
  },

  // ---------------- 标准邮票库（M3C） ----------------
  stampCatalog: {
    async list(p: StampCatalogListParams = {}): Promise<Paged<StampCatalogRead>> {
      const { data } = await http.get<Paged<StampCatalogRead>>('/stamp-catalog', {
        params: cleanParams({
          q: p.q,
          source_state: p.source_state,
          page: p.page,
          size: p.size,
        }),
      });
      return data;
    },
    async get(id: UUID): Promise<StampCatalogRead> {
      const { data } = await http.get<StampCatalogRead>(`/stamp-catalog/${id}`);
      return data;
    },
    async create(payload: StampCatalogCreate): Promise<StampCatalogRead> {
      const { data } = await http.post<StampCatalogRead>('/stamp-catalog', payload);
      return data;
    },
    async update(id: UUID, payload: StampCatalogUpdate): Promise<StampCatalogRead> {
      const { data } = await http.patch<StampCatalogRead>(`/stamp-catalog/${id}`, payload);
      return data;
    },
    /** 抓取官方发行页（后台）+ 解析；返回 queued 状态。 */
    async refresh(id: UUID): Promise<StampCatalogRead> {
      const { data } = await http.post<StampCatalogRead>(`/stamp-catalog/${id}/refresh`);
      return data;
    },
    /** 人工复核后应用解析结果；一页多枚传 designNo。 */
    async applyParse(id: UUID, designNo?: string | null): Promise<StampCatalogRead> {
      const { data } = await http.post<StampCatalogRead>(`/stamp-catalog/${id}/apply-parse`, {
        design_no: designNo ?? null,
      });
      return data;
    },
    /** 有票记录、但还没关联标准票的用户票（票墙「未编目」补录队列）。 */
    async unlinkedStamps(
      p: { page?: number; size?: number } = {},
    ): Promise<Paged<UnlinkedStampRead>> {
      const { data } = await http.get<Paged<UnlinkedStampRead>>(
        '/stamp-catalog/unlinked-stamps',
        { params: cleanParams({ page: p.page, size: p.size }) },
      );
      return data;
    },
    /** M3C-4：批量回填预览。 */
    async backfillPreview(): Promise<BackfillPreview> {
      const { data } = await http.get<BackfillPreview>('/stamp-catalog/backfill/preview');
      return data;
    },
    /** M3C-4：应用当前预览（匹配→关联；无候选→建 draft；多候选→跳过）。 */
    async backfillApply(): Promise<BackfillApplyResult> {
      const { data } = await http.post<BackfillApplyResult>('/stamp-catalog/backfill/apply');
      return data;
    },
    /** M3C-4：重复标准票分组。 */
    async duplicates(): Promise<DuplicateGroup[]> {
      const { data } = await http.get<DuplicateGroup[]>('/stamp-catalog/duplicates');
      return data;
    },
    /** M3C-4：把 source 合并到 target（用户票重指 + 删除 source）。 */
    async merge(targetId: UUID, sourceIds: UUID[]): Promise<StampCatalogRead> {
      const { data } = await http.post<StampCatalogRead>('/stamp-catalog/merge', {
        target_id: targetId,
        source_ids: sourceIds,
      });
      return data;
    },
  },

  // ---------------- 资产 ----------------
  assets: {
    /**
     * 上传一个文件。字段名必须是 `file`（后端用 `UploadFile = File(...)`）。
     *
     * **超时刻意放宽到 300 秒**（其余请求走全局的 120 秒）。理由：nginx 的
     * `client_body_timeout` 是 300 秒，`.env` 允许 100 MiB 的上传 —— 客户端
     * 不该比服务端先放弃。若沿用 120 秒，慢速链路上传一个大 PDF 时会看到
     * "请求超时"，而服务端其实还在正常接收，用户只能盲目重试。
     */
    async upload(file: File, onProgress?: (percent: number) => void): Promise<AssetRead> {
      const form = new FormData();
      form.append('file', file);
      const { data } = await http.post<AssetRead>('/assets', form, {
        timeout: 300_000,
        onUploadProgress: (event) => {
          if (!onProgress) return;
          const total = event.total ?? 0;
          if (total > 0) onProgress(Math.round((event.loaded / total) * 100));
        },
      });
      return data;
    },
  },

  // ---------------- 识别（P3 · D2） ----------------
  recognition: {
    /**
     * 识别一张扫描图（或单卡图），返回**待确认**的结果。**不写库。**
     *
     * 这是 spec A6 的**结构性**保证：本端点只读（后端有测试断言它没有任何
     * 写操作），落库只发生在 `confirm()` 里。所以"没经人工确认就落库"这件事
     * 在这一层是不可能发生的，而不是靠人记得别写。
     *
     * 两条入口共用一个端点：给 `assetId`（首选，原始件留档）或 `file`
     * （只用于开发排障）。**不要两个都给** —— 后端优先用 `asset_id`。
     *
     * ⚠️ `assetId` 走的是**查询串**，而 `file` 走 multipart。后端那侧
     * `file` 是可选参数，所以只给 `assetId` 时也必须发一个**空的 FormData**：
     * axios 不带请求体时不会设置 multipart 的 Content-Type，
     * FastAPI 会按 JSON 去解析而报 422（"必须给 asset_id 或 file"）。
     */
    async recognize(p: RecognizeParams): Promise<RecognizedPageRead> {
      const form = new FormData();
      if (p.file) form.append('file', p.file);

      const { data } = await http.post<RecognizedPageRead>(
        `/recognize${buildQueryString({
          asset_id: p.assetId,
          dpi: p.dpi,
          long_side: p.longSide,
          // 只发 true：false 是服务端默认值，留在查询串里只是噪声
          use_vlm: p.useVlm ? true : undefined,
        })}`,
        form,
        // 识别比普通请求慢（切分 + OCR + 可能的云端往返）。云端超时另有
        // `vlm_timeout_s`，这里是客户端愿意等多久。
        { timeout: 300_000 },
      );
      return data;
    },

    /**
     * 把**人工确认过**的结果落库。**这是唯一的写入点。**
     *
     * `confirm_again=true` 用于"该资产下已经有卡"的情形（后端默认 409 挡双击提交）。
     */
    async confirm(payload: ConfirmSubmission): Promise<ConfirmResult> {
      const { data } = await http.post<ConfirmResult>('/cards/confirm', payload);
      return data;
    },
  },

  // ---------------- 电子卡（v1.1 M1） ----------------
  ecard: {
    /** 内置模板来自 `public-data/templates/*.json`，用户模板来自 `template` 表。 */
    async templates(): Promise<TemplateRead[]> {
      const { data } = await http.get<TemplateRead[]>('/templates');
      return data;
    },
    async template(templateId: string): Promise<TemplateDetail> {
      const { data } = await http.get<TemplateDetail>(
        `/templates/${encodeURIComponent(templateId)}`,
      );
      return data;
    },
    /** 没有电子卡时返回 `scene: null` —— 与"空场景"是两件事。 */
    async getScene(cardId: UUID): Promise<CardSceneRead> {
      const { data } = await http.get<CardSceneRead>(`/cards/${cardId}/scene`);
      return data;
    },
    async saveScene(cardId: UUID, scene: CardScene): Promise<CardSceneRead> {
      const { data } = await http.put<CardSceneRead>(`/cards/${cardId}/scene`, { scene });
      return data;
    },
    /**
     * 套模板。**默认 dry_run=true：只返回预览场景，不写库。**
     * 只有用户点「保存」才用 `dryRun=false` 落库。
     */
    async applyTemplate(
      cardId: UUID,
      templateId: string,
      dryRun = true,
    ): Promise<ApplyTemplateOut> {
      const { data } = await http.post<ApplyTemplateOut>(`/cards/${cardId}/apply-template`, {
        template_id: templateId,
        dry_run: dryRun,
      });
      return data;
    },
    /** 在整卡图上人工框一次邮票 → `stamp_cut` +（必要时）一条 `stamp`。 */
    async createStampCut(
      cardId: UUID,
      box: [number, number, number, number],
      stampId?: UUID | null,
    ): Promise<StampCutOut> {
      const { data } = await http.post<StampCutOut>(`/cards/${cardId}/stamp-cut`, {
        box,
        stamp_id: stampId ?? null,
      });
      return data;
    },
  },

  // ---------------- 展览集 / 画廊（v1.1 M3） ----------------
  gallery: {
    /**
     * 时间轴（卡片级聚合）。
     *
     * `provenance` 由调用方明确给出：`real_scan` 是 UI 的默认（只看真实跑戳），
     * `all` 表示不加 provenance 条件。**不要省略** —— 后端的默认值也是
     * `real_scan`，省略会让"关掉开关"静默失效。
     */
    async timeline(p: GalleryTimelineParams = {}): Promise<GalleryTimeline> {
      const { data } = await http.get<GalleryTimeline>('/gallery/timeline', {
        params: cleanParams({
          year: p.year,
          visit_date_from: p.visit_date_from,
          visit_date_to: p.visit_date_to,
          place_id: p.place_id,
          tag_id: p.tag_id,
          source: p.source,
          provenance: p.provenance,
          page: p.page,
          size: p.size,
        }),
      });
      return data;
    },

    /**
     * 戳墙（一页 60 枚，服务端按 `group_by` 分组）。
     *
     * `provenance` 永远显式传（`real_scan` / `all`）—— 与时间轴同一条理由：
     * 后端默认值会让"关掉开关"静默失效。
     */
    async postmarks(p: GalleryPostmarkParams = {}): Promise<GalleryPostmarkWall> {
      const { data } = await http.get<GalleryPostmarkWall>('/gallery/postmarks', {
        params: cleanParams({
          year: p.year,
          visit_date_from: p.visit_date_from,
          visit_date_to: p.visit_date_to,
          place_id: p.place_id,
          tag_id: p.tag_id,
          type: p.type,
          is_color: p.is_color,
          provenance: p.provenance,
          group_by: p.group_by,
          sort: p.sort,
          page: p.page,
          size: p.size,
        }),
      });
      return data;
    },

    /** 票墙（一页 60 枚；服务端按 `group_by` 分组，并给出"票未录"计数）。 */
    async stamps(p: GalleryStampParams = {}): Promise<GalleryStampWall> {
      const { data } = await http.get<GalleryStampWall>('/gallery/stamps', {
        params: cleanParams({
          year: p.year,
          visit_date_from: p.visit_date_from,
          visit_date_to: p.visit_date_to,
          place_id: p.place_id,
          tag_id: p.tag_id,
          series: p.series,
          catalog_system: p.catalog_system,
          face_min: p.face_min,
          face_max: p.face_max,
          provenance: p.provenance,
          group_by: p.group_by,
          page: p.page,
          size: p.size,
        }),
      });
      return data;
    },

    /**
     * 地图（M3d，形态 B：省级清单 + 静态省份图）。
     *
     * 与时间轴**同一组筛选条件**；响应里的 `points` 恒为 null（现在没有坐标）。
     * `provenance` 永远显式传 —— 与时间轴/戳墙同一条理由。
     */
    async map(p: GalleryMapParams = {}): Promise<GalleryMap> {
      const { data } = await http.get<GalleryMap>('/gallery/map', {
        params: cleanParams({
          year: p.year,
          visit_date_from: p.visit_date_from,
          visit_date_to: p.visit_date_to,
          place_id: p.place_id,
          tag_id: p.tag_id,
          source: p.source,
          provenance: p.provenance,
        }),
      });
      return data;
    },
  },

  // ---------------- 统计 ----------------
  stats: {
    async overview(): Promise<StatsOverview> {
      const { data } = await http.get<StatsOverview>('/stats/overview');
      return data;
    },
    async byYear(): Promise<StatsByYear> {
      const { data } = await http.get<StatsByYear>('/stats/by-year');
      return data;
    },
    async byPlace(level: number): Promise<StatsByPlace> {
      const { data } = await http.get<StatsByPlace>('/stats/by-place', {
        params: cleanParams({ level }),
      });
      return data;
    },
  },
};

// --------------------------------------------------------------------------- //
//  资源 URL
// --------------------------------------------------------------------------- //

/**
 * 这两个是 **同源图片地址**，不是 XHR。浏览器会自动带上 httpOnly cookie，
 * 所以直接丢给 <img src> 即可，不需要在 JS 里 fetch 再转 blob。
 */
export function assetRawUrl(assetId: UUID): string {
  return `/api/assets/${assetId}/raw`;
}

export function assetThumbUrl(assetId: UUID, maxPx?: number, alpha = false): string {
  // 画廊按 `00` §3.5 固定用 320；其余页面沿用服务端默认（`thumbnail_max_px`）。
  const params = new URLSearchParams();
  if (maxPx !== undefined && maxPx > 0) params.set('max_px', String(maxPx));
  // 戳贴片必须保留圆外透明；`alpha=1` 让服务端出 PNG 而不是 JPEG。
  if (alpha) params.set('alpha', '1');
  const query = params.toString();
  return `/api/assets/${assetId}/thumb${query === '' ? '' : `?${query}`}`;
}

/**
 * 页面预览图：把整张图缩到 `maxPx`（长边），**保持原比例**。
 *
 * 识别返回的 `quad` / `circle` 都在**工作尺度**的坐标里，而工作尺度就是
 * 原图按比例缩放 —— 所以一张等比缩放的预览图可以直接当那个坐标空间的
 * 画布：叠层用 `viewBox="0 0 workW workH"` + `preserveAspectRatio="none"`，
 * 就会严丝合缝地贴在这张图上，**与预览图实际有多少像素无关**。
 *
 * ⚠️ 因此**不能**用裁剪或改比例的图。`get_thumb` 走的是
 * `Image.thumbnail()`（等比），所以这里可以放心用；但它上限 1600px，
 * 超过就会返回**比请求小**的图 —— 对叠层没有影响，对"想放大看细节"有影响。
 */
export function assetPreviewUrl(assetId: UUID, maxPx = 1600): string {
  return `/api/assets/${assetId}/thumb?max_px=${maxPx}`;
}

/**
 * **这一张卡**的图：服务端按识别给出的四边形做**透视校正**后裁出来（`ok/19` §三 3.2）。
 *
 * 为什么不让前端用 CSS 裁：卡是**倾斜**扫进来的，直角外接框裁必然把深色垫底
 * 带进画面（用户 2026-10-12 报的"点【整卡】就有黑边"）。真校正需要投影变换，
 * 而 canvas 的渲染结果**没法用测试看** —— 所以几何交给服务端（与确认落库
 * 用的是同一个 `warp_card`），出图可直接查。
 *
 * `quad` 是**工作尺度**的 4 个点（识别响应里就有），配 `workScale` 换算回原图。
 */
export function assetCardCropUrl(
  assetId: UUID,
  quad: readonly (readonly [number, number])[],
  workScale: number,
  maxPx = 1600,
): string {
  const flat = quad.map(([x, y]) => `${x},${y}`).join(',');
  const params = new URLSearchParams({
    quad: flat,
    work_scale: String(workScale),
    max_px: String(maxPx),
  });
  return `/api/assets/${assetId}/card-crop?${params.toString()}`;
}

/** CSV 导出：同源跳转即可下载，无需处理 blob。 */
export function exportCardsCsvUrl(p: CardListParams = {}): string {
  return `/api/export/cards.csv${buildQueryString({ ...p, page: undefined, size: undefined })}`;
}
