import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';

import { api, toApiError } from '../../shared/api';
import { EmptyState } from '../../shared/components/EmptyState';
import { Modal } from '../../shared/components/Modal';
import { useToast } from '../../shared/components/Toast';
import type { StampRead } from '../../shared/types';

interface Props {
  stamp: StampRead;
  onClose: () => void;
  onRefresh: () => void;
  onApplied: (stamp: StampRead) => void;
}

function similarityClass(value: number | null): string {
  if (value === null) return 'badge';
  if (value >= 80) return 'badge badge--ok';
  if (value >= 60) return 'badge badge--warn';
  return 'badge badge--danger';
}

export function StampIdentifyModal({ stamp, onClose, onRefresh, onApplied }: Props) {
  const toast = useToast();
  const [selected, setSelected] = useState<number | null>(null);

  const apply = useMutation({
    mutationFn: () => {
      if (selected === null) throw new Error('先选一个候选');
      const replace = stamp.catalog_id !== null;
      if (replace && !window.confirm('这枚票已关联标准票。替换关联？旧标准票不会删除。')) {
        throw new Error('已取消');
      }
      return api.stamps.applyIdentify(stamp.id, selected, replace);
    },
    onSuccess: (data) => {
      toast.success(
        data.created
          ? `已建标准票 ${data.catalog.standard_code} 并关联`
          : `已关联标准票 ${data.catalog.standard_code}`,
      );
      onApplied(data.stamp);
      onClose();
    },
    onError: (error) => {
      const message = toApiError(error).message;
      if (message !== '已取消') toast.error(message);
    },
  });

  return (
    <Modal
      open
      title="识别邮票"
      size="wide"
      onClose={onClose}
      footer={
        <>
          <span className="small faint grow">
            识别只给候选；应用后是 draft，官方页字段请再到标准库「抓取并解析」。
          </span>
          <button type="button" className="btn" onClick={onRefresh}>
            重新搜索
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={selected === null || apply.isPending}
            onClick={() => apply.mutate()}
          >
            {apply.isPending ? '应用中…' : '用这枚'}
          </button>
        </>
      }
    >
      {stamp.identify_status === 'queued' || stamp.identify_status === 'running' ? (
        <div className="small muted">识别中…（Colnect 搜索可能要 20 秒左右）</div>
      ) : null}
      {stamp.identify_status === 'error' ? (
        <div className="catalog-fetch__error">{stamp.identify_error ?? '识别失败'}</div>
      ) : null}
      {stamp.identify_status === 'ok' && stamp.identify_candidates.length === 0 ? (
        <EmptyState
          icon="🔎"
          title="没找到候选"
          text="可以重新搜索，或直接到标准库手工建/选标准票。"
        />
      ) : null}

      {stamp.identify_candidates.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: 34 }} />
              <th style={{ width: 76 }}>图</th>
              <th>名称 / c_id</th>
              <th>相似度</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {stamp.identify_candidates.map((candidate) => (
              <tr
                key={candidate.c_id}
                className={selected === candidate.c_id ? 'is-selected' : undefined}
                onClick={() => setSelected(candidate.c_id)}
                style={{ cursor: 'pointer' }}
              >
                <td>
                  <input
                    type="radio"
                    name="identify-candidate"
                    checked={selected === candidate.c_id}
                    onChange={() => setSelected(candidate.c_id)}
                  />
                </td>
                <td>
                  {candidate.image ? (
                    <img
                      src={candidate.image}
                      alt=""
                      loading="lazy"
                      style={{ width: 54, height: 66, objectFit: 'contain' }}
                    />
                  ) : (
                    <span className="small faint">无图</span>
                  )}
                </td>
                <td>
                  <div>
                    <strong>{candidate.name ?? '未命名'}</strong>
                  </div>
                  <div className="small faint mono">c_id {candidate.c_id}</div>
                  {candidate.url ? (
                    <a
                      className="small"
                      href={candidate.url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(event) => event.stopPropagation()}
                    >
                      Colnect 条目
                    </a>
                  ) : null}
                </td>
                <td>
                  <span className={similarityClass(candidate.similarity)}>
                    {candidate.similarity ?? '—'}
                  </span>
                </td>
                <td className="right">
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelected(candidate.c_id);
                    }}
                  >
                    选这个
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </Modal>
  );
}
