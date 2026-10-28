import type { ReactNode } from 'react';

interface EmptyStateProps {
  icon?: string;
  title: string;
  text?: ReactNode;
  action?: ReactNode;
}

/** 空状态一律给"下一步该做什么"，而不是只显示"暂无数据"。 */
export function EmptyState({ icon = '📭', title, text, action }: EmptyStateProps) {
  return (
    <div className="empty">
      <div className="empty__icon" aria-hidden="true">
        {icon}
      </div>
      <div className="empty__title">{title}</div>
      {text ? <div className="empty__text">{text}</div> : null}
      {action ? <div style={{ marginTop: 8 }}>{action}</div> : null}
    </div>
  );
}
