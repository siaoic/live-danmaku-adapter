# -*- coding: utf-8 -*-
"""SiliconFlow ASR 模型测速：同一批音频逐一打 /v1/audio/transcriptions。

音频取自 TTS 训练数据（默认 ./audio），文件名即标注文本，
可顺带比对识别质量。运行前先设置 SILICONFLOW_API_KEY。运行方式：

    python tests/asr_benchmark.py [每条音频跑几轮，默认 2]

只输出耗时与结果，不改插件任何状态。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import List, Tuple

import requests

API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
# 运行前设置环境变量 SILICONFLOW_API_KEY，或直接把 key 填在这里（不要提交）
API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
# 音频目录：每条 wav 的文件名即标注文本
AUDIO_DIR = Path(os.environ.get("ASR_AUDIO_DIR", "audio"))

MODELS = [
    "FunAudioLLM/SenseVoiceSmall",
    "Qwen/Qwen3-ASR-1.7B",
    "XingChenAGI/XingChenASR-V3.2",
    "XingChenAGI/XingChenASR-V3.2-Ultra",
    "XingChenAGI/XingChenGSR-V1.0",
    "XingChenAGI/XingChenASR-Diarize-V3.0",
]

# 短 / 中 / 长各挑一条，覆盖典型语句长度
AUDIOS = [
    "Ok最后一个问题。.wav",
    "一个很很宽的电池。.wav",
    "不知昨天今天明天的区别，在透过早晨咖啡的雾气思索的时候也什么也。.wav",
]


def transcribe(model: str, audio_path: Path, timeout: float = 90.0) -> Tuple[float, str, str]:
    """调一次转写接口，返回 (耗时秒, 文本或空, 错误或空)。"""

    t0 = time.perf_counter()
    with open(audio_path, "rb") as fh:
        files = {
            "file": (audio_path.name, fh),
            "model": (None, model),
        }
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
                files=files,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return time.perf_counter() - t0, "", f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - t0
    if resp.status_code != 200:
        return elapsed, "", f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        data = resp.json()
    except ValueError:
        return elapsed, "", f"非 JSON 响应: {resp.text[:200]}"
    return elapsed, str(data.get("text") or "").strip(), ""


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    missing = [name for name in AUDIOS if not (AUDIO_DIR / name).exists()]
    if missing:
        print(f"音频缺失: {missing}")
        return 1

    print(f"音频目录: {AUDIO_DIR}")
    print(f"每条音频跑 {rounds} 轮，取平均\n")
    summary: List[str] = []
    for model in MODELS:
        times: List[float] = []
        texts: List[str] = []
        errors: List[str] = []
        for name in AUDIOS:
            path = AUDIO_DIR / name
            for _ in range(rounds):
                elapsed, text, err = transcribe(model, path)
                times.append(elapsed)
                if err:
                    errors.append(f"{name[:6]}…: {err}")
                else:
                    texts.append(text)
        avg = sum(times) / len(times) if times else 0.0
        ok = not errors
        summary.append((model, avg, ok))
        status = "OK" if ok else "有错误"
        print(f"=== {model}  平均 {avg:.2f}s  [{status}]")
        for t, name in zip(times, [n for n in AUDIOS for _ in range(rounds)]):
            print(f"    {t:6.2f}s  {name[:18]}…")
        for err in errors[:2]:
            print(f"    错误: {err[:180]}")
        # 展示一次识别文本与标注对比
        if texts:
            print(f"    识别示例: {texts[0][:60]}")
            label = AUDIOS[0].rsplit(".", 1)[0]
            print(f"    标注参考: {label[:60]}")
        print()

    print("====== 汇总（按平均耗时排序） ======")
    for model, avg, ok in sorted(summary, key=lambda x: x[1]):
        flag = "" if ok else "  (有错误)"
        print(f"  {avg:6.2f}s  {model}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
