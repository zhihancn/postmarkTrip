interface PaginationProps {
  page: number;
  size: number;
  total: number;
  onPage: (page: number) => void;
  onSize?: (size: number) => void;
}

const SIZE_CHOICES = [12, 24, 48, 96];

/** 页码窗口：首页、末页、当前页 ±1，中间用省略号。 */
function pageWindow(current: number, pages: number): (number | 'gap')[] {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, index) => index + 1);
  }
  const wanted = new Set<number>([1, pages, current, current - 1, current + 1]);
  const sorted = [...wanted].filter((page) => page >= 1 && page <= pages).sort((a, b) => a - b);

  const output: (number | 'gap')[] = [];
  let previous = 0;
  for (const page of sorted) {
    if (previous !== 0 && page - previous > 1) output.push('gap');
    output.push(page);
    previous = page;
  }
  return output;
}

export function Pagination({ page, size, total, onPage, onSize }: PaginationProps) {
  const pages = Math.max(1, Math.ceil(total / Math.max(1, size)));
  const safePage = Math.min(Math.max(1, page), pages);
  const from = total === 0 ? 0 : (safePage - 1) * size + 1;
  const to = Math.min(total, safePage * size);

  return (
    <div className="pagination">
      <div className="pagination__info">
        共 {total} 条{total > 0 ? ` · 当前 ${from}–${to}` : ''} · 第 {safePage}/{pages} 页
      </div>

      {onSize ? (
        <label className="row small muted">
          每页
          <select
            className="select"
            style={{ width: 78 }}
            value={size}
            onChange={(event) => onSize(Number(event.target.value))}
          >
            {SIZE_CHOICES.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      <div className="pagination__pages">
        <button
          type="button"
          className="btn btn--sm"
          disabled={safePage <= 1}
          onClick={() => onPage(safePage - 1)}
        >
          上一页
        </button>

        {pageWindow(safePage, pages).map((item, index) =>
          item === 'gap' ? (
            <span key={`gap-${index}`} className="faint small">
              …
            </span>
          ) : (
            <button
              key={item}
              type="button"
              className={`btn btn--sm${item === safePage ? ' btn--primary' : ''}`}
              onClick={() => onPage(item)}
            >
              {item}
            </button>
          ),
        )}

        <button
          type="button"
          className="btn btn--sm"
          disabled={safePage >= pages}
          onClick={() => onPage(safePage + 1)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
