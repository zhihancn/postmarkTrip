import { useState } from 'react';
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { isApiError } from './shared/api';
import { AppShell } from './shared/components/AppShell';
import { ConfirmProvider } from './shared/components/Confirm';
import { EmptyState } from './shared/components/EmptyState';
import { LoadingState } from './shared/components/States';
import { ToastProvider } from './shared/components/Toast';
import { ChangePasswordModal } from './features/auth/ChangePasswordModal';
import { LoginPage } from './features/auth/LoginPage';
import { useLogout, useMe } from './features/auth/useSession';
import { CardDetailPage } from './features/cards/CardDetailPage';
import { CardsPage } from './features/cards/CardsPage';
import { DictsPage } from './features/dicts/DictsPage';
import { GalleryPage } from './features/gallery/GalleryPage';
import { MapView } from './features/gallery/MapView';
import { PostmarkWallView } from './features/gallery/PostmarkWallView';
import { StampWallView } from './features/gallery/StampWallView';
import { TimelineView } from './features/gallery/TimelineView';
import { RecognizePage } from './features/recognize/RecognizePage';
import { StatsPage } from './features/stats/StatsPage';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 4xx 是"你说错了"，重试没有意义；网络抖动才值得重试
      retry: (failureCount, error) => {
        if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
        return failureCount < 2;
      },
      refetchOnWindowFocus: false,
      staleTime: 10_000,
    },
    mutations: {
      retry: false,
    },
  },
});

function NotFound() {
  return (
    <EmptyState
      icon="🧭"
      title="页面不存在"
      text="地址可能拼错了，或者这个页面还没做。"
      action={
        <Link className="btn" to="/cards">
          回到卡片列表
        </Link>
      }
    />
  );
}

/**
 * 受保护的区域。
 *
 * 认证是 httpOnly cookie：前端拿不到、也不该拿 token，所以判断"是否登录"
 * 只能靠 `GET /api/auth/me` 是否 401。这里把它做成路由守卫。
 */
function ProtectedLayout() {
  const me = useMe();
  const logout = useLogout();
  const location = useLocation();
  const [passwordOpen, setPasswordOpen] = useState(false);

  if (me.isLoading) {
    return (
      <div className="app-shell">
        <main className="app-main">
          <LoadingState text="正在确认登录状态…" />
        </main>
      </div>
    );
  }

  if (!me.data) {
    // 把当前地址带上，登录后回到原处
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }

  return (
    <>
      <AppShell
        username={me.data.username}
        onLogout={() => logout.mutate()}
        onOpenPassword={() => setPasswordOpen(true)}
      />
      <ChangePasswordModal open={passwordOpen} onClose={() => setPasswordOpen(false)} />
    </>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<ProtectedLayout />}>
        <Route path="/" element={<Navigate to="/cards" replace />} />
        <Route path="/cards" element={<CardsPage />} />
        {/* new 必须排在 :id 之前，否则会被当成 id="new" */}
        <Route path="/cards/new" element={<CardDetailPage />} />
        <Route path="/cards/:id" element={<CardDetailPage />} />
        {/* 扫描识别 → 确认 → 落库。不动库的预览在 /api/recognize，写库在确认之后 */}
        <Route path="/recognize" element={<RecognizePage />} />
        <Route path="/dicts" element={<DictsPage />} />
        <Route path="/stats" element={<StatsPage />} />

        {/* 展览集（M3）。筛选状态在 URL 里，四个视图共用同一份（GalleryPage 持有）。
            时间轴 / 戳墙 / 票墙 / 地图（形态 B）都已实现。 */}
        <Route path="/gallery" element={<GalleryPage />}>
          <Route index element={<Navigate to="timeline" replace />} />
          <Route path="timeline" element={<TimelineView />} />
          <Route path="postmarks" element={<PostmarkWallView />} />
          <Route path="stamps" element={<StampWallView />} />
          <Route path="map" element={<MapView />} />
        </Route>
      </Route>

      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ConfirmProvider>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ConfirmProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}
