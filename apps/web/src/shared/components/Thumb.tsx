import { useEffect, useState } from 'react';

import { assetRawUrl, assetThumbUrl } from '../api';

interface ThumbProps {
  assetId: string | null | undefined;
  alt: string;
  /** 用原图而不是缩略图（邮票/邮戳贴片很小，缩略图可能糊） */
  raw?: boolean;
  /** 缩略图长边。不给则用服务端默认值。 */
  maxPx?: number;
  /** 保留 alpha（戳贴片）；开启后服务端返回 PNG。 */
  alpha?: boolean;
  className?: string;
  placeholder?: string;
}

/**
 * 资产图片。注意 /api/assets/... 是**同源图片地址**，
 * 浏览器会自动带上 httpOnly cookie，所以直接给 <img src> 即可。
 */
export function Thumb({
  assetId,
  alt,
  raw = false,
  maxPx,
  alpha = false,
  className = 'card-tile__thumb',
  placeholder = '暂无图片',
}: ThumbProps) {
  const [failed, setFailed] = useState(false);

  // 换了资产就把失败状态复位，否则换图后永远显示占位
  useEffect(() => {
    setFailed(false);
  }, [assetId]);

  if (!assetId || failed) {
    return <div className={`${className} card-tile__thumb--empty`}>{placeholder}</div>;
  }

  return (
    <img
      className={className}
      src={raw ? assetRawUrl(assetId) : assetThumbUrl(assetId, maxPx, alpha)}
      alt={alt}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}
