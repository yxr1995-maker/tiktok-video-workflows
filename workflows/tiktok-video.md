# TikTok 带货视频生成工作流（可复用沉淀）

> 来源：`/Users/earan/Documents/tiktokshop/video/` 实测链路 + flow2api fork `work/video-env-20260922`。
> 原则：成片唯一入口是 ffmpeg 生成器；AI 素材只进素材池；发布一律人工。

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

## 2. 成片（唯一入口：ffmpeg）

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
| 状态编排 | OpenMontage | 管编排状态（脚本 status=pending-review 等闸门） |
| 后期包装 | HyperFrames | 对成片做后期包装（`scripts/to_hyperframes.py`，依赖 OM 产出） |
| 成片 | `build_video.py` (ffmpeg) | 唯一成片出口 |

- flow2api fork：`/Users/earan/Documents/flow2api`，分支 `work/video-env-20260922`。
- bridge 镜像：`integrations/comfyui_bridge/`（custom_node + `workflows/*_api.json` 出图/出视频 + tests）。
- 干跑检查：`.github/workflows/video-env-check.yml`（bridge 语法 + workflows JSON 有效性，无需付费调用）。
- 人工闸门：③人工审稿（脚本）→ ⑤人工审片（output 成品）→ ⑥人工发布挂车，任何任务不自动发布。

## 4. 相关脚本

- `video/scripts/to_openmontage.py` / `to_hyperframes.py`（带 `--dry-run`，后者依赖 OM 产出）。
- `video/scripts/*.md`：脚本文档（人工审稿对象）。
- 完整流程说明见 `video/带货视频流程.md`。

## 5. 不放 secrets

本目录只放流程文档与配置模板，不放任何 token / cookie / 私钥。凭证走本机已有凭据机制。
