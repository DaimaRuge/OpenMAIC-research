# OpenMAIC 学习路径与贡献指南

**仓库:** THU-MAIC/OpenMAIC
**学习者画像:** INTERMEDIATE (中级开发者)
**学习目标:** DEEP_UNDERSTAND + CONTRIBUTE (深度理解 + 贡献)
**可用时间:** 10 小时/周
**分析日期:** 2026-03-20

---

## 前置知识地图

### 必备前置知识

| 知识点 | 为什么需要 | 推荐资源 |
|--------|------------|----------|
| ✅ TypeScript 基础 | 整个项目使用 TS，strict 模式 | [TypeScript 官方文档](https://www.typescriptlang.org/docs/) |
| ✅ React 19 Hooks | UI 组件全部使用函数式组件 | [React 官方文档](https://react.dev/) |
| ✅ Next.js App Router | 路由和 API 设计基于 App Router | [Next.js 文档](https://nextjs.org/docs) |
| ✅ REST API 基础 | 理解 API 端点设计 | [MDN Web Docs](https://developer.mozilla.org/en-US/docs/Web/HTTP) |
| ✅ 异步编程 | Promise, async/await, 流式处理 | [JavaScript.info](https://javascript.info/async) |

### 加分前置知识

| 知识点 | 如何增强理解 |
|--------|--------------|
| Zustand 状态管理 | 理解全局状态管理模式 |
| LangGraph / LangChain | 理解多智能体编排原理 |
| SSE (Server-Sent Events) | 理解流式响应机制 |
| Canvas / SVG | 理解幻灯片渲染和白板 |
| IndexedDB | 理解本地存储机制 |

---

## 分阶段学习路线图

### 🟢 阶段 1：项目定位（第 1-2 周）

**目标:** 理解项目做什么，为什么存在

#### 阅读清单（按顺序）

1. **README.md**
   - 重点: Overview, Features, Quick Start
   - 理解: 项目定位、核心功能、目标用户

2. **在线演示** (https://open.maic.chat/)
   - 操作: 生成一个简单课程，体验完整流程
   - 观察: UI 交互、生成过程、播放体验

3. **论文** (https://jcst.ict.ac.cn/en/article/doi/10.1007/s11390-025-6000-0)
   - 重点: 架构设计理念、技术选型理由

4. **package.json**
   - 理解: 依赖结构、脚本命令

#### 动手任务

- [ ] 本地运行项目
  ```bash
  git clone https://github.com/THU-MAIC/OpenMAIC.git
  cd OpenMAIC
  pnpm install
  cp .env.example .env.local
  # 配置 API Key
  pnpm dev
  ```

- [ ] 生成一个课程
  - 输入: "教我 Python 基础"
  - 观察: 生成过程、输出内容

- [ ] 体验播放功能
  - 观察: 语音播放、白板演示、交互

- [ ] 探索仓库结构
  - 列出顶层目录，理解每个目录的用途

#### 检查点

> 能否用 2 句话解释 OpenMAIC 是什么？

**答案示例:**
> OpenMAIC 是一个 AI 多智能体互动课堂平台，可以将任何主题自动转化为包含幻灯片、测验、互动实验的完整课程。
> 它使用 LangGraph 编排多个 AI 角色（教师、同学），通过语音讲解和白板演示提供沉浸式学习体验。

---

### 🟡 阶段 2：核心概念（第 3-5 周）

**目标:** 理解核心机制和主要用例

#### 代码阅读路径（按顺序）

1. **入口点:** `app/page.tsx:Home()`
   - 追踪: 用户输入如何触发生成
   - 理解: 表单提交 → API 调用

2. **生成 API:** `app/api/generate-classroom/route.ts:POST()`
   - 理解: 请求验证、任务创建、响应格式

3. **生成管道:** `lib/generation/pipeline-runner.ts:runGenerationPipeline()`
   - 理解: 双阶段流程（大纲 → 场景）

4. **对话 API:** `app/api/chat/route.ts:POST()`
   - 理解: SSE 流式响应、状态管理

5. **编排图:** `lib/orchestration/director-graph.ts:createOrchestrationGraph()`
   - 理解: LangGraph 状态机拓扑

6. **播放引擎:** `lib/playback/engine.ts:PlaybackEngine`
   - 理解: 状态机 (idle → playing → live)

7. **动作引擎:** `lib/action/engine.ts:ActionEngine`
   - 理解: 28+ 动作类型的执行

#### 关键文件深度阅读

| 文件 | 重点关注 | 理解目标 |
|------|----------|----------|
| `lib/types/action.ts` | Action 联合类型 | 理解所有动作类型定义 |
| `lib/types/stage.ts` | Scene, Stage 类型 | 理解核心数据模型 |
| `lib/ai/providers.ts` | getModel() | 理解多提供商抽象 |
| `lib/store/canvas.ts` | Zustand store | 理解状态管理模式 |

#### 动手项目

- [ ] **构建最小示例**
  - 创建一个简单的场景（手动 JSON）
  - 在播放器中加载并播放

- [ ] **修改小功能**
  - 例如：修改默认智能体名称
  - 文件: `lib/orchestration/registry/store.ts`
  - 观察: 修改后的效果

- [ ] **编写一个测试**
  - 为工具函数添加单元测试
  - 理解测试框架（需先添加）

#### 检查点

> 能否追踪一个请求从开始到结束的完整流程？

**追踪示例:**
```
用户输入 "教我 Python"
    ↓
app/page.tsx: handleSubmit()
    ↓
POST /api/generate-classroom
    ↓
lib/generation/pipeline-runner.ts: runGenerationPipeline()
    ↓
Stage 1: generateSceneOutlinesFromRequirements() → LLM
    ↓
Stage 2: generateFullScenes() → LLM (并行)
    ↓
保存到 IndexedDB
    ↓
返回 stageId
    ↓
跳转到 /classroom/[id]
    ↓
PlaybackEngine.start()
    ↓
逐个执行 Scene.actions[]
```

---

### 🔴 阶段 3：深度掌握（第 6-10 周）

**目标:** 理解高级功能、内部实现、贡献模式

#### 高级主题

1. **LangGraph 多智能体编排**
   - 文件: `lib/orchestration/director-graph.ts`
   - 关键模式: 状态机、条件边、循环
   - 对比: 与 LangChain Agent 的区别

2. **双阶段生成管道**
   - 文件: `lib/generation/`
   - 关键模式: 大纲 → 场景 → 动作
   - 性能: 并行生成优化

3. **动作系统架构**
   - 文件: `lib/action/engine.ts`, `lib/types/action.ts`
   - 关键模式: 策略模式、同步/异步执行
   - 扩展: 如何添加新动作类型

4. **性能特征与瓶颈**
   - LLM 调用: 最大延迟来源
   - TTS 生成: 可预生成或实时
   - 前端渲染: Canvas 复杂度

#### 贡献路径

1. **设置开发环境**
   ```bash
   # Fork 仓库到你的 GitHub
   # Clone 你的 fork
   git clone https://github.com/YOUR_USERNAME/OpenMAIC.git
   cd OpenMAIC

   # 添加上游仓库
   git remote add upstream https://github.com/THU-MAIC/OpenMAIC.git

   # 创建功能分支
   git checkout -b feature/my-feature
   ```

2. **寻找 "good first issue"**
   - 链接: https://github.com/THU-MAIC/OpenMAIC/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22
   - 类型: Bug 修复、文档改进、小功能

3. **理解 PR 流程**
   - 阅读: `CONTRIBUTING.md`（如存在）
   - 参考: `.github/pull_request_template.md`

4. **建议首次贡献类型**

| 贡献类型 | 难度 | 示例 |
|----------|------|------|
| 文档改进 | ⭐ | 修复 typo，补充说明 |
| Bug 修复 | ⭐⭐ | 修复 UI 问题 |
| 测试添加 | ⭐⭐ | 为工具函数添加测试 |
| 新动作类型 | ⭐⭐⭐ | 添加新的白板动作 |
| 新提供商 | ⭐⭐⭐ | 添加新的 AI 提供商 |

#### 掌握项目

- [ ] **构建非平凡项目**
  - 使用 OpenMAIC 作为基础构建新功能
  - 例如: 添加新的场景类型

- [ ] **移植功能**
  - 从类似项目移植设计模式
  - 理解设计差异

- [ ] **性能分析**
  - 在负载下分析应用性能
  - 识别瓶颈

---

## 快速参考卡

| 我想... | 从这里开始 | 关键文件 |
|---------|-----------|----------|
| 添加新的 AI 提供商 | `lib/ai/providers.ts` | `PROVIDERS` 对象 |
| 添加新的动作类型 | `lib/types/action.ts` | Action 联合类型 |
| | `lib/action/engine.ts` | execute() 方法 |
| 修改生成流程 | `lib/generation/pipeline-runner.ts` | runGenerationPipeline() |
| 修改对话逻辑 | `lib/orchestration/director-graph.ts` | directorNode() |
| 调试播放问题 | `lib/playback/engine.ts` | processNext() |
| 修改 UI 组件 | `components/` | 对应组件文件 |
| 运行特定测试 | `pnpm test` | `tests/` (需添加) |

---

## 社区与生态资源

| 资源 | 链接 | 最适合 |
|------|------|--------|
| 官方文档 | https://open.maic.chat/ | 功能了解 |
| GitHub 仓库 | https://github.com/THU-MAIC/OpenMAIC | 源码、Issue |
| Discord 社区 | https://discord.gg/PtZaaTbH | 讨论、问答 |
| 飞书交流群 | 见 `community/feishu.md` | 中文讨论 |
| 学术论文 | JCST 2026 | 架构理解 |
| OpenClaw | https://github.com/openclaw/openclaw | 消息应用集成 |

---

## 快速获胜 (Quick Win)

**最快获得可见结果的方法：**

```bash
# 1. Fork 并 Clone 仓库
# 2. 本地运行
pnpm dev

# 3. 修改智能体默认名称
# 编辑: lib/orchestration/registry/store.ts
# 找到 DEFAULT_AGENTS，修改 name 属性

# 4. 刷新页面，生成课程
# 观察你的修改生效！

# 5. 提交 PR
git add .
git commit -m "feat: customize default agent names"
git push origin feature/custom-agent-names
```

**预期时间:** 30 分钟
**学到的:** 如何修改代码并看到效果

---

## 学习检查清单

### 阶段 1 完成标志
- [ ] 能解释 OpenMAIC 的核心价值
- [ ] 本地成功运行项目
- [ ] 生成并播放了一个课程

### 阶段 2 完成标志
- [ ] 能追踪生成请求的完整流程
- [ ] 理解 LangGraph 状态机
- [ ] 理解双阶段生成管道
- [ ] 完成一个小修改

### 阶段 3 完成标志
- [ ] 能独立添加新功能
- [ ] 提交了第一个 PR
- [ ] 能帮助他人解答问题

---

*学习路径生成日期: 2026-03-20*
*基于实际代码结构设计*
*预计完整学习周期: 10 周 (10 小时/周)*
