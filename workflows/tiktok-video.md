# TikTok 带货视频生成工作流（可复用沉淀）

> 来源：`/Users/earan/Documents/tiktokshop/video/` 实测链路 + flow2api fork `main`（原 `work/video-env-20260922` 已并入 main，PR #1）。
> 原则：成片入口为 ffmpeg 生成器（带货走 `build_video.py`，短剧走 `videoctl drama`）；AI 素材只进素材池；发布一律人工。

## 1. 素材流向

```
video/products/*.json        # 生成配置（脚本定稿后落地，含 scenes/caption/voice）
  -> video/assets/{id}/      # 图片 / 配音 / 中间产物缓存（按商品 id 分目录）
  -> video/output/*.mp4      # 成品（人工审片对象）
```

- 配置示例：`products/3337_pajama_th_v1.json`（泰语 A 主片，`clip` 混排视频片段）
  / `products/pawbalm_*.json`（美区英文，`image` 静帧轮播）。
- 场景配置：`{"clip": "clips/xx.mp4"}` 用视频片段，`{"image": N}` 用静帧，可混排。
- 字幕与配音分离：`voice` 给 TTS 发音用，`caption` 给观众看；不配 caption 默认用 voice。

## 2. 带货成片（ffmpeg 入口：build_video.py）

```bash
cd /Users/earan/Documents/tiktokshop/video
.venv/bin/python build_video.py products/<id>.json [--bgm /path/to/bgm.mp3]
```

流水线：商品配置 JSON -> 下载主图 -> edge-tts 逐场景配音 -> Ken Burns 场景渲染
-> 拼接 -> 烧录字幕 + 合成配音 -> MP4（1080x1920, 30fps）。
目标时长 30-40s，场景 2-4s 短切 + 推拉镜头，必烧录字幕。

## 3. 素材分工（四件套定位）

| 环节 | 工具 | 作用 |
|---|---|---|
| 素材生成 | ComfyUI + flow2api bridge | 只做图片 / 图生视频素材，进 `assets/`，不直出成片 |
| 决策溯源 | OpenMontage | 在 build 后生成剪辑决策、素材清单与交付溯源记录（`scripts/to_openmontage.py`，供 HyperFrames 消费） |
| 后期包装 | HyperFrames | 对成片做后期包装（`scripts/to_hyperframes.py`，依赖 OM 产出） |
| 成片 | `build_video.py` (ffmpeg) | 带货路径成片出口（短剧路径由 `videoctl drama` 成片） |

- flow2api fork：`https://github.com/yxr1995-maker/flow2api`，分支 `main`。
- bridge 源码：在 flow2api fork 仓库内 `integrations/comfyui_bridge/`（custom_node + `workflows/*_api.json` 出图/出视频 + tests），本仓不含 bridge 代码。
- 干跑检查：flow2api 仓内 `.github/workflows/video-env-check.yml` 校验 bridge 语法 + workflows JSON；本仓同名 workflow 只校验文档引用的 fork 分支仍存在。
- 人工闸门：③人工审稿（脚本）→ ⑤人工审片（output 成品）→ ⑥人工发布挂车，任何任务不自动发布。

## 4. 相关脚本

- `video/scripts/to_openmontage.py` / `to_hyperframes.py`（带 `--dry-run`，后者依赖 OM 产出）。
- `video/scripts/*.md`：脚本文档（人工审稿对象）。
- 完整流程说明见 `video/带货视频流程.md`。

## 5. 不放 secrets

本目录只放流程文档与配置模板，不放任何 token / cookie / 私钥。凭证走本机已有凭据机制。


## 6. 通用视频 CLI (`videoctl.py`)

用于带货（commerce）与短剧（drama）项目的统筹 CLI，支持参数化路径与 stdlib+ffmpeg 原生处理。

```bash
# 1. ComfyUI 素材生成（显式触发，其他阶段绝不自动扣积分）
python3 videoctl.py generate --workflow examples/comfyui_workflow_example.json --server http://127.0.0.1:8188 --output-dir ./output/generated --manifest ./output/gen_manifest.json

# 2. 带货流水线调度（build 走 .venv，OM/HF 走 system python3，HF 先 OM）
python3 videoctl.py commerce --video-root /Users/earan/Documents/tiktokshop/video --config products/3337_pajama_th_v1.json --stage hf --dry-run

# 3. 短剧分镜拼接（输入驱动，LibTV 生成素材仅作为输入；防止覆盖源片）
python3 videoctl.py drama --manifest examples/drama_manifest_example.json --output ./output/drama_out.mp4

# 4. 预检门禁（缺输入、无音频、不可解码、重复字幕、未核实 claims/price 均失败；亮度异常仅警告）
python3 videoctl.py preflight --manifest examples/drama_manifest_example.json

# 5. 成品质检与哈希清单
python3 videoctl.py qc --video ./output/drama_out.mp4
python3 videoctl.py manifest --files ./output/drama_out.mp4 --output ./output/delivery_manifest.json
```

### ComfyUI Bridge 协议与付费防损

- 链路：`POST /prompt` -> 轮询 `GET /history/{prompt_id}` -> `GET /view` 下载产物并写入 SHA256 到 manifest。
- 离线测试：使用本地 HTTP mock（如 `tests/test_videoctl.py`）或 flow2api fork 的 mock 校验，确认链路正常且消耗 0 积分。
- 真实调用：仅在明确需要生成并确认服务器地址时人工触发 `videoctl generate`，其他任何命令（commerce/drama/preflight/qc）绝不自动发起付费生成。

### 短剧模板与审核门禁注意事项

- 模板定位：`examples/drama_manifest_example.json` 是结构模板，默认 `review_status: "pending"`，不带伪造的价格与功效核验数据。
- 门禁要求：严禁未经人工核验直接标记 `approved` 或伪造价格来源。新项目使用时须填写真实素材路径，完成价格/功效的人工核对并记录来源与时间，确认审核通过后再将状态改为 `approved`。
