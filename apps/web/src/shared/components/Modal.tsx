import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

export type ModalSize = 'normal' | 'wide' | 'xwide';

interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: ModalSize;
}

const SIZE_CLASS: Record<ModalSize, string> = {
  normal: '',
  wide: ' modal--wide',
  xwide: ' modal--xwide',
};

/**
 * 用 portal 挂到 body：避免被父级的 overflow / transform 裁掉。
 * Esc 关闭、点遮罩关闭、打开期间锁定 body 滚动。
 */
export function Modal({ open, title, onClose, children, footer, size = 'normal' }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // 打开后把焦点移进对话框，键盘用户不会"掉在"背景页面上
    const timer = window.setTimeout(() => {
      const focusable = dialogRef.current?.querySelector<HTMLElement>(
        'input:not([type="hidden"]), select, textarea, button, [tabindex]:not([tabindex="-1"])',
      );
      focusable?.focus();
    }, 0);

    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      window.clearTimeout(timer);
    };
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={`modal${SIZE_CLASS[size]}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={dialogRef}
      >
        <div className="modal__head">
          <div className="modal__title">{title}</div>
          <button type="button" className="btn btn--ghost btn--sm" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>
        <div className="modal__body">{children}</div>
        {footer ? <div className="modal__foot">{footer}</div> : null}
      </div>
    </div>,
    document.body,
  );
}
