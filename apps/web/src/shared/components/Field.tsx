import type { ReactNode } from 'react';

interface FieldProps {
  label?: string;
  htmlFor?: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  className?: string;
  children: ReactNode;
}

/** 表单行的统一外壳：标签 + 控件 + 提示/错误。 */
export function Field({ label, htmlFor, hint, error, required, className, children }: FieldProps) {
  const classes = className ? `field ${className}` : 'field';
  const labelContent = (
    <>
      {label}
      {required ? (
        <span className="field__required" aria-hidden="true">
          *
        </span>
      ) : null}
    </>
  );

  return (
    <div className={classes}>
      {label ? (
        // 没有 htmlFor 时用 span：孤立的 <label> 不会被关联到任何控件，
        // 点它不会有反应，读屏器也读不出关系 —— 不如老实当文本。
        htmlFor ? (
          <label className="field__label" htmlFor={htmlFor}>
            {labelContent}
          </label>
        ) : (
          <span className="field__label">{labelContent}</span>
        )
      ) : null}
      {children}
      {error ? <div className="field__error">{error}</div> : null}
      {!error && hint ? <div className="field__hint">{hint}</div> : null}
    </div>
  );
}
