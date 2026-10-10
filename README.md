# conch-shell-util

一些实用小工具。

## 工具列表

- [ComfyUI 批量生图测试](comfyui-batch/README.md)：提供图形界面，批量选择工作流、替换占位符、控制种子，并提取 MAIE 图片生成状态和失败 API 日志。支持 API 工作流和常规普通工作流转换。

## 快速启动

需要 Python 3.10+ 和 Tkinter，先自行启动 ComfyUI：

```sh
python comfyui-batch/comfyui_batch.py
```

Windows 下也可双击 `comfyui-batch/start_comfyui_batch.bat` 一键启动。占位符参数会自动保存，并在下次启动后按同名参数恢复。

具体操作、兼容范围和测试方法见工具目录中的使用说明。
