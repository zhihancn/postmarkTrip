import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { useConfirm } from '../../shared/components/Confirm';
import { DateInput } from '../../shared/components/DateInput';
import { EmptyState } from '../../shared/components/EmptyState';
import { Field } from '../../shared/components/Field';
import { Select } from '../../shared/components/Select';
import { Alert, ErrorState, LoadingState, ValidationSummary } from '../../shared/components/States';
import { TagPicker } from '../../shared/components/TagPicker';
import { useToast } from '../../shared/components/Toast';
import { useVocabSafe } from '../../shared/vocab';
import { AssetUploader } from './AssetUploader';
import { ECardPanel } from './ECardPanel';
import { PostmarkEditor } from './PostmarkEditor';
import { StampEditor } from './StampEditor';
import {
  applyTemplate,
  cardToDraft,
  draftToCardCreate,
  draftToCardUpdate,
  emptyCard,
  emptyPostmark,
  emptyStamp,
  postmarkPayload,
  serializeDraft,
  stampPayload,
  validateDraft,
  type CardDraft,
} from './draft';

export function CardDetailPage() {
  const params = useParams<{ id: string }>();
  const cardId = params.id;
  const isNew = cardId === undefined;

  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const confirm = useConfirm();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { vocab } = useVocabSafe();

  const [draft, setDraft] = useState<CardDraft | null>(null);
  const [baseline, setBaseline] = useState('');
  const [originalPostmarkIds, setOriginalPostmarkIds] = useState<string[]>([]);
  const [originalStampIds, setOriginalStampIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [lastError, setLastError] = useState<unknown>(null);
  const [templateDismissed, setTemplateDismissed] = useState(false);

  const loadedKeyRef = useRef<string | null>(null);
  const deepLinkPostmark = searchParams.get('postmark');
  const deepLinkStamp = searchParams.get('stamp');

  // ---------------------------------------------------------------- 加载

  const cardQuery = useQuery({
    queryKey: ['card', cardId],
    queryFn: () => api.cards.get(cardId as string),
    enabled: !isNew,
    retry: false,
  });

  // 新建模式：初始化一张空卡
  useEffect(() => {
    if (!isNew) return;
    if (loadedKeyRef.current === 'new') return;
    loadedKeyRef.current = 'new';
    const next = emptyCard();
    setDraft(next);
    setBaseline(serializeDraft(next));
  }, [isNew]);

  // 编辑模式：把服务端数据灌进草稿，并记下"原始子项 id"，用于保存时算删除集合
  useEffect(() => {
    if (isNew) return;
    const card = cardQuery.data;
    if (!card) return;
    if (loadedKeyRef.current === card.id) return;
    loadedKeyRef.current = card.id;

    const next = cardToDraft(card);
    setDraft(next);
    setBaseline(serializeDraft(next));
    setOriginalPostmarkIds(card.postmarks.map((item) => item.id));
    setOriginalStampIds(card.stamps.map((item) => item.id));
  }, [isNew, cardQuery.data]);

  // 戳墙瓦片深链：/cards/{id}?postmark={postmark_id} → 滚到那一枚戳。
  // 只在卡片加载完成（draft.id 就绪）或目标变化时滚一次；draft 每次输入都会变，
  // 把它整个放进依赖会导致每敲一个字就跳一次。
  useEffect(() => {
    if (!deepLinkPostmark || draft === null) return;
    const timer = window.setTimeout(() => {
      document
        .getElementById(`postmark-${deepLinkPostmark}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [deepLinkPostmark, draft?.id]);

  // 票墙瓦片深链：/cards/{id}?stamp={stamp_id} → 滚到那一枚票。
  useEffect(() => {
    if (!deepLinkStamp || draft === null) return;
    const timer = window.setTimeout(() => {
      document
        .getElementById(`stamp-${deepLinkStamp}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [deepLinkStamp, draft?.id]);

  // 票墙空态的「去框票」：/cards/{id}#ecard（面板在卡片加载后才出现，
  // 浏览器自带的 hash 滚动会扑空，所以草稿就绪后再滚一次）。
  useEffect(() => {
    if (draft === null || window.location.hash !== '#ecard') return;
    const timer = window.setTimeout(() => {
      document.getElementById('ecard')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [draft?.id]);

  const dirty = draft !== null && serializeDraft(draft) !== baseline;

  // ---------------------------------------------------------------- 保存

  const save = useCallback(async () => {
    if (draft === null || saving) return;

    const problem = validateDraft(draft);
    if (problem !== null) {
      toast.error(problem);
      return;
    }

    setSaving(true);
    setLastError(null);

    // ---------------------------------------------------------------------
    // 记住"哪几条已经在服务端建好了"（草稿 key → 服务端 id）。
    //
    // 「1 张卡 + N 个邮戳 + M 枚邮票」是**多次请求，不是原子的**：中途失败是
    // 正常情况（网络抖动、某一条没过校验）。而新建行的草稿里 `id` 一直是 null，
    // 如果不把 create 返回的 id 记回来，用户重试保存时它们会被**再创建一次**
    // —— 一张卡上出现两条一模一样的邮戳，而且用户很可能不会立刻察觉。
    //
    // 下面在 catch 里把这些 id 写回草稿；写回之后重试走的是 PATCH，不会重复。
    // ---------------------------------------------------------------------
    const createdIds = {
      postmarks: new Map<string, string>(),
      stamps: new Map<string, string>(),
    };

    try {
      if (draft.id === null) {
        const created = await api.cards.create(draftToCardCreate(draft));
        toast.success('已创建卡片');
        void queryClient.invalidateQueries({ queryKey: ['cards'] });
        void queryClient.invalidateQueries({ queryKey: ['stats'] });
        navigate(`/cards/${created.id}`, { replace: true });
        return;
      }

      const id = draft.id;
      await api.cards.update(id, draftToCardUpdate(draft));

      // 邮戳：新增 → 更新 → 删除（顺序不能反：先建后删，避免中途状态空掉）
      const keptPostmarkIds = new Set<string>();
      for (const item of draft.postmarks) {
        const payload = postmarkPayload(item);
        if (item.id === null) {
          const created = await api.postmarks.create(id, payload);
          createdIds.postmarks.set(item.key, created.id);
        } else {
          keptPostmarkIds.add(item.id);
          await api.postmarks.update(item.id, payload);
        }
      }
      for (const oldId of originalPostmarkIds) {
        if (!keptPostmarkIds.has(oldId)) await api.postmarks.remove(oldId);
      }

      // 邮票同理
      const keptStampIds = new Set<string>();
      for (const item of draft.stamps) {
        const payload = stampPayload(item);
        if (item.id === null) {
          const created = await api.stamps.create(id, payload);
          createdIds.stamps.set(item.key, created.id);
        } else {
          keptStampIds.add(item.id);
          await api.stamps.update(item.id, payload);
        }
      }
      for (const oldId of originalStampIds) {
        if (!keptStampIds.has(oldId)) await api.stamps.remove(oldId);
      }

      // 重新拉一次，拿到服务端补齐的字段（created_at / place / post_office 等）
      const fresh = await api.cards.get(id);
      const nextDraft = cardToDraft(fresh);
      setDraft(nextDraft);
      setBaseline(serializeDraft(nextDraft));
      setOriginalPostmarkIds(fresh.postmarks.map((item) => item.id));
      setOriginalStampIds(fresh.stamps.map((item) => item.id));
      queryClient.setQueryData(['card', id], fresh);
      void queryClient.invalidateQueries({ queryKey: ['cards'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
      toast.success('已保存');
    } catch (error) {
      // ---------------------------------------------------------------------
      // 把已经建好的那几条的 id 写回草稿 —— 这份 catch 是那个"重试即重复"
      // 缺陷的解药，别把它删了。
      //
      // 不写回的话：这次失败之后草稿里那几条仍然是 `id === null`，用户再点一次
      // 保存就会**再创建一次**，同一张卡上出现两条相同的邮戳 / 两枚相同的邮票。
      // 写回之后重试走的是 PATCH，安全。
      //
      // 刻意**不**更新 baseline：本地还有没保存成功的改动，表单应当保持"未保存"
      // 状态，用户才知道需要再试一次。
      // ---------------------------------------------------------------------
      if (createdIds.postmarks.size > 0 || createdIds.stamps.size > 0) {
        setDraft((prev) =>
          prev === null
            ? prev
            : {
                ...prev,
                postmarks: prev.postmarks.map((item) => {
                  const assigned =
                    item.id === null ? createdIds.postmarks.get(item.key) : undefined;
                  return assigned === undefined ? item : { ...item, id: assigned };
                }),
                stamps: prev.stamps.map((item) => {
                  const assigned = item.id === null ? createdIds.stamps.get(item.key) : undefined;
                  return assigned === undefined ? item : { ...item, id: assigned };
                }),
              },
        );
      }
      setLastError(error);
      toast.error(toApiError(error).message);
    } finally {
      setSaving(false);
    }
  }, [
    draft,
    saving,
    toast,
    queryClient,
    navigate,
    originalPostmarkIds,
    originalStampIds,
  ]);

  // Ctrl/Cmd + S 保存
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void save();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [save]);

  // 有未保存改动时离开页面给个提示
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  // -------------------------------------------------- 复制上次的字段组合

  const firstPostmark = draft?.postmarks[0] ?? null;
  const templateDate = draft?.visit_date ?? '';
  const templateOffice = firstPostmark?.post_office_id ?? '';
  const templateEnabled =
    isNew && !templateDismissed && templateDate !== '' && templateOffice !== '';

  useEffect(() => {
    // 日期或邮局一变，就重新允许提示
    setTemplateDismissed(false);
  }, [templateDate, templateOffice]);

  const templateQuery = useQuery({
    queryKey: ['cards', 'template', templateDate, templateOffice],
    queryFn: () =>
      api.cards.list({
        visit_date_from: templateDate,
        visit_date_to: templateDate,
        post_office_id: templateOffice,
        size: 1,
      }),
    enabled: templateEnabled,
    staleTime: 30_000,
  });

  const templateHit = templateQuery.data?.items?.[0] ?? null;

  const applyTemplateMutation = useMutation({
    mutationFn: (sourceId: string) => api.cards.get(sourceId),
    onSuccess: (source) => {
      setDraft((current) => (current === null ? current : applyTemplate(current, source)));
      setTemplateDismissed(true);
      toast.success('已复制上次的字段组合（只复制来源/状态/标签与戳的环境字段）');
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  // ---------------------------------------------------------------- 删除

  const removeMutation = useMutation({
    mutationFn: (id: string) => api.cards.remove(id),
    onSuccess: () => {
      toast.success('已移入回收站');
      void queryClient.invalidateQueries({ queryKey: ['cards'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
      navigate('/cards');
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  const onDelete = async () => {
    const id = draft?.id ?? null;
    if (id === null) return;
    const ok = await confirm({
      title: '把这张卡片移入回收站？',
      confirmText: '移入回收站',
      danger: true,
      description: (
        <p>
          卡片会从列表与统计中消失，但**可以恢复**，也可以在回收站里彻底删除。
          原始扫描件不会被删除。
        </p>
      ),
    });
    if (ok) removeMutation.mutate(id);
  };

  // ---------------------------------------------------------------- 渲染

  const postmarkCount = draft?.postmarks.length ?? 0;
  const stampCount = draft?.stamps.length ?? 0;

  const headerSubtitle = useMemo(() => {
    if (isNew) return '新建卡片。填好后按 Ctrl+S 保存。';
    if (draft === null) return '';
    return `${postmarkCount} 个邮戳 · ${stampCount} 枚邮票`;
  }, [isNew, draft, postmarkCount, stampCount]);

  if (!isNew && cardQuery.isLoading) {
    return <LoadingState text="正在加载卡片…" />;
  }

  if (!isNew && cardQuery.isError) {
    return (
      <ErrorState
        title="打不开这张卡片"
        error={cardQuery.error}
        onRetry={() => cardQuery.refetch()}
      />
    );
  }

  if (draft === null) {
    return <LoadingState text="正在准备编辑器…" />;
  }

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <h1 className="page__title">
            {isNew ? '新增邮戳卡' : draft.title ?? '邮戳卡'}
          </h1>
          <div className="page__subtitle">
            {headerSubtitle}
            {dirty ? <span className="badge badge--warn" style={{ marginLeft: 8 }}>有未保存改动</span> : null}
          </div>
        </div>

        <div className="page__actions">
          <Link to="/cards" className="btn">
            返回列表
          </Link>
          {!isNew ? (
            <button
              type="button"
              className="btn btn--danger-ghost"
              onClick={() => {
                void onDelete();
              }}
              disabled={removeMutation.isPending}
            >
              移入回收站
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => {
              void save();
            }}
            disabled={saving || (!dirty && !isNew)}
            title="保存（Ctrl+S）"
          >
            {saving ? '保存中…' : isNew ? '创建卡片' : '保存'}
          </button>
        </div>
      </header>

      {lastError !== null ? <ValidationSummary error={lastError} /> : null}

      {templateHit !== null && !templateDismissed ? (
        <Alert
          kind="info"
          title="同一邮局、同一天已经盖过戳"
          action={
            <div className="row" style={{ gap: 6 }}>
              <button
                type="button"
                className="btn btn--sm btn--primary"
                disabled={applyTemplateMutation.isPending}
                onClick={() => applyTemplateMutation.mutate(templateHit.id)}
              >
                复制字段组合
              </button>
              <button type="button" className="btn btn--sm btn--ghost" onClick={() => setTemplateDismissed(true)}>
                不用
              </button>
            </div>
          }
        >
          <div className="small">
            {templateHit.visit_date}
            {templateHit.postmark_summary ? ` · ${templateHit.postmark_summary}` : ''}
            。复制的是**来源、状态、标签**，以及第一个邮戳的**戳种 / 地点 / 邮局 / 彩色 / 来源标记** ——
            戳面原文和日期不会复制（每张卡都不一样）。
          </div>
        </Alert>
      ) : null}

      {/* ------------------------------ 卡片主表单 ------------------------------ */}
      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">卡片</div>
          <div className="small muted">一张卡 = 一次跑戳行为 + 它承载的邮戳与邮票</div>
        </div>

        <div className="panel__body">
          <div className="form-grid form-grid--3">
            <Field label="跑戳日期" required hint="求戳信场景填**寄出日**">
              <DateInput
                quick
                value={draft.visit_date}
                onChange={(value) => setDraft({ ...draft, visit_date: value })}
              />
            </Field>

            <Field label="收回日期" hint="求戳信才有：寄出 → 盖戳 → 收到">
              <DateInput
                value={draft.received_date}
                onChange={(value) => setDraft({ ...draft, received_date: value })}
              />
            </Field>

            <Field label="来源">
              <Select
                value={draft.source}
                onChange={(value) => setDraft({ ...draft, source: value })}
                options={vocab.card_sources}
              />
            </Field>

            <Field label="状态">
              <Select
                value={draft.status}
                onChange={(value) => setDraft({ ...draft, status: value })}
                options={vocab.card_statuses}
              />
            </Field>

            <Field
              label="标题"
              className="span-2"
              hint="留空则列表里只用日期与戳面原文显示"
            >
              <input
                className="input"
                value={draft.title}
                onChange={(event) => setDraft({ ...draft, title: event.target.value })}
                placeholder="如「三清山首日」"
              />
            </Field>
          </div>

          <div style={{ marginTop: 12 }}>
            <Field label="卡片标签">
              <TagPicker
                value={draft.tag_ids}
                onChange={(ids) => setDraft({ ...draft, tag_ids: ids })}
              />
            </Field>
          </div>

          <div style={{ marginTop: 12 }}>
            <Field
              label="卡片原图"
              hint="一张 A4 上可以放多张卡（实测能放 8 张）。扫描时**垫一张黑色卡纸**，切分会稳得多。"
            >
              <AssetUploader
                capturePaste
                assetId={draft.image_asset_id}
                onChange={(assetId) => setDraft({ ...draft, image_asset_id: assetId })}
                title="拖拽扫描件到这里，或点击选择，也可以直接 Ctrl+V 粘贴"
              />
            </Field>
          </div>

          <div style={{ marginTop: 12 }}>
            <Field label="备注">
              <textarea
                className="textarea"
                value={draft.note}
                onChange={(event) => setDraft({ ...draft, note: event.target.value })}
                placeholder="自由备注"
              />
            </Field>
          </div>
        </div>
      </section>

      {/* ------------------------------ 邮戳 ------------------------------ */}
      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">邮戳</div>
          <div className="small muted">
            一张卡上通常只有 1 个戳，但结构上支持多个（1:N）—— 同一张纸上贴满邮票盖满戳也装得下
          </div>
          <div className="right">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setDraft({ ...draft, postmarks: [...draft.postmarks, emptyPostmark()] })}
            >
              ＋ 添加邮戳
            </button>
          </div>
        </div>

        <div className="panel__body">
          {draft.postmarks.length === 0 ? (
            <EmptyState
              icon="🏤"
              title="还没有邮戳记录"
              text="至少需要一条 —— 邮戳是这张卡存在的理由。"
              action={
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => setDraft({ ...draft, postmarks: [emptyPostmark()] })}
                >
                  ＋ 添加邮戳
                </button>
              }
            />
          ) : (
            draft.postmarks.map((item, index) => (
              <PostmarkEditor
                key={item.key}
                draft={item}
                index={index}
                total={draft.postmarks.length}
                domId={item.id ? `postmark-${item.id}` : undefined}
                onChange={(next) => {
                  const list = [...draft.postmarks];
                  list[index] = next;
                  setDraft({ ...draft, postmarks: list });
                }}
                onRemove={() => {
                  setDraft({
                    ...draft,
                    postmarks: draft.postmarks.filter((_, i) => i !== index),
                  });
                }}
              />
            ))
          )}
        </div>
      </section>

      {/* ------------------------------ 邮票 ------------------------------ */}
      <section className="panel">
        <div className="panel__head">
          <div className="panel__title">邮票</div>
          <div className="small muted">
            即需求里的「所用邮票」。留空也可以 —— 有的人只记戳
          </div>
          <div className="right">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setDraft({ ...draft, stamps: [...draft.stamps, emptyStamp()] })}
            >
              ＋ 添加邮票
            </button>
          </div>
        </div>

        <div className="panel__body">
          {draft.stamps.length === 0 ? (
            <div className="small faint" style={{ padding: '4px 2px' }}>
              还没有邮票记录。点右上角「＋ 添加邮票」补一枚。
            </div>
          ) : (
            draft.stamps.map((item, index) => (
              <StampEditor
                key={item.key}
                draft={item}
                index={index}
                domId={item.id ? `stamp-${item.id}` : undefined}
                onChange={(next) => {
                  const list = [...draft.stamps];
                  list[index] = next;
                  setDraft({ ...draft, stamps: list });
                }}
                onRemove={() => {
                  setDraft({ ...draft, stamps: draft.stamps.filter((_, i) => i !== index) });
                }}
              />
            ))
          )}
        </div>
      </section>

      {/* ------------------------------ 电子卡（v1.1 M1） ------------------------------ */}
      {!isNew && cardQuery.data ? (
        <ECardPanel
          card={cardQuery.data}
          onChanged={() => {
            void queryClient.invalidateQueries({ queryKey: ['card', cardId] });
          }}
        />
      ) : null}

      {/* ------------------------------ 底部操作条 ------------------------------ */}
      <div className="panel">
        <div className="panel__body">
          <div className="form-actions" style={{ borderTop: 'none', paddingTop: 0 }}>
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => {
                void save();
              }}
              disabled={saving || (!dirty && !isNew)}
            >
              {saving ? '保存中…' : isNew ? '创建卡片' : '保存全部改动'}
            </button>
            <span className="small muted">
              一次保存会把这些一起提交：卡片字段 · {postmarkCount} 个邮戳 · {stampCount} 枚邮票。
              快捷键 Ctrl+S。
            </span>
            {dirty ? (
              <span className="badge badge--warn right">有未保存改动</span>
            ) : (
              <span className="badge badge--ok right">已同步</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
