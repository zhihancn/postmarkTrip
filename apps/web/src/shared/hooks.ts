import { useEffect, useState } from 'react';

/**
 * 防抖。用于增量补全的输入框 —— 每敲一个字符就打一次后端既慢又吵。
 *
 * 注意：返回的是"滞后"的值，调用方要用它作为 react-query 的 queryKey 一部分，
 * 这样请求次数才真的降下来。
 */
export function useDebouncedValue<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);

  return debounced;
}

/** 点击/聚焦到元素之外时触发。下拉框、弹层用。 */
export function useOutsideClick<T extends HTMLElement>(
  ref: React.RefObject<T | null>,
  handler: () => void,
  active = true,
): void {
  useEffect(() => {
    if (!active) return;

    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      const node = ref.current;
      if (node === null) return;
      const target = event.target;
      if (target instanceof Node && !node.contains(target)) {
        handler();
      }
    };

    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
    };
  }, [ref, handler, active]);
}
