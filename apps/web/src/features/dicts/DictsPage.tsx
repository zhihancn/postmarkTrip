import { useSearchParams } from 'react-router-dom';

import { PlacesPanel } from './PlacesPanel';
import { PostOfficesPanel } from './PostOfficesPanel';
import { StampCatalogPanel } from './StampCatalogPanel';
import { TagsPanel } from './TagsPanel';

type TabKey = 'places' | 'offices' | 'tags' | 'catalog';

const TABS: { key: TabKey; label: string; hint: string }[] = [
  { key: 'places', label: '行政区划', hint: '地点树：国 → 一级 → 二级 → …' },
  { key: 'offices', label: '邮局', hint: '哪个邮局盖的戳' },
  { key: 'tags', label: '标签', hint: '主题邮局、彩色戳…' },
  { key: 'catalog', label: '标准邮票', hint: '标准票库：志号/图序/Colnect/发行页' },
];

export function DictsPage() {
  // Tab 写进 URL：从票墙「未编目」可以直接链到 /dicts?tab=catalog。
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get('tab');
  const tab: TabKey = TABS.some((item) => item.key === raw) ? (raw as TabKey) : 'places';
  const active = TABS.find((item) => item.key === tab) ?? TABS[0];

  const setTab = (key: TabKey) => {
    setSearchParams(key === 'places' ? {} : { tab: key }, { replace: true });
  };

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <h1 className="page__title">字典</h1>
          <div className="page__subtitle">{active.hint}</div>
        </div>
      </header>

      <div className="tabs">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`tab${item.key === tab ? ' tab--active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === 'places' ? <PlacesPanel /> : null}
      {tab === 'offices' ? <PostOfficesPanel /> : null}
      {tab === 'tags' ? <TagsPanel /> : null}
      {tab === 'catalog' ? <StampCatalogPanel /> : null}
    </div>
  );
}
