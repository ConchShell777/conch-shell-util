# ComfyUI 批量生图测试工具

## 启动

复制 comfyui_batch.py 到有 Python 的电脑。需要 Python 3.10 或更新版本，且安装了 Tkinter（Windows 官网 Python 安装包一般自带）。不需要 pip 安装任何第三方包。

```text
python comfyui_batch.py
```

先自行启动 ComfyUI，确保网页能够正常生图，再打开本工具。

## 使用步骤

1. 选择 ComfyUI 安装目录，可以是包含 main.py 的目录，也可以是 Windows 便携版根目录。此设置用于输出路径参考，不会启动服务，也不会改变服务使用的模型目录。
2. 填写服务地址，默认 http://127.0.0.1:8188，点击“测试连接”。也可连接另一台电脑的服务，前提是该地址可访问。
3. 导入工作流文件夹，递归显示所有 .json / .JSON 文件。点击文件名称勾选或取消；点击文件夹可整组选择；支持一键全选。小三角仅负责展开/折叠，空格也可切换勾选。无效 JSON 会显示警告，选中后日志会说明原因。
4. 左侧自动汇总已勾选文件中的占位符。同名占位符只显示一次，填写类型和值。string 支持多行；int 要填写整数。
5. 设置每个文件的重复次数，点击“执行选中工作流”。按相对路径排序，先将一个文件重复运行指定次数，再运行下一个文件。每次只提交一个任务，等待它完成后提交下一任务。批次开始时快照文件内容和参数，之后的编辑仅影响下次批次；文件外部修改后需重新导入。
6. 查看底部日志、文件状态和 batch_results 中的报告。

## 占位符

占位符在 JSON 的值中使用，名称区分大小写，允许中文。不会替换键名、节点 ID 或字段名称。

```json
{
  "image": "{{image}}",
  "seed": "{{seed}}",
  "text": "背景：{{background}}"
}
```

整个字符串为占位符时，保留所选类型：选择 int，"{{seed}}" 会成为 JSON 数字。字符串内部的占位符转换为文字拼接。替换过程直接操作解析后的 JSON，不会因为引号、换行或反斜杠破坏文件。空 string 是合法值。

image 通常选择 string，填 ComfyUI input 目录内的文件名或相对路径，例如 example.png 或 portraits/example.png。本工具不上传图片，也不把本机绝对路径自动复制到服务器。

## 种子模式

任何 int 参数均可选择以下模式；种子名称包含 seed 或“种子”时默认选择 int。

- 固定：每次使用填写的整数。
- 随机：每次尝试生成 0 到 2^64-1 的随机整数，填写框可留空。具体节点可能有更小的范围，服务器会校验。
- 递增：第一个任务使用填写值，之后按整个批次的任务尝试序号加 1；失败尝试也占一个序号。达到 2^64 后回到 0。新批次从填写值重新开始。

同一次尝试中，同名占位符共享同一个实际值；不同名称各自计算。模式只影响占位符，工作流内未使用占位符的种子保持原值。普通 JSON 中的“生成后控制”不会覆盖本工具指定的种子。

## 普通工作流兼容范围

API JSON 可直接提交，也兼容顶层含 prompt 的封装格式。普通 UI JSON 使用服务器 /object_info 提供的输入定义转换，支持常规节点、常规控件、连线、Reroute 转接节点和 PrimitiveNode 常量节点。

普通 JSON 的转换无法完全取代 ComfyUI 浏览器前端：部分自定义节点需要 JavaScript 控件或执行前处理，绕过模式、子图、Get/Set 等前端专用节点，以及特殊控件布局无法可靠转换。工具会对可识别的不支持情况显示具体错误，请在 ComfyUI 中打开该文件并导出 API 格式再导入。转换成功也应先运行一个样本确认与网页结果一致。服务器必须已安装相应节点、模型和输入文件。

## 停止、失败和报告

“停止后续执行”停止新任务提交，已经入队的任务仍会执行，工具等待并记录其结果。不调用全局中断，不清空其他任务。等待上限包含排队时间，默认 3600 秒。正在执行时请先停止并等待报告保存后关闭窗口。

