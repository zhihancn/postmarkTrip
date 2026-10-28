export interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  options: readonly SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  invalid?: boolean;
  className?: string;
}

/**
 * 受控 select。options 一律来自 `/api/vocab` —— 组件不知道任何枚举取值。
 * 空字符串代表"未选择"，此时显示 placeholder。
 */
export function Select({
  id,
  value,
  onChange,
  options,
  placeholder,
  disabled,
  invalid,
  className,
}: SelectProps) {
  const classes = ['select'];
  if (invalid) classes.push('select--invalid');
  if (className) classes.push(className);

  return (
    <select
      id={id}
      className={classes.join(' ')}
      value={value}
      disabled={disabled}
      aria-invalid={invalid ? true : undefined}
      onChange={(event) => onChange(event.target.value)}
    >
      {placeholder !== undefined ? <option value="">{placeholder}</option> : null}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}
