# 跑戳日志 · postmarkTrip

> 一个自托管的**跑戳（postmark hunting）记录与管理系统**。把纸质邮戳卡数字化、结构化，并长期积累成可检索的个人邮/戳史档案。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: v0.1 in development](https://img.shields.io/badge/status-v0.1%20in%20development-orange.svg)](#路线图)

> **在线演示**：<https://zhihancn.github.io/postmarkTrip/> —— 预录回放，不上传图片、不调用识别、不联网。
> 真实系统需要自托管，见下方「快速开始」。

---

**跑戳**是集邮者专程前往某个邮局、在自己的邮戳卡上请营业员加盖邮戳的行为。它天然带着"我为了这个戳去了这个地方"的时空属性——所以**时间 + 地点**是记录的骨架。

postmarkTrip 要做的事：

1. **记录** —— 卡片、邮戳、邮票、邮局、地点、来源（实地跑戳 / 求戳信），可筛选、可搜索、可统计
2. **识别** —— 扫描邮戳卡后自动预填字段，人工确认即可入库
3. **重建** —— 用电子模板把邮票与邮戳重新合成到虚拟卡片上
4. **分享** —— 个人画廊 + 公共戳库(今后计划)

### English abstract

postmarkTrip is a self-hosted log and management system for **postmark collecting**. It digitizes physical postmark cards (covers bearing a cancellation and the stamps it cancelled), extracts structured records from scans with a human-in-the-loop workflow, and lets collectors re-compose their cards on electronic templates. It ships with a federated data model: private records stay on your machine, public reference data (postmarks, stamps, post offices) can be shared.

---

## 开始

## 1.从源码构建镜像

> 只需要 **Docker**：Windows / macOS 用 Docker Desktop，Linux 用 Docker Engine + compose 插件。
> 宿主机不需要装 Python、Node 或 Go——全部构建在容器里完成。

```powershell
# Windows / PowerShell
git clone https://github.com/zhihancn/postmarkTrip.git
cd postmarkTrip
.\pmtrip.ps1 up
```

```bash
# Linux / macOS
git clone https://github.com/zhihancn/postmarkTrip.git
cd postmarkTrip
make up
```

浏览器打开 <http://localhost:9999>，登录后请**立刻改口令**。

## 2. 直接下载已经构建好的镜像

## 联系

QQ群: 1124888733
