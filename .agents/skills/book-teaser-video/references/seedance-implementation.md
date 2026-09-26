# Seedance 调用实现记录

## 已检查的本地样例

来源：用户本机提供的 Seedance 脚本与实现笔记；文件路径不在公开仓库记录。

这些 Python 脚本用 `volcenginesdkarkruntime.Ark`，base URL 为 `https://ark.cn-beijing.volces.com/api/v3`，从 `ARK_API_KEY` 读取密钥。图片按文件扩展名选择 MIME type，base64 编码为 `data:image/...;base64,...`，再按以下结构作为参考图随 2.0 任务提交：

```python
{
    "type": "image_url",
    "image_url": {"url": data_url},
    "role": "reference_image",
}
```

任务由 `client.content_generation.tasks.create(...)` 创建，典型字段包括 `model`、`content`、`generate_audio`、`ratio`、`duration`、`watermark`。记录返回的 task ID 后，以 `client.content_generation.tasks.get(task_id=...)` 轮询 `status`；`succeeded` 时取 `result.content.video_url` 并下载，`failed` 时记录错误并停止。样例每 10–15 秒轮询。远端视频 URL 是临时资源，成功后及时下载到项目制作目录。

样例脚本模型常量为 `doubao-seedance-2-0-fast-260128`。它证明了 SDK、任务轮询、参考图和下载的实现方法，但这些脚本本身并未配置 2.5。随附的方舟 API PDF 在视频生成 API 的图片参数中明确列出公网 URL、Base64 data URL、`asset://` 三种输入形式，因此本地参考图可继续采用已熟悉的 Base64 data URL 方式，2.5 请求使用相同的 `image_url` 结构；图片 role 对参考图必填 `reference_image`。2.5 模型 ID 使用 `doubao-seedance-2-5-260628`，实际调用前仍需确认当前方舟项目已开通该模型。

分集脚本把每镜头所需图片编码后仅发送该镜头使用的参考图，并维护“原资产 ID → 本次上传图片序号”的映射，将提示词里的资产标签重映射成 `图片N`。这能减少参考图混淆。通用合并样例以有序片段列表调用 FFmpeg concat；正式使用需检查编码参数是否一致，若不一致先统一转码再无损拼接。

## 本书先导片的请求要点

- Seedance 2.5 全模态参考生视频支持 0–30 张图片；每张参考图须作为 `type: image_url` 并填写 `role: reference_image`。本书按镜头只发送实际需要的人物定妆照和必要场景参考图，保持资产 ID、上传序号与提示词映射一致。单图需为 jpeg/png/webp/bmp/tiff/gif，宽高比 0.4–2.5、边长 300–6000 px、小于 30 MB；整次请求体不超过 64 MB，大图片使用公网 URL 或资产 ID，避免 Base64 请求过大。
- API 明确禁止直接上传含真人人脸的参考图/视频。若现有定妆照涉及真实人物或相似肖像，使用平台支持的已授权真人/虚拟人像素材流程；不能把 Base64 编码当作绕过审核的方式。虚构角色图按当前接口审核结果使用。
- 明确 ratio、duration 和 generate_audio。先导片需要对白、拟音和环境声随视频由同一个 Seedance 2.5 请求生成，因此设置 generate_audio=true；在提示词中逐句写明允许生成的准确台词、说话人、声线、情绪与时序，并逐项描述所需拟音和环境声。默认不加 BGM，也明确禁止未写入剧本的额外对白、旁白、闲聊和歌词。当前提示词指南支持按对白、音效、BGM分别控制，因此禁掉未请求的音频类别，不要全局禁止对白。成功后逐条试听，重点核对台词准确度、声线、情绪、声场和声画同步。
- 约 20 秒作品先按最终镜头时码分组，再按批次生成。Seedance 2.5 常规 duration 为 4–30 秒，参数按整数秒提交；不要将每个短镜头单独补到 4 秒。合并相邻镜头后让单次任务的 duration 尽量等于该批次成片时长总和；若小数秒相加后不能按接口步进精确表达，重新分组以减少向上取整的计费损耗。每批仅上传该批次对应的参考图，并按上传顺序在同一提示词中绑定镜头号和时间段。出错时仅重做最小必要批次，并重新核算该任务时长和费用。
- 普通关键镜头参考图使用 `reference_image` 作为当前镜头视觉锚点。API 不保证把参考图精确锁在任意视频时间点。
- 方舟视频 API 将首帧、首尾帧和多模态参考列为互斥任务模式。普通关键帧参考图用 `reference_image`；仅在需严格控制视频头尾画面时使用 `first_frame` / `last_frame`。
- 任务完成后即时下载并校验本地文件；记录 task ID、模型、提示词版本、参考图 ID、比例、时长和输出文件。

