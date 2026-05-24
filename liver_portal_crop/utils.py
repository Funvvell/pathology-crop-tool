"""坐标映射工具函数。"""


def map_thumb_to_full(
    thumb_rect: tuple[int, int, int, int],
    thumb_size: tuple[int, int],
    full_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """将缩略图坐标映射到全分辨率（level 0）坐标。

    Args:
        thumb_rect: (x, y, w, h) in thumbnail coordinates
        thumb_size: (width, height) of thumbnail
        full_size: (width, height) of full-resolution image

    Returns:
        (x, y, w, h) in full-resolution coordinates
    """
    tw, th = thumb_size
    fw, fh = full_size
    if tw == 0 or th == 0:
        raise ValueError("Thumbnail dimensions cannot be zero")

    scale_x = fw / tw
    scale_y = fh / th

    tx, ty, tw_roi, th_roi = thumb_rect
    fx = round(tx * scale_x)
    fy = round(ty * scale_y)
    fw_roi = round(tw_roi * scale_x)
    fh_roi = round(th_roi * scale_y)

    return (fx, fy, fw_roi, fh_roi)


def center_crop_rect(
    center_x: int,
    center_y: int,
    crop_w: int,
    crop_h: int,
    image_w: int,
    image_h: int,
) -> tuple[int, int, int, int]:
    """以 (center_x, center_y) 为中心生成裁剪矩形，超出边界时 clamp。

    Returns:
        (x, y, w, h) 保证完全在图像范围内
    """
    half_w = crop_w // 2
    half_h = crop_h // 2

    x1 = center_x - half_w
    y1 = center_y - half_h
    x2 = x1 + crop_w
    y2 = y1 + crop_h

    # clamp to image bounds
    x1 = max(0, x1)
    y1 = max(0, y1)
    x2 = min(image_w, x2)
    y2 = min(image_h, y2)

    return (x1, y1, x2 - x1, y2 - y1)
