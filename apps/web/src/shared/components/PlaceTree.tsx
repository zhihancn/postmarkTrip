import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../api';
import type { PlaceNode, PlaceRead } from '../types';
import { InlineSpinner } from './States';

/**
 * 行政区划树。**按需展开，不一次拉全树** —— 中国五级区划有几十万节点，
 * 后端 `/api/places/tree` 也是这个设计（每次只取一层）。
 *
 * 后端没有 `has_children` 字段，所以展开箭头一律显示；展开后为空就提示"无下级"。
 */

interface PlaceTreeProps {
  selectedId?: string | null;
  onSelect?: (place: PlaceRead) => void;
  renderActions?: (place: PlaceNode) => ReactNode;
  showOfficeCount?: boolean;
  countryCode?: string | null;
}

function ChildNodes({
  parentId,
  depth,
  selectedId,
  onSelect,
  renderActions,
  showOfficeCount,
  countryCode,
}: {
  parentId: string;
  depth: number;
  selectedId?: string | null;
  onSelect?: (place: PlaceRead) => void;
  renderActions?: (place: PlaceNode) => ReactNode;
  showOfficeCount?: boolean;
  countryCode?: string | null;
}) {
  const query = useQuery({
    queryKey: ['places', 'children', parentId],
    queryFn: () => api.places.tree(parentId, 2, countryCode),
    staleTime: 60_000,
  });

  if (query.isLoading) {
    return (
      <div className="tree__children">
        <div className="row small muted" style={{ padding: '4px 6px' }}>
          <InlineSpinner /> 加载下级…
        </div>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="tree__children">
        <div className="small" style={{ padding: '4px 6px', color: 'var(--danger)' }}>
          下级加载失败
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => query.refetch()}>
            重试
          </button>
        </div>
      </div>
    );
  }

  const node = query.data?.[0];
  const children = node?.children ?? [];

  if (children.length === 0) {
    return (
      <div className="tree__children">
        <div className="small faint" style={{ padding: '4px 6px' }}>
          （无下级）
        </div>
      </div>
    );
  }

  return (
    <div className="tree__children">
      {children.map((child) => (
        <TreeNodeView
          key={child.id}
          node={child}
          depth={depth + 1}
          selectedId={selectedId}
          onSelect={onSelect}
          renderActions={renderActions}
          showOfficeCount={showOfficeCount}
          countryCode={countryCode}
        />
      ))}
    </div>
  );
}

function TreeNodeView({
  node,
  depth,
  selectedId,
  onSelect,
  renderActions,
  showOfficeCount,
  countryCode,
}: {
  node: PlaceNode;
  depth: number;
  selectedId?: string | null;
  onSelect?: (place: PlaceRead) => void;
  renderActions?: (place: PlaceNode) => ReactNode;
  showOfficeCount?: boolean;
  countryCode?: string | null;
}) {
  // 根层已经带了一层 children（depth=1 时为空），展开时再拉
  const [expanded, setExpanded] = useState(false);
  const isSelected = selectedId !== undefined && selectedId === node.id;

  return (
    <div className="tree__node">
      <div className={`tree__row${isSelected ? ' tree__row--active' : ''}`}>
        <button
          type="button"
          className={`tree__toggle${expanded ? '' : ''}`}
          aria-label={expanded ? '收起' : '展开'}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? '▼' : '▶'}
        </button>

        {onSelect ? (
          <button
            type="button"
            className="tree__label"
            onClick={() => onSelect(node)}
            title={node.path ?? node.name}
          >
            {node.name}
          </button>
        ) : (
          <span className="tree__label" title={node.path ?? node.name}>
            {node.name}
          </span>
        )}

        <span className="tree__meta">
          {node.level_label ? <span>{node.level_label}</span> : <span>L{node.level}</span>}
          {showOfficeCount ? <span>{node.post_office_count} 局</span> : null}
        </span>

        {renderActions ? <span className="tree__actions">{renderActions(node)}</span> : null}
      </div>

      {expanded ? (
        <ChildNodes
          parentId={node.id}
          depth={depth}
          selectedId={selectedId}
          onSelect={onSelect}
          renderActions={renderActions}
          showOfficeCount={showOfficeCount}
          countryCode={countryCode}
        />
      ) : null}
    </div>
  );
}

export function PlaceTree({
  selectedId,
  onSelect,
  renderActions,
  showOfficeCount = false,
  countryCode,
}: PlaceTreeProps) {
  const query = useQuery({
    queryKey: ['places', 'roots', countryCode ?? null],
    queryFn: () => api.places.tree(null, 1, countryCode),
    staleTime: 60_000,
  });

  if (query.isLoading) {
    return (
      <div className="row small muted" style={{ padding: 8 }}>
        <InlineSpinner /> 加载行政区划…
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="alert alert--error">
        <div className="alert__body">行政区划加载失败。</div>
        <button type="button" className="btn btn--sm" onClick={() => query.refetch()}>
          重试
        </button>
      </div>
    );
  }

  const roots = query.data ?? [];

  if (roots.length === 0) {
    return (
      <div className="small faint" style={{ padding: 8 }}>
        还没有任何行政区划数据。可以手工新增，或先在「地点」页导入基础数据。
      </div>
    );
  }

  return (
    <div className="tree">
      {roots.map((root) => (
        <TreeNodeView
          key={root.id}
          node={root}
          depth={0}
          selectedId={selectedId}
          onSelect={onSelect}
          renderActions={renderActions}
          showOfficeCount={showOfficeCount}
          countryCode={countryCode}
        />
      ))}
    </div>
  );
}
