"""应用程序常量定义。"""

# 金字塔渲染
TILE_SIZE = 1024              # tile 读取尺寸
MAX_TILE_CACHE = 512          # LRU 缓存最大 tile 数
RENDER_DEBOUNCE_MS = 200      # 渲染防抖延迟 (ms)
PRELOAD_MARGIN = 0.3          # 预加载 margin 比例
MAX_TILES_PER_CYCLE = 4       # 每次渲染循环最多加载的新 tile 数（避免主线程长时间阻塞）

# ROI 标注
ROI_ID_LENGTH = 12            # UUID hex 截断长度
MIN_ROI_SIZE = 20             # ROI 最小尺寸 (pixels)

# 光学参数
FIELD_NUMBER_MM = 22.0        # 显微镜视场数 (mm)

# UI 相关
PREVIEW_REFRESH_MS = 500      # 预览面板刷新防抖 (ms)
IMAGEJ_CHECK_INTERVAL_MS = 2000  # ImageJ 子进程轮询间隔 (ms)

# 会话
SESSION_DIR_NAME = ".liver_portal_crop"
DEFAULT_OUTPUT_DIR_NAME = "liver_crop_output"
