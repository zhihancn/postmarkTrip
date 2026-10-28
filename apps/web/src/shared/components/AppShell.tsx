import type { ReactNode } from 'react';
import { NavLink, Outlet } from 'react-router-dom';

interface AppShellProps {
  username: string;
  onLogout: () => void;
  onOpenPassword: () => void;
  /** 渲染在右侧的额外内容（例如同步状态） */
  extra?: ReactNode;
}

const NAV_ITEMS = [
  { to: '/cards', label: '卡片' },
  // 展览集排在卡片后面：它是"看已有记录"的另一个入口（时间轴/戳墙/票墙/地图），
  // 与录入路径（扫描识别）刻意分开。
  { to: '/gallery', label: '展览集' },
  // 扫描识别排在卡片后面：它是**录入入口**，与"看已有记录"是两件事。
  // 放在卡片详情页里的做法试过（原型的思路），但那个页面已经承担了编辑+邮戳+邮票，
  // 再加一整套识别流程就分不清"在看哪一层"了。
  { to: '/recognize', label: '扫描识别' },
  { to: '/dicts', label: '字典' },
  { to: '/stats', label: '统计' },
];

/**
 * 纯展示的外壳：**不依赖任何 feature 模块**，会话信息与动作由 App.tsx 注入。
 * 这样 shared/ 不反向依赖 features/。
 */
export function AppShell({ username, onLogout, onOpenPassword, extra }: AppShellProps) {
  return (
    <div className="app-shell">
      <nav className="app-nav">
        <NavLink to="/cards" className="app-nav__brand">
          <img className="app-nav__mark" src="/favicon.svg" alt="" />
          <span className="app-nav__brand-text">跑戳日志</span>
        </NavLink>

        <div className="app-nav__links">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `app-nav__link${isActive ? ' app-nav__link--active' : ''}`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </div>

        <div className="app-nav__right">
          {extra}
          <span className="small nowrap" title="当前登录用户">
            {username}
          </span>
          <button type="button" className="btn btn--sm btn--ghost" onClick={onOpenPassword}>
            修改口令
          </button>
          <button type="button" className="btn btn--sm" onClick={onLogout}>
            退出
          </button>
        </div>
      </nav>

      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
