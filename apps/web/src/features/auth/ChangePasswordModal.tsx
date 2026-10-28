import { useState, type FormEvent } from 'react';

import { toApiError } from '../../shared/api';
import { Field } from '../../shared/components/Field';
import { Modal } from '../../shared/components/Modal';
import { Alert } from '../../shared/components/States';
import { useToast } from '../../shared/components/Toast';
import { useChangePassword } from './useSession';

interface ChangePasswordModalProps {
  open: boolean;
  onClose: () => void;
}

/**
 * 改口令。
 *
 * 后端 `POST /api/auth/password` 的字段是 `current_password` / `new_password` ——
 * 刻意不复用登录用的 `LoginRequest`，避免"username = 新口令"这种反直觉映射。
 */
export function ChangePasswordModal({ open, onClose }: ChangePasswordModalProps) {
  const toast = useToast();
  const change = useChangePassword();

  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  const tooShort = newPassword !== '' && newPassword.length < 8;
  const mismatch = confirmPassword !== '' && confirmPassword !== newPassword;
  const canSubmit =
    oldPassword !== '' && newPassword.length >= 8 && newPassword === confirmPassword;

  const reset = () => {
    setOldPassword('');
    setNewPassword('');
    setConfirmPassword('');
    change.reset();
  };

  const onCloseInternal = () => {
    reset();
    onClose();
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    change.mutate(
      { current_password: oldPassword, new_password: newPassword },
      {
        onSuccess: (result) => {
          toast.success(result.message ?? '口令已更新');
          onCloseInternal();
        },
      },
    );
  };

  return (
    <Modal
      open={open}
      title="修改口令"
      onClose={onCloseInternal}
      footer={
        <>
          <button type="button" className="btn" onClick={onCloseInternal}>
            取消
          </button>
          <button
            type="submit"
            form="change-password-form"
            className="btn btn--primary"
            disabled={!canSubmit || change.isPending}
          >
            {change.isPending ? '提交中…' : '确认修改'}
          </button>
        </>
      }
    >
      <form id="change-password-form" onSubmit={onSubmit} className="col" style={{ gap: 12 }}>
        {change.isError ? <Alert kind="error">{toApiError(change.error).message}</Alert> : null}

        <Field label="当前口令" required htmlFor="old-password">
          <input
            id="old-password"
            className="input"
            type="password"
            autoComplete="current-password"
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
          />
        </Field>

        <Field
          label="新口令"
          required
          htmlFor="new-password"
          hint="至少 8 位。这是局域网内唯一的访问凭据，别用弱口令。"
          error={tooShort ? '新口令至少 8 位' : null}
        >
          <input
            id="new-password"
            className="input"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
        </Field>

        <Field
          label="确认新口令"
          required
          htmlFor="confirm-password"
          error={mismatch ? '两次输入不一致' : null}
        >
          <input
            id="confirm-password"
            className="input"
            type="password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
          />
        </Field>
      </form>
    </Modal>
  );
}
