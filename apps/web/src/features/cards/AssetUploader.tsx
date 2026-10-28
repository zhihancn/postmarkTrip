import { useEffect, useRef, useState, type DragEvent } from 'react';

import { api, toApiError } from '../../shared/api';
import { Thumb } from '../../shared/components/Thumb';
import { Alert } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import { formatBytes } from '../../shared/format';
import type { AssetRead } from '../../shared/types';

interface AssetUploaderProps {
  assetId: string;
  onChange: (assetId: string) => void;
  title?: string;
  hint?: string;
  /**
   * 是否抢占全局 Ctrl+V。
   * 一张卡上可能有多个上传位（卡片原图 / 戳特写 / 票图），
   * 只让最主要的那一个抢，否则一次粘贴会同时进好几个。
   */
  capturePaste?: boolean;
  compact?: boolean;
}

const ACCEPT = 'image/*,.pdf,.tif,.tiff';

/**
 * 图片上传：拖拽 + 点击 + 粘贴。
 *
 * 注意"清除"只是**解除引用**，不会删除资产 —— 原始件是 L0 层证据，只进不出
 * （docs/ok/01 §2，后端也刻意不提供删除资产的接口）。
 */
export function AssetUploader({
  assetId,
  onChange,
  title = '卡片原图',
  hint = '拖拽扫描件到这里，或点击选择，也可以直接 Ctrl+V 粘贴',
  capturePaste = false,
  compact = false,
}: AssetUploaderProps) {
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<AssetRead | null>(null);

  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    setProgress(0);
    try {
      const asset = await api.assets.upload(file, setProgress);
      setMeta(asset);
      onChange(asset.id);
      toast.success(`已上传 ${asset.original_filename ?? file.name}`);
    } catch (uploadError) {
      const apiError = toApiError(uploadError);
      setError(apiError.message);
      toast.error(apiError.message);
    } finally {
      setBusy(false);
      setProgress(0);
    }
  };

  const takeFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const first = files[0];
    if (first) void upload(first);
  };

  // 全局粘贴：只在明确要求、且当前还没有图时生效
  useEffect(() => {
    if (!capturePaste || assetId !== '') return;

    const onPaste = (event: ClipboardEvent) => {
      const items = event.clipboardData?.items;
      if (!items) return;
      for (const item of items) {
        if (item.kind === 'file') {
          const file = item.getAsFile();
          if (file) {
            event.preventDefault();
            void upload(file);
            return;
          }
        }
      }
    };

    document.addEventListener('paste', onPaste);
    return () => document.removeEventListener('paste', onPaste);
    // upload 是每次渲染新建的闭包，但不影响正确性；依赖只关心这两个开关
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capturePaste, assetId]);

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOver(false);
    takeFiles(event.dataTransfer.files);
  };

  if (assetId !== '') {
    return (
      <div className="col" style={{ gap: 8 }}>
        <div className="asset-preview">
          <div className="asset-preview__frame">
            <Thumb assetId={assetId} alt={title} raw className="asset-preview__img" placeholder="无预览" />
          </div>
          <div className="asset-preview__meta">
            <div style={{ fontWeight: 600, color: 'var(--text)' }}>{title}</div>
            {meta ? (
              <>
                <div>
                  {meta.width ?? '?'}×{meta.height ?? '?'} px
                  {meta.dpi ? ` · ${meta.dpi} dpi` : ''} · {formatBytes(meta.bytes)}
                </div>
                <div className="mono small" title={meta.sha256}>
                  {meta.sha256.slice(0, 12)}…
                </div>
                <div className="small">
                  {meta.is_immutable ? '原始件（不可变）' : `派生件（${meta.kind}）`}
                </div>
              </>
            ) : (
              <div className="small faint">已关联图片。元数据将在刷新后显示。</div>
            )}
            <div className="row" style={{ gap: 6, marginTop: 4 }}>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => inputRef.current?.click()}
                disabled={busy}
              >
                替换
              </button>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => {
                  onChange('');
                  setMeta(null);
                }}
                disabled={busy}
                title="解除引用（不会删除原始文件）"
              >
                清除
              </button>
            </div>
          </div>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="sr-only"
          onChange={(event) => {
            takeFiles(event.target.files);
            event.target.value = '';
          }}
        />
      </div>
    );
  }

  return (
    <div className="col" style={{ gap: 8 }}>
      <div
        className={`uploader${dragOver ? ' uploader--over' : ''}${busy ? ' uploader--busy' : ''}`}
        style={compact ? { padding: 14 } : undefined}
        role="button"
        tabIndex={0}
        onClick={() => {
          if (!busy) inputRef.current?.click();
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
      >
        <div className="uploader__title">{busy ? `上传中… ${progress}%` : title}</div>
        <div className="uploader__hint">{hint}</div>
        {busy ? (
          <div className="progress" style={{ width: '70%', marginTop: 6 }}>
            <div className="progress__fill" style={{ width: `${progress}%` }} />
          </div>
        ) : null}
      </div>

      {error ? <Alert kind="error">{error}</Alert> : null}

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        className="sr-only"
        onChange={(event) => {
          takeFiles(event.target.files);
          event.target.value = '';
        }}
      />
    </div>
  );
}
