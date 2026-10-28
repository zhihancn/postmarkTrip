import { NavLink, Outlet, useLocation } from 'react-router-dom';

import { GalleryFilterBar } from './GalleryFilterBar';
import {
  GALLERY_VIEWS,
  galleryPath,
  type GalleryView,
} from './galleryFilters';
import { useGalleryFilters, type GalleryOutletContext } from './useGalleryFilters';

const TABS: { view: GalleryView; label: string; pending: boolean }[] = [
  { view: 'timeline', label: '时间轴', pending: false },
  { view: 'postmarks', label: '戳墙', pending: false },
  { view: 'stamps', label: '票墙', pending: false },
  { view: 'map', label: '地图', pending: false },
];

function currentView(pathname: string): GalleryView {
  const segment = pathname.split('/').filter(Boolean)[1];
  return (GALLERY_VIEWS as readonly string[]).includes(segment ?? '')
    ? (segment as GalleryView)
    : 'timeline';
}

/**
 * 展览集外壳（M3-0）：标题 + 四个视图 Tab + 共用筛选条 + 空态由各视图自己给。
 *
 * 筛选状态**只在这里持有**，四个视图通过 `Outlet context` 取用 ——
 * 否则切 Tab 会丢掉条件，或者每个视图各写一份解析逻辑。
 */
export function GalleryPage() {
  const location = useLocation();
  const { state, patch, reset, activeCount } = useGalleryFilters();
  const view = currentView(location.pathname);

  const context: GalleryOutletContext = { state, patch, reset, activeCount };

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <h1 className="page__title">展览集</h1>
          <div className="page__subtitle">
            把记录变成可浏览的墙。筛选条件都在 URL 里 —— 刷新、后退、分享都不丢。
          </div>
        </div>
      </header>

      <nav className="tabs" aria-label="展览集视图">
        {TABS.map((tab) => (
          <NavLink
            key={tab.view}
            to={galleryPath(tab.view, state)}
            className={({ isActive }) => `tab${isActive ? ' tab--active' : ''}`}
          >
            {tab.label}
            {tab.pending ? <span className="gallery-tab__pending">待做</span> : null}
          </NavLink>
        ))}
      </nav>

      <GalleryFilterBar
        state={state}
        patch={patch}
        reset={reset}
        activeCount={activeCount}
        showSource={view === 'timeline'}
      />

      <Outlet context={context} />
    </div>
  );
}
