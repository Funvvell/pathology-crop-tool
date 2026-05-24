# 病理裁剪工具

SDPC 全景病理切片查看与汇管区批量裁剪桌面工具。

## 功能

- 打开生强（ShengQiang）SDPC 格式病理全玻片图像
- 金字塔层级浏览，按需加载原图 tile
- 固定尺寸浮动框标注汇管区 ROI
- 按空格键快速创建 ROI
- 批量导出为 TIFF
- 导航缩略图定位

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

## 打包

```bash
pip install pyinstaller
pyinstaller build.spec
# dist/病理裁剪工具.exe
```

## 技术栈

Python 3.10+ · PySide6 · sdpc-for-python · numpy · tifffile
