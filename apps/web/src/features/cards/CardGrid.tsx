import { Link } from 'react-router-dom';

import { Thumb } from '../../shared/components/Thumb';
import { labelOf, useVocabSafe } from '../../shared/vocab';
import { formatFromNow } from '../../shared/format';
import type { CardListItem } from '../../shared/types';
import type { ViewMode } from './useCardFilters';

interface CardGridProps {
  cards: CardListItem[];
  view: ViewMode;
  /** 回收站视图：不给跳详情（详情页的编辑会对已删除记录返回 404），只给恢复/彻底删除 */
  deleted?: boolean;
  onRestore?: (id: string) => void;
  onPurge?: (id: string) => void;
  busyId?: string | null;
}

function PlaceBreadcrumb({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="breadcrumb">
      {names.map((name, index) => (
        <span key={`${name}-${index}`}>
          {index > 0 ? <span className="breadcrumb__sep"> › </span> : null}
          {name}
        </span>
      ))}
    </div>
  );
}

export function CardGrid({ cards, view, deleted, onRestore, onPurge, busyId }: CardGridProps) {
  const { vocab } = useVocabSafe();

  const badges = (card: CardListItem) => (
    <div className="card-tile__badges">
      {card.postmark_type ? (
        <span className="badge badge--primary">{labelOf(vocab.postmark_types, card.postmark_type)}</span>
      ) : null}
      {card.is_color ? <span className="badge badge--color">彩色</span> : null}
      {card.source !== 'field_trip' ? (
        <span className="badge">{labelOf(vocab.card_sources, card.source)}</span>
      ) : null}
      {card.status !== 'collected' ? (
        <span className="badge badge--warn">{labelOf(vocab.card_statuses, card.status)}</span>
      ) : null}
    </div>
  );

  const trashActions = (card: CardListItem) => (
    <div className="row" style={{ gap: 6, marginTop: 8 }}>
      <button
        type="button"
        className="btn btn--sm"
        disabled={busyId === card.id}
        onClick={() => onRestore?.(card.id)}
      >
        恢复
      </button>
      <button
        type="button"
        className="btn btn--sm btn--danger-ghost"
        disabled={busyId === card.id}
        onClick={() => onPurge?.(card.id)}
      >
        彻底删除
      </button>
    </div>
  );

  if (view === 'list') {
    return (
      <div className="card-list">
        {cards.map((card) => {
          const body = (
            <>
              <div className="card-list__thumb">
                <Thumb assetId={card.image_asset_id} alt={card.postmark_summary ?? '邮戳卡'} />
              </div>
              <div className="card-list__main">
                <div className="card-tile__date">
                  {card.visit_date}
                  <span className="small faint">{formatFromNow(card.visit_date)}</span>
                </div>
                {card.title ? <div className="card-tile__title">{card.title}</div> : null}
                {card.postmark_summary ? (
                  <div className="card-tile__line" title={card.postmark_summary}>
                    🏤 {card.postmark_summary}
                  </div>
                ) : null}
                {card.stamp_summary ? (
                  <div className="card-tile__line" title={card.stamp_summary}>
                    🖼 {card.stamp_summary}
                  </div>
                ) : null}
                <PlaceBreadcrumb names={card.place_names} />
                {deleted ? trashActions(card) : null}
              </div>
              <div className="card-list__side">{badges(card)}</div>
            </>
          );

          return deleted ? (
            <div key={card.id} className="card-list__item">
              {body}
            </div>
          ) : (
            <Link key={card.id} to={`/cards/${card.id}`} className="card-list__item">
              {body}
            </Link>
          );
        })}
      </div>
    );
  }

  return (
    <div className="card-grid">
      {cards.map((card) => {
        const inner = (
          <>
            <Thumb assetId={card.image_asset_id} alt={card.postmark_summary ?? '邮戳卡'} />
            <div className="card-tile__body">
              <div className="card-tile__date">
                {card.visit_date}
                <span className="small faint">{formatFromNow(card.visit_date)}</span>
              </div>
              {card.title ? <div className="card-tile__title">{card.title}</div> : null}
              {card.postmark_summary ? (
                <div className="card-tile__line" title={card.postmark_summary}>
                  {card.postmark_summary}
                </div>
              ) : (
                <div className="card-tile__line faint">（没有戳面原文）</div>
              )}
              {card.stamp_summary ? (
                <div className="card-tile__line" title={card.stamp_summary}>
                  {card.stamp_summary}
                </div>
              ) : null}
              <PlaceBreadcrumb names={card.place_names} />
              {badges(card)}
              {deleted ? trashActions(card) : null}
            </div>
          </>
        );

        return deleted ? (
          <div key={card.id} className="card-tile">
            {inner}
          </div>
        ) : (
          <Link key={card.id} to={`/cards/${card.id}`} className="card-tile">
            {inner}
          </Link>
        );
      })}
    </div>
  );
}
