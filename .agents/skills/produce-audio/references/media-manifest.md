# 正式媒体清单约定

`production/manifest.json` 是正式媒体阶段的输入、输出和费用证据，不是人工确认记录。`produce check` 和 `next` 会重新核对文件哈希；只写 `status: done` 不算完成。自动检查报告不应加入人工确认交付物清单。

正式 `cues` 阶段只校验并绑定现有的人工 cue 表，不会生成或覆盖表格。它要求当前 `final.md`、同版本的 `final.sentences.json`、含定稿哈希的 cue 表，以及有效的句子锚点；空 cue 表须写明 `no_sfx_reason`。开场前 5 秒按配置语速预估并阻断，实际混音仍须按最终时间表复核。该阶段不调用付费服务，输入快照同时绑定定稿与稳定句子表；修改相关语速、cue 路径或数量预算会使阶段失效。

正式阶段使用 `mode: real`。每条 `inputs` 的路径相对书目项目根目录；每条 `outputs` 的路径相对本集目录。`scope: sfx_library` 仅允许在 `sfx` 或 `mix` 阶段引用本机共享音效库，路径相对音效库根目录。输入、输出都要存文件 SHA-256；不接受绝对路径、越界路径、符号链接或重复条目。已完成归档的媒体可用本集归档清单中的同哈希记录代替本地文件，归档状态仍须单独验收。

```json
{
  "mode": "real",
  "charges": [
    {"id": "voice-request-001", "stage": "voice", "cost_cny": 1.50}
  ],
  "stages": {
    "voice": {
      "status": "done",
      "inputs": [
        {"path": "episodes/ep01/final.md", "sha256": "<64 位小写十六进制>"},
        {"path": "production/voice_cast.yaml", "sha256": "<64 位小写十六进制>"}
      ],
      "outputs": [
        {"path": "production/voice.wav", "sha256": "<64 位小写十六进制>"},
        {"path": "production/timing.json", "sha256": "<64 位小写十六进制>"}
      ],
      "cost_cny": 1.50,
      "charge_ids": ["voice-request-001"]
    }
  }
}
```

`charges` 是逐笔追加的已发生费用占用记录；重跑阶段时不能删去旧尝试。豆包配音金额按任务提交时锁定的单价与服务端报告的计费字符数计算，并标记 `invoice_reconciled: false`；这不是已核实的最终账单，仍须与平台账单对账。`charge_ids` 只列当前阶段记录对应的费用，其金额之和须等于该阶段的 `cost_cny`。预算预检还会计入未结清任务并阻止重发；没有逐笔账的旧清单须先人工核账。口播和音效单价默认为未知，按核实后的实际计费规则写入项目配置。

正式配音的段落预检读取 `voice_production.segments` 指向的 `mode: real` 清单；可复用片段从 `voice_production.parts_dir` 下读取，例如默认的 `voice_parts/para-0001.mp3` 或带版本后缀的同段 MP3。片段须绑定当前段落文字/音色表/口播配置哈希、逐句时间戳、可探测且时长相符的音频，以及 `manifest.json.charges` 中的 `charge_id`。新版本片段应另存文件，不覆盖旧版音频与费用记录；恢复旧文案时仍可选回旧版缓存。任一证据缺失就按待生成计费，不把测试静音或来源不明的音频当作免费缓存。文案改动时，费用预检仅为受影响段落预留新配音费用；历史逐笔费用仍累计。

`bookflow/adapters/doubao_voice.py` 已隔离豆包 2.0 异步提交/查询协议、HTTPS 音频下载和时间戳核对。正式 `voice` 阶段通过 `./run.sh produce <项目> ep01 --until voice --allow-paid` 显式启用：先检查媒体守卫、文案确认、cue 阶段、音色表和预算；每次命令最多提交或查询一段，未完成时重跑会继续原任务。没有 `--allow-paid` 不会调用豆包。提交异常一律视为结果未知，不能自动重复付费。异步接口形状依据本项目已有成功任务记录实现，尚未进行真实服务端请求或账单核对，不能据单元测试宣称正式豆包配音已实地验收。

`production/voice_jobs.json` 是正式配音的请求日志：新请求先写 `request_id` 和输入指纹，再向服务端提交；已有 `task_id` 时只查询同一任务；提交结果未知或任务未结清时，费用预检阻断新的付费请求。服务端报告完成后先登记本次费用，再下载并验证 MP3、逐句时间戳及输入哈希，最后结清该任务；下载失败时只查询原任务以重新取回音频。旧尝试不删除、不改用新 ID 冒充续跑。所有段落可用后才拼接 `voice.wav`、写出 `timing.json` 并登记正式 `voice` 清单，人工试听仍是独立验收。