服务器明确拒绝或任务明确失败时，记录错误后继续下一个任务。提交响应丢失、连续无法查询状态或等待超时时，停止批次，因为当前任务可能仍在服务器运行；先检查 ComfyUI 队列再决定是否重新执行。POST 提交不会自动重试。

脚本旁的 batch_results/日期时间_随机标识/ 目录包含：

- report.json：源文件、重复次数、实际参数、任务 ID、状态、错误和输出链接。逐任务更新。
- 00001_prompt.json 等：替换及转换后实际提交的 API 工作流。

图片由工作流自身的保存节点写入 ComfyUI 的目录，工具不下载图片。报告输出链接来自 /view，实际输出依节点而定；自定义节点不一定在历史中报告文件。本地路径仅为参考，自定义 --output-directory 或远程服务器情况下请使用输出链接或服务器实际目录。

保存/加载配置可复用服务地址、目录、重复次数、超时和参数；不保存文件勾选。建议把配置和报告放在工作流导入目录之外，避免被扫描成工作流。报告与配置包含实际提示词和参数，请按需保存。

## 离线验证

附带 test_comfyui_batch.py，可在没有 ComfyUI 的环境运行：

```text
python -m unittest -v test_comfyui_batch.py
```

它验证占位符类型、种子模式、普通工作流转换、模拟 HTTP 提交/查询，以及失败后继续和报告生成；不代表已经通过真实 ComfyUI 生图测试。

接口参考：[ComfyUI 官方服务器源码](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py)，[官方前端工作流转换](https://github.com/Comfy-Org/ComfyUI_frontend/blob/main/src/utils/executionUtil.ts)。

## MAIE 图片状态与 API 日志提取

执行前自动根据连线查找 MAIE_ExecuteTasks 和 ShowText|pysssss 的实际节点编号：第二个输出对应 API 连线索引 1，第三个输出对应索引 2。支持同一工作流内存在多个 MAIE 任务节点或多个 ShowText，节点编号会显示在界面日志并写入 report.json。普通工作流先转换为 API 格式再查找；不会使用普通 JSON 中保存的旧 ShowText 显示内容。

任务完成后从当前任务 /history 返回的 outputs 中读取对应 ShowText 的 text。第三个输出按 | 分隔图片状态，每项去掉两端空格后严格比较 success；其他值（包括空项）均记录为图片失败。一张或多张失败会将这次工作流运行标记为“失败”，即使 ComfyUI 的执行状态是 success。report.json 的 generation_result 保存逐图状态、失败位置和日志来源。

失败时生成 00001_failure_log.txt 等 UTF-8 文件，包含工作流路径、任务 ID、MAIE 与 ShowText 节点编号、图片状态和第二个输出的 API 日志。ComfyUI 自身节点报错时，也会保存它已经返回的部分输出和执行错误。失败后仍按原设置继续下一任务。

如果缺少第二个或第三个输出对应的 ShowText，或它未在本次任务中返回 text，则递归查找 outputs 中最长的字符串，保留原文及来源路径。失败时它作为失败日志中的回退内容；没有明确失败时另存为 00001_fallback_log.txt。未连接状态 ShowText 的 MAIE 节点无法可靠判断图片结果，将标记“生成状态未确认”，不会仅凭 ComfyUI 执行成功认定图片成功，也不会把任意最长日志字符串当作图片状态。

每次返回历史结果还会保存 00001_outputs.json 等原始结果文件，便于排查。若服务器根本没有返回任何字符串，日志会说明未提取到 API 日志。该功能提取节点返回内容，不读取 ComfyUI 终端 stdout 或外部 API 服务自身的日志文件。

新增离线测试可运行：

```text
python -m unittest -v test_comfyui_batch.py test_comfyui_task_logs.py
```

ShowText 返回格式参考：[ComfyUI-Custom-Scripts 的 ShowText 源码](https://github.com/pythongosssss/ComfyUI-Custom-Scripts/blob/main/py/show_text.py)。
