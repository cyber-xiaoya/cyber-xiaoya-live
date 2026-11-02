"""TTS 子包: omnivoice(本机引擎直连) / audio(统一 PCM 工具) / dispatch(__init__)。

本包**不经过** D:\\speech-to-speech 的语音桥 —— 组件版数字人有自己的 TTS 状态
(音色预设独立热切), 与弹幕朗读等消费方互不影响; 底层共享 WSL2 的 OmniVoice 引擎。
"""
