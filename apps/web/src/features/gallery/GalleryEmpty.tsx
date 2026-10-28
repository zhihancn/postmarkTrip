import { Link } from 'react-router-dom';

import { EmptyState } from '../../shared/components/EmptyState';

interface GalleryEmptyProps {
  /** true = 有筛选条件；false = 库里真的没有记录 */
  filtered: boolean;
  onReset: () => void;
  /** 「卡片」/「戳」/「票」—— 决定文案 */
  resource?: string;
}

/**
 * 展览集的两种空态（验收 G6）：**0 条数据**与**筛选后 0 条**要给不同的下一步。
 *
 * 前者引导去录入（否则用户只看到一个白眼），后者引导清除筛选（否则用户以为
 * 数据丢了）。四个视图共用这一个组件，措辞只随资源名变化。
 */
export function GalleryEmpty({ filtered, onReset, resource = '卡片' }: GalleryEmptyProps) {
  if (filtered) {
    return (
      <EmptyState
        icon="🔍"
        title={`当前筛选下没有${resource}`}
        text="条件可能太窄了。清掉筛选看看全部，或者只放宽其中一项。"
        action={
          <button type="button" className="btn btn--primary" onClick={onReset}>
            清除筛选
          </button>
        }
      />
    );
  }

  return (
    <EmptyState
      icon="🗂"
      title="展览集还是空的"
      text={`先录入${resource}，这里才有东西可看。有扫描件的话，最快的是走一遍「扫描识别」。`}
      action={
        <div className="row" style={{ gap: 8 }}>
          <Link className="btn btn--primary" to="/recognize">
            去扫描识别
          </Link>
          <Link className="btn" to="/cards/new">
            ＋ 新建卡片
          </Link>
        </div>
      }
    />
  );
}
