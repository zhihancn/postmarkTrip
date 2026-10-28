import type { ReactNode } from 'react';

import { toApiError } from '../api';

export function InlineSpinner() {
  return <span className="spinner" aria-hidden="true" />;
}

export function LoadingState({ text = '加载中…' }: { text?: string }) {
  return (
    <div className="state-box">
      <span className="spinner spinner--lg" aria-hidden="true" />
      <div>{text}</div>
    </div>
  );
}

interface ErrorStateProps {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}

/**
 * 错误展示。刻意把"网络不可达"和"业务报错"分开措辞 ——
 * 前者要用户去看服务是否启动，后者要用户改输入。
 */
export function ErrorState({ error, onRetry, title }: ErrorStateProps) {
  const apiError = toApiError(error);

  return (
    <div className="state-box">
      <div className="empty__icon" aria-hidden="true">
        ⚠️
      </div>
      <div className="empty__title">{title ?? '加载失败'}</div>
      <div className="empty__text">{apiError.message}</div>
      {apiError.isNetworkError ? (
        <div className="small faint">
          如果是首次部署，请确认后端容器已启动（<code className="mono">GET /api/health</code>）。
        </div>
      ) : null}
      {onRetry ? (
        <button type="button" className="btn btn--sm" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

export type AlertKind = 'info' | 'warn' | 'error' | 'success';

interface AlertProps {
  kind?: AlertKind;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
}

export function Alert({ kind = 'info', title, children, action }: AlertProps) {
  return (
    <div className={`alert alert--${kind}`}>
      <div className="alert__body">
        {title ? <div style={{ fontWeight: 600 }}>{title}</div> : null}
        {children}
      </div>
      {action}
    </div>
  );
}

/** 表单校验错误的内联汇总（422 的 details 会展开成列表）。 */
export function ValidationSummary({ error }: { error: unknown }) {
  const apiError = toApiError(error);
  if (apiError.details && apiError.details.length > 0) {
    return (
      <Alert kind="error" title="提交被拒绝：">
        <ul style={{ marginTop: 4 }}>
          {apiError.details.map((detail, index) => (
            <li key={`${detail.loc.join('.')}-${index}`} className="small">
              <code className="mono">{detail.loc.filter((p) => p !== 'body').join('.') || '(整体)'}</code>
              {' — '}
              {detail.msg}
            </li>
          ))}
        </ul>
      </Alert>
    );
  }
  return <Alert kind="error">{apiError.message}</Alert>;
}
