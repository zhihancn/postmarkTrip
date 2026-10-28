import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';

import { Modal } from './Modal';

export interface ConfirmOptions {
  title: string;
  description?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  /** 破坏性操作用红色确认按钮 */
  danger?: boolean;
}

export type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/**
 * 破坏性操作统一走这里：`if (await confirm({...})) { ... }`
 *
 * 用 Promise 而不是回调，是为了让调用点保持线性的 async/await 写法 ——
 * 二十几个删除按钮各自维护 open state 会非常啰嗦。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<{
    options: ConfirmOptions;
    resolve: (value: boolean) => void;
  } | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (options) =>
      new Promise<boolean>((resolve) => {
        setPending({ options, resolve });
      }),
    [],
  );

  const settle = useCallback(
    (value: boolean) => {
      if (pending !== null) {
        pending.resolve(value);
        setPending(null);
      }
    },
    [pending],
  );

  const options = pending?.options;

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Modal
        open={pending !== null}
        title={options?.title ?? ''}
        onClose={() => settle(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => settle(false)}>
              {options?.cancelText ?? '取消'}
            </button>
            <button
              type="button"
              className={`btn ${options?.danger ? 'btn--danger' : 'btn--primary'}`}
              onClick={() => settle(true)}
            >
              {options?.confirmText ?? '确定'}
            </button>
          </>
        }
      >
        {options?.description ?? null}
      </Modal>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const context = useContext(ConfirmContext);
  if (context === null) {
    throw new Error('useConfirm 必须在 <ConfirmProvider> 内使用');
  }
  return context;
}
