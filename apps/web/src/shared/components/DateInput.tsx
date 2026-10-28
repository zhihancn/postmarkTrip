import { yesterdayISO, todayISO } from '../format';

interface DateInputProps {
  id?: string;
  /** "" 表示空值。不用 null，避免调用点到处判空。 */
  value: string;
  onChange: (value: string) => void;
  /** 显示"今天 / 昨天 / 清空"快捷按钮（跑戳日期几乎总是这几天） */
  quick?: boolean;
  disabled?: boolean;
  min?: string;
  max?: string;
  invalid?: boolean;
}

/**
 * 原生 date input + 快捷按钮。
 *
 * 为什么不引日期库组件：原生 `<input type="date">` 在中文环境下自带本地化、
 * 自带键盘输入、自带移动端选择器，而第三方日期组件的体积与样式适配成本都不划算。
 */
export function DateInput({
  id,
  value,
  onChange,
  quick = false,
  disabled,
  min,
  max,
  invalid,
}: DateInputProps) {
  const input = (
    <input
      id={id}
      type="date"
      className={invalid ? 'input input--invalid' : 'input'}
      value={value}
      disabled={disabled}
      min={min}
      max={max}
      onChange={(event) => onChange(event.target.value)}
    />
  );

  if (!quick) return input;

  return (
    <div className="date-input">
      {input}
      <div className="date-quick">
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          disabled={disabled}
          onClick={() => onChange(todayISO())}
          title="设为今天"
        >
          今天
        </button>
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          disabled={disabled}
          onClick={() => onChange(yesterdayISO())}
          title="设为昨天"
        >
          昨天
        </button>
        {value ? (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            disabled={disabled}
            onClick={() => onChange('')}
            title="清空"
          >
            ✕
          </button>
        ) : null}
      </div>
    </div>
  );
}