## 2.5 与模型/官方文档核对

以下为此次实施时查阅的官方入口；模型与参数会变更，执行任务前核对当前页面和 SDK：

- [方舟模型列表](https://docs.volcengine.com/docs/ark/model-list?lang=zh)
- [创建视频生成任务](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)
- [查询视频生成任务](https://docs.volcengine.com/docs/ark/get-video-generation-task-api?lang=zh)
- [方舟素材库参考图说明](https://docs.volcengine.com/docs/ark/private-virtual-avatar-library-guide-preview?lang=zh&redirect=1)

用户提供的 API 参考 PDF 第 6 章“视频生成 API”（印刷页 443–460）；图片输入方式和尺寸约束见 446–447 页，时长、音频和任务字段见 454–459 页。请以本机原件核对，参考文件不随公开仓库分发。

当前模型列表列出的 Seedance 2.5 模型 ID 是 `doubao-seedance-2-5-260628`。PDF 第 6 章说明视频生成 API 为 `POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks`，查询为 `GET https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}`；创建任务返回 ID 后异步轮询，任务状态包括 `queued`、`running`、`succeeded`、`failed`。视频任务记录保留 7 天；生成视频 URL 有效期 24 小时，Seedance 2.5 下载上限为 100 次，应尽快下载归档。

PDF 说明 Seedance 2.5 可单次直出最长 30 秒连续视频、参考图片 0–30 张，并支持全模态参考；常规 duration 范围为 4–30 秒，具体输出约束按当前模型版本与生成任务配置确认。视频 API 支持设置 generate_audio：true 时基于画面和提示词生成声音，false 时输出无声视频。先导片把计划对白、拟音与环境声放在同一请求中，因此采用 true；当前官方提示词指南支持分别控制对白、音效和 BGM。提示词应禁止未请求的对白、旁白、歌词和背景音乐，但保留该镜头明确写出的台词与音效。官方提示词指南可能更新，提交请求前需复核音频控制的最新说明。

若普通 `image_url` 输入不可用，方舟私域素材库指南说明可用 CreateAsset 上传图像/视频/音频资产，随后轮询 GetAsset 至 `Active`，在视频请求的 `image_url.url` 中使用 `asset://asset-ID`。素材资产接口使用 AK/SK 和相应 IAM 权限；私域虚拟人像库还可能要求高级创作权益。不要为单次制作自行购买/开通，先确认账号当前权限；如没有权限，采用模型当前文档支持的公开图片 URL 或已验证的直传形式。

## 先导片一体化声音生成边界

本先导片的计划对白、旁白、拟音和环境声与画面一并由 Seedance 2.5 生成；generate_audio=true 是默认请求。提示词使用连续时间段，把说话人、准确台词、性别与年龄感、音高/音域、音色、口音、语速、情绪强度、声音位置和空间反射写清楚。拟音同时注明声源、触发动作、时点、远近、质感和优先级。没有用户要求时不生成 BGM。

方舟官方提示词指南支持按时间段描述画面、台词和音效，也说明音效、BGM、对白可分别用提示控制。使用用户确认的音频参考时，需按官方 audio reference 模式和对应主体图绑定；音色 ID 不等同于参考音频，也不应作为 Seedance 的参数猜测传入。详见[Doubao Seedance 2.5 提示词指南](https://docs.volcengine.com/docs/ark/seedance-2-5-prompt-guide?lang=zh)。

目前先导片不安排豆包 TTS 1.0 逐句配音或豆包音效模型的独立生产。若 Seedance 对某个镜头的对白/声音生成不合格，先针对该镜头修订提示词并重生成；若重复生成仍不能达到要求，记录具体问题并向用户说明选项，不自行切换成后期配音方案。此项目的长篇口播与声音规则仍按项目工作流执行，不因先导片修改而改变。
