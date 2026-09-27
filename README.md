# tiktok-video-workflows

TikTok 带货与短剧视频工作流沉淀（可复用）：统筹 CLI `videoctl.py` + 素材池隔离 + 本地 ffmpeg 渲染 + 质量/哈希门禁。

- 详细工作流文档：[workflows/tiktok-video.md](workflows/tiktok-video.md)
- 任意视频先做 Seedance 创作策划：[workflows/seedance-planning.md](workflows/seedance-planning.md)
- CI 语法与干跑检查：[.github/workflows/video-env-check.yml](.github/workflows/video-env-check.yml)

## 1. 统一入口 (`videoctl.py`)

制作前先按 [Seedance 创作策划](workflows/seedance-planning.md)选读真实模板并整理分镜；`videoctl` 负责既有生成/合成流程，不会自动读教程或替你选择生成模型。

仓库根目录提供可执行入口 `videoctl.py`（核心位于 `scripts/videoctl.py`）：

```bash
# 查看帮助
python3 videoctl.py --help

# 1. 带货流水线（build 走 venv，OM/HF 走 system python3，HF 先 OM；带货成片由 build_video.py 导出）
python3 videoctl.py commerce --video-root /path/to/video --config products/3337_pajama_th_v1.json --stage hf --dry-run

# 2. 预检门禁（缺输入、无音频、不可解码、连续/双字幕冲突、未核实 claims/price、未审核均硬拦截；亮度仅警告）
# 示例模板 examples/drama_manifest_example.json 初始为 pending，用于演示门禁拦截
python3 videoctl.py preflight --manifest examples/drama_manifest_example.json

# 3. 短剧流水线（短剧成片由 videoctl drama 导出，需替换真实素材路径并经人工核验为 approved）
python3 videoctl.py drama --manifest path/to/approved_manifest.json --output ./output/drama_out.mp4

# 4. 产物质检与 SHA-256 清单
python3 videoctl.py qc --video ./output/drama_out.mp4
python3 videoctl.py manifest --files ./output/drama_out.mp4 --output ./output/manifest.json

# 5. ComfyUI 素材生成（显式触发，经 /prompt->/history->/view；其他阶段绝不自动扣积分）
python3 videoctl.py generate --workflow examples/comfyui_workflow_example.json --output-dir ./output/generated
```

亮度 QC 按视频全长等间隔取 5 个中点样本，逐点输出时间与 YAVG；低于 100 或高于 235 只告警，抽样失败则 QC 失败。

## 2. 素材与产物布局

| 目录/位置 | 内容说明 |
|---|---|
| `video/products/*.json` | 带货视频脚本配置（包含 scenes, caption, voice, price 等元数据） |
| `video/assets/{id}/` | 带货商品图片、TTS 音频、中间剪辑缓存 |
| `video/短剧/ep1/clips/` | 短剧分镜片段与角色素材（外部生成素材如 LibTV 仅作为输入导入素材池） |
| `output/*.mp4` | 最终成片（人工审片对象；带货成片由 `build_video.py` 导出，短剧成片由 `videoctl drama` 导出） |
| `output/*_manifest.json` | 交付与溯源清单（记录 SHA-256、QC 质检结果与素材来源） |

## 3. 审核状态与发布闸门

- **模板需替换与人工审核**：`examples/drama_manifest_example.json` 仅为配置结构模板，默认标为 `review_status: "pending"` 且功效/价格均为未核实占位。实际生产必须替换为真实素材、人工核验真实来源并置为 `review_status: "approved"`，否则 `preflight` 与 `drama` 均强行拒绝执行。
- **价格与宣称（claims/price）人工核验**：未核实的功效承诺或价格标注（`claims_verified: false` 或 `price_verified: false`）严禁展示。若包含价格或功效宣称，必须明确人工核对来源（`price_source` / `claims_source`）及核对时间（`price_verified_at` / `claims_verified_at`），杜绝伪造。
- **人工发布原则**：AI 素材只进素材池，最终发布挂车一律人工确认，不自动推流。

## 5. generate/resume 崩溃恢复

`videoctl generate` 写原子 job file（含 workflow SHA、server URL、prompt ID），支持幂等重入和中断恢复。render 流程当前要求导入的 manifest 包含媒体场景片段（`source_path`）；纯分镜创建的项目尚不支持自动渲染成片。

```bash
# 生成（指定 job file 路径）
python3 scripts/videoctl.py generate \
  --workflow examples/comfyui_workflow_example.json \
  --output-dir ./output/generated \
  --job-file ./output/generated/my-job.json

# 中断后恢复
python3 scripts/videoctl.py resume \
  --job-file ./output/generated/my-job.json \
  --output-dir ./output/generated
```

恢复边界：
- 提交前先落盘并加锁：生成前完成 job 文件落盘并施加 flock，同 job 仅发起一次 POST
- resume 绝不发起 POST：恢复阶段仅查询状态或复用既有产出，遇未知状态（服务端已受理但 prompt ID 未落盘）严格拒绝重新提交
- 严格绑定与目录防护：校验 job schema、server URL、prompt ID 语法及 output-directory 绑定
- 输出路径限制在 output directory 内，防止路径穿越