正式 `sfx` 阶段现可不新增付费请求地绑定已有音效：每条启用的 cue 必须明确选用已试听接受、哈希有效的共享库 `SFX-####`，或指定本集 `production/` 内音频并写入文件哈希。后者还须注明 `asset_origin: user_supplied`，或用 `asset_charge_id` 关联清单中已有的音效收费记录；不能把来源不明的付费素材记成零元。`on_hold` cue 不参与。`./run.sh produce <项目> ep01 --until sfx` 仍先检查媒体守卫。未选定的 cue 只列出共享库候选，不会擅自选用或调用豆包。全部选定后，脚本核对音效时长，写 `production/sfx_bindings.json` 和正式 `sfx` 阶段清单，再接续已有的 FFmpeg 混音；重跑不会覆盖来源不明的绑定文件，中断后按待写哈希恢复。`sfx_production.binding_output` 可在项目配置中覆盖绑定文件路径。豆包 1.0 的**付费音效生成**仍未接入正式适配器，旧本地脚本的请求格式尚未获独立官方接口文档核实，`--allow-paid` 也不会为缺失音效发起请求。

`production/sfx_jobs.json` 是未来付费音效适配器使用的本地请求日志，目前仅实现安全状态机，不会调用服务商。每条 cue 的新请求须先持锁写入 `request_id`、cue／定稿／配置指纹和 `submitting`；已有可查询 `task_id` 只返回原任务，提交结果未知、失败或服务端已完成但未结清时均不新建请求。结清须同时核对同 ID 的唯一逐笔收费记录、cue 的 `asset_charge_id`、本集音频文件哈希及时长；未结清的日志阻止费用预检和正式音效绑定。旧版已收费素材没有该日志时仍按原有费用／文件证据兼容绑定，不伪造补录。当前没有已核实的豆包 1.0 音效接口协议和真实账单验收，所以**不能据此日志调用付费接口或宣称音效自动生成已完成**；不确定的旧尝试应人工核账，不得换 ID 重发。

阶段依赖由 `produce check` 检查：混音需引用口播与音效产物，字幕与分镜需引用混音后的最终时间表，图像需引用分镜，渲染需引用混音、字幕和图像。清单能证明文件未变化，不证明听感、画质、版权或平台账单正确；这些仍要按正式验收流程检查。

正式本地混音与字幕现可用 `./run.sh produce <项目> ep01 --from mix --until subs` 接续已验的 `cues`、`voice`、`sfx` 阶段；也可分别运行 `--from mix`、`--from subs`。默认调用会从首个无效阶段推进：若首个阶段是 `mix`、`subs` 或 `render`，只做下一个本地阶段；遇到需要助手判断的分镜/图片会停下，不伪装已完成。正式音效付费适配器仍未接入，所以仅凭已接入的配音不能从空白书目自动生成完整混音。

`project.yaml` 可覆盖默认 `voice_production.output`、`voice_production.timing_output`、`voice_production.segments`、`voice_production.parts_dir`，以及 `mix.voice`、`mix.timing`、`mix.cues`、`mix.output`、`mix.timing_output`、`mix.max_silence_sec`、`mix.timeout_sec` 和 `subs.timing`、`subs.audio`、`subs.output`。段落缓存清单与片段目录必须留在本集 `production/` 内，路径变化会进入配置指纹并使旧缓存失效；默认值为 `production/voice_segments.json` 和 `production/voice_parts`。配音与混音的旁白、时间表路径必须一致。默认输入分别为 `production/voice.wav`、`production/timing.json`、`production/sound_cues.yaml`；输出为 `production/final_mix.wav`、`production/timing_actual.json` 和 `production/subtitles.srt`。cue 的 `asset_id` 必须指向本集 `sfx` 清单绑定的 `production/` 音频，或已试听接受且哈希有效的本机音效库 `SFX-####`。未准备好的 cue、估算时间表、无效句子锚点、前 5 秒音效、超长留白和输入文件变化都会阻断正式混音。

混音按 `gap` / `partial_gap` 在句子边界插入声音空间，并把位移写入最终句子时间表；`duck` 音效用旁白作侧链压低。生成后复测响度范围、真峰值、时长和长空白，检查报告与 cue 时间线只写入 `production/_reports/mix_qc.json`。字幕逐句采用 `final.md` 稳定句子表的原文标点，末条不得超出最终混音。`mix`、`subs`、`render` 的新清单还保存有效配置摘要；配置或输入改变后不再视为新鲜产物。机器检查不能代替人工试听与发布确认。

本地 FFmpeg 渲染适配器已接入 `./run.sh produce <项目> ep01 --from render`，只在前序正式清单全部有效时运行，不调用豆包或生图。路径、帧率、转场、字幕样式和是否要求标题卡由 `project.yaml` 覆盖默认 `render` 配置；分镜 `shots` 每条需有 `start`、`end`、`image`，图片路径相对分镜文件目录。标题卡默认必需，项目内 `production/title_card.yaml` 的示例：

```yaml
mode: overlay_on_original_plot_frame
image: title.png
start_sec: 12.0
end_sec: 16.0
```

标题卡图片须为带透明通道的 PNG，叠在原剧情镜头上，不延长视频。渲染器核对音视频流、尺寸、时长并在 `production/_reports/` 输出字幕抽帧；抽帧可供人工检查，但机器不会声称字幕视觉、角色身份或标题设计已获人工验收。已有成片若与清单哈希不符，不会被重跑覆盖。当前只测试了短片夹具的单镜头和双镜头转场；真实长片性能、豆包配音与音效适配器仍待验证，不能据此宣称正式音画流水线完成。
