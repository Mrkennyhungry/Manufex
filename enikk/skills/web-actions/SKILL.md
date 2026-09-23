---
name: web-actions
description: "浏览器网页自动化（Playwright DOM 级）。Triggers: 浏览器, 网页, 登录, 网站, url, 控制台, 表单, 搜索, 下拉, 填写"
tags: [web, playwright, browser, automation]
platforms: [windows]
---

# 网页自动化（Playwright DOM 级）

浏览器内的操作**一律优先用 `web_*` 工具**（真实 selector，稳定且快），
不要用截图+坐标去点网页。受控 Chromium 带持久 profile（`.enikk-home/web_profile/`）：
**登录过一次的站点下次免登录**，用户随时可以肉眼监督、人工接管（如验证码）。

## 工具速查

| 工具 | 用途 |
| --- | --- |
| `web_open(url)` | 打开/导航网址（复用已开页签） |
| `web_snapshot(max_elements)` | **采集可交互元素**：tag/文本/候选 selector。一切的起点 |
| `web_click(selector)` | 点击（失败自动附结构化诊断） |
| `web_type(selector, text, press_enter)` | 输入框 fill（中文安全），可带回车 |
| `web_select_option(selector, value)` | 原生 `<select>` 下拉 |
| `web_press_key(key)` | Enter/Escape/Tab/方向键 |
| `web_scroll(pixels)` | 页面滚动 |
| `web_extract(selector, max_chars)` | 提取文本（列表/表格/详情） |
| `web_wait(selector, timeout_ms)` | 等待元素出现（新页签/异步加载） |
| `web_status()` / `web_close()` | 查看页签 / 关闭浏览器（登录态保留） |

## 硬性铁律（来自真实自动化会话的教训）

1. **selector 必须来自最近的 `web_snapshot`**——禁止凭记忆、凭截图、凭上次会话猜。
   页面一点击就可能变化。
2. **每次点击 / 切 tab / 开抽屉后必须重新 snapshot**——DOM 变了，旧 selector 即刻失效。
   （这是弱模型死循环的头号根因）
3. **相信采集结果**：页面实际比你预期的少一步就跳过那步，多一步就加上。
   不要反复点"取消/返回"重试——那是死循环入口。发现走错页面，直接 web_open 正确 URL。
4. **失败诊断优先**：`web_click` 失败时返回 `diagnosis`（零匹配 → 重新 snapshot；
   多匹配 → 加容器前缀或换更独特文本；不可见 → 元素被折叠，先点它的父级展开）。
   同一 selector 最多原样重试 2 次，然后必须换策略。
5. **登录态**：持久 profile 会记住登录。若遇到验证码/扫码，请用户在可见的浏览器窗口里
   人工完成，然后继续 snapshot（不要反复刷新）。

## 推荐节奏

```
web_open → web_snapshot → web_click/web_type → web_snapshot（验证/下一步）
→ … → web_extract（核对结果） → 汇报
```

每个动作之后都要重新观察——和桌面流程一样，"观察→动作→再观察"。

## 与桌面工具的分工

- 页面内的一切 → 优先 `web_*`（selector 稳定、快、可验证）
- **视觉兜底（web_* 卡住时）**：按诊断换法重试 2-3 次仍失败，或页面根本采不到元素
  （canvas 应用、复杂 iframe、shadow DOM）时，切截图视觉来判断：
  1. `ioa_pick_window` 绑定浏览器窗口 → `ioa_analyze` 看页面实际状态
     （是不是有弹层/验证码挡路？页面和预期不一样？）
  2. 据所见二选一：直接 `ioa_click` / `ioa_type_text` 操作；
     或看清结构后回到 `web_*` 用修正后的 selector（更稳，优先）
  3. 视觉操作后必须再 `web_snapshot` 验证状态
- 页面外的系统弹窗（文件上传对话框、打印对话框、浏览器自身菜单）→ 桌面 `ioa_*`（视觉/UIA）
- 上传文件：web_click 点"上传"按钮 → 弹出**系统文件对话框** → 切桌面工具处理 → 回到 web_* 验证
