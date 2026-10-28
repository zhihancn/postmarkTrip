import { useState, type FormEvent } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { Alert, LoadingState } from '../../shared/components/States';
import { useLogin, useMe } from './useSession';

interface LocationState {
  from?: string;
}

export function LoginPage() {
  const location = useLocation();
  const me = useMe();
  const login = useLogin();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  // 登录页也要显示后端自检结果 —— 部署出问题时，第一眼就能看出是哪一环断了
  const health = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    retry: false,
    staleTime: 30_000,
  });

  const state = location.state as LocationState | null;
  const redirectTo = state?.from ?? '/cards';

  // 先确认会话，避免已登录用户看到登录表单闪一下
  if (me.isLoading) {
    return (
      <div className="login">
        <div className="login__card">
          <LoadingState text="正在确认登录状态…" />
        </div>
      </div>
    );
  }

  if (me.data) {
    return <Navigate to={redirectTo} replace />;
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (username.trim() === '' || password === '') return;
    login.mutate({ username: username.trim(), password });
  };

  return (
    <div className="login">
      <form className="login__card" onSubmit={onSubmit}>
        <div className="login__head">
          <img className="app-nav__mark" src="/favicon.svg" alt="" style={{ width: 40, height: 40 }} />
          <div className="login__title">跑戳日志</div>
          <div className="login__subtitle">记录每一枚邮戳背后的行程</div>
        </div>

        {login.isError ? <Alert kind="error">{toApiError(login.error).message}</Alert> : null}

        <label className="field">
          <span className="field__label">用户名</span>
          <input
            className="input"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>

        <label className="field">
          <span className="field__label">口令</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        <button
          type="submit"
          className="btn btn--primary btn--block"
          disabled={login.isPending || username.trim() === '' || password === ''}
        >
          {login.isPending ? '登录中…' : '登录'}
        </button>

        <div className="login__foot">
          {health.isLoading ? <div>正在检查后端…</div> : null}
          {health.isError ? (
            <div style={{ color: 'var(--danger)' }}>
              无法连接后端（{toApiError(health.error).message}）
            </div>
          ) : null}
          {health.data ? (
            <>
              <div className="kv">
                <span className="kv__k">数据库</span>
                <span className="kv__v">{health.data.database ? '正常' : '不可用'}</span>
              </div>
              <div className="kv">
                <span className="kv__k">资产目录</span>
                <span className="kv__v">{health.data.assets_writable ? '可写' : '不可写'}</span>
              </div>
              <div className="kv">
                <span className="kv__k">投递目录</span>
                <span className="kv__v">{health.data.inbox_present ? '已发现' : '未发现'}</span>
              </div>
            </>
          ) : null}
        </div>
      </form>
    </div>
  );
}
