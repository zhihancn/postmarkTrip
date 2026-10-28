import { Link } from 'react-router-dom';

import { EmptyState } from '../../shared/components/EmptyState';

interface GalleryComingSoonProps {
  title: string;
  /** 为什么还没做、下一轮会做什么 */
  text: string;
}

/**
 * 戳墙 / 票墙 / 地图的占位。
 *
 * **刻意不是 404**：外壳与筛选条已经可用，Tab 也在正确位置；
 * 点进来看到"下一轮做"比看到"页面不存在"更能说明现在的状态。
 */
export function GalleryComingSoon({ title, text }: GalleryComingSoonProps) {
  return (
    <EmptyState
      icon="🚧"
      title={`${title}还没做`}
      text={text}
      action={
        <Link className="btn" to="/cards">
          先去看卡片
        </Link>
      }
    />
  );
}
