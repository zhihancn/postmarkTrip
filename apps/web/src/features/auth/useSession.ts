import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../../shared/api';
import type { ChangePasswordRequest, LoginRequest, MeResponse } from '../../shared/types';

export const ME_QUERY_KEY = ['auth', 'me'] as const;

/**
 * 当前会话。**401 不是"错误"，而是"未登录"** —— 所以 `retry: false`，
 * 由路由守卫据此跳登录页，而不是弹一个红色错误提示。
 */
export function useMe() {
  return useQuery<MeResponse>({
    queryKey: ME_QUERY_KEY,
    queryFn: api.auth.me,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: LoginRequest) => api.auth.login(payload),
    onSuccess: (me) => {
      // 直接把结果写进缓存，省掉一次 /auth/me 往返
      queryClient.setQueryData(ME_QUERY_KEY, me);
      // 换了用户，之前缓存的所有业务数据都要丢掉
      void queryClient.invalidateQueries();
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.auth.logout(),
    onSuccess: () => {
      queryClient.clear();
    },
    onError: () => {
      // 即使请求失败也要清本地状态，否则界面会卡在"已登录"的假象里
      queryClient.clear();
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (payload: ChangePasswordRequest) => api.auth.changePassword(payload),
  });
}
