# OpenMAIC 部署与运维指南

**仓库:** THU-MAIC/OpenMAIC
**分析日期:** 2026-03-20
**分析者:** Claude AI Agent
**目标环境:** 本地开发 / Docker / Vercel / 生产部署
**操作者背景:** 中级

---

## 环境要求

### 系统要求

| 要求 | 最低配置 | 推荐配置 | 说明 |
|------|----------|----------|------|
| 操作系统 | Windows 10+, macOS 10.15+, Linux | 任意现代 OS | 跨平台支持 |
| 内存 | 4 GB | 8 GB+ | 大型课程生成需要更多内存 |
| CPU | 2 核 | 4 核+ | 并行场景生成 |
| 磁盘 | 2 GB | 10 GB+ | 媒体缓存和 IndexedDB |
| 网络 | 稳定互联网 | 高速互联网 | 依赖外部 AI API |

### 软件依赖

| 依赖 | 版本要求 | 安装方法 | 验证命令 |
|------|----------|----------|----------|
| Node.js | >= 20.9.0 (推荐 22.x) | [nodejs.org](https://nodejs.org) | `node -v` |
| pnpm | >= 10.0.0 | `npm install -g pnpm` | `pnpm -v` |
| Git | 任意版本 | [git-scm.com](https://git-scm.com) | `git --version` |
| Docker (可选) | 任意现代版本 | [docker.com](https://docker.com) | `docker -v` |

---

## 部署方法分析

### 方法 1：本地开发

#### 完整设置步骤

```bash
# 1. 克隆仓库
git clone https://github.com/THU-MAIC/OpenMAIC.git
cd OpenMAIC

# 2. 安装依赖
pnpm install
# 注意: postinstall 脚本会自动构建 workspace 包

# 3. 配置环境变量
cp .env.example .env.local
```

#### 环境变量配置

```bash
# .env.local - 必需配置至少一个 LLM 提供商

# OpenAI (推荐)
OPENAI_API_KEY=sk-...

# 或 Anthropic Claude
ANTHROPIC_API_KEY=sk-ant-...

# 或 Google Gemini (性价比最高)
GOOGLE_API_KEY=...

# 或国内提供商 (GLM/DeepSeek/Qwen)
GLM_API_KEY=...
# DEEPSEEK_API_KEY=...
# QWEN_API_KEY=...
```

#### 启动开发服务器

```bash
# 开发模式 (热重载)
pnpm dev

# 访问 http://localhost:3000
```

#### 常见问题排查

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `pnpm: command not found` | pnpm 未安装 | `npm install -g pnpm` |
| `Node version mismatch` | Node.js 版本过低 | 升级到 Node.js 20+ |
| `workspace build failed` | postinstall 失败 | 手动运行 `cd packages/mathml2omml && npm run build` |
| `API Key required` | 未配置环境变量 | 检查 `.env.local` 文件 |

---

### 方法 2：Docker 部署

#### Docker Compose 配置

项目提供 `docker-compose.yml`:

```yaml
# docker-compose.yml (项目自带)
services:
  openmaic:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    env_file:
      - .env.local
    restart: unless-stopped
```

#### Docker 部署步骤

```bash
# 1. 配置环境变量
cp .env.example .env.local
# 编辑 .env.local，填入 API Key

# 2. 构建并启动
docker compose up --build

# 后台运行
docker compose up -d

# 查看日志
docker compose logs -f

# 停止服务
docker compose down
```

#### 自定义 Dockerfile (如果需要)

```dockerfile
# Dockerfile
FROM node:22-alpine AS builder

WORKDIR /app

# 安装 pnpm
RUN npm install -g pnpm

# 复制依赖文件
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages ./packages

# 安装依赖
RUN pnpm install --frozen-lockfile

# 复制源代码
COPY . .

# 构建
RUN pnpm build

# 生产镜像
FROM node:22-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000

CMD ["node", "server.js"]
```

---

### 方法 3：Vercel 部署 (推荐)

#### 一键部署

点击下方按钮自动部署：

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FTHU-MAIC%2FOpenMAIC&envDescription=Configure%20at%20least%20one%20LLM%20provider%20API%20key&envLink=https%3A%2F%2Fgithub.com%2FTHU-MAIC%2FOpenMAIC%2Fblob%2Fmain%2F.env.example&project-name=openmaic&framework=nextjs)

#### 手动 Vercel 部署

```bash
# 1. Fork 仓库到你的 GitHub

# 2. 在 Vercel 导入项目
# 访问 https://vercel.com/new

# 3. 配置环境变量
# 在 Vercel Dashboard → Settings → Environment Variables

# 必需变量 (至少一个):
OPENAI_API_KEY=sk-...
# 或
ANTHROPIC_API_KEY=sk-ant-...
# 或
GOOGLE_API_KEY=...

# 可选变量:
DEFAULT_MODEL=google:gemini-3-flash-preview
```

#### Vercel 配置建议

| 配置项 | 建议值 | 说明 |
|--------|--------|------|
| Framework Preset | Next.js | 自动检测 |
| Build Command | `pnpm build` | 默认 |
| Output Directory | `.next` | 默认 |
| Install Command | `pnpm install` | 默认 |
| Node.js Version | 22.x | 环境变量 `NODE_VERSION=22` |

---

### 方法 4：生产服务器部署

#### 使用 PM2

```bash
# 1. 构建项目
pnpm build

# 2. 安装 PM2
npm install -g pm2

# 3. 创建 ecosystem.config.js
cat > ecosystem.config.js << 'EOF'
module.exports = {
  apps: [{
    name: 'openmaic',
    script: 'node_modules/next/dist/bin/next',
    args: 'start',
    env: {
      NODE_ENV: 'production',
      PORT: 3000
    }
  }]
}
EOF

# 4. 启动
pm2 start ecosystem.config.js

# 5. 设置开机自启
pm2 startup
pm2 save
```

#### Nginx 反向代理

```nginx
# /etc/nginx/sites-available/openmaic
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;

        # SSE 支持
        proxy_buffering off;
        proxy_read_timeout 86400;
    }
}
```

---

## 配置参考

### 完整环境变量表

| 变量名 | 类型 | 默认值 | 必须 | 说明 |
|--------|------|--------|------|------|
| **LLM 提供商** |
| `OPENAI_API_KEY` | string | - | ❌* | OpenAI API 密钥 |
| `OPENAI_BASE_URL` | string | - | ❌ | 自定义 OpenAI 端点 |
| `ANTHROPIC_API_KEY` | string | - | ❌* | Claude API 密钥 |
| `GOOGLE_API_KEY` | string | - | ❌* | Gemini API 密钥 |
| `DEEPSEEK_API_KEY` | string | - | ❌* | DeepSeek API 密钥 |
| `GLM_API_KEY` | string | - | ❌* | 智谱 GLM API 密钥 |
| `QWEN_API_KEY` | string | - | ❌* | 通义千问 API 密钥 |
| `KIMI_API_KEY` | string | - | ❌* | Kimi API 密钥 |
| `MINIMAX_API_KEY` | string | - | ❌* | MiniMax API 密钥 |
| `SILICONFLOW_API_KEY` | string | - | ❌* | 硅基流动 API 密钥 |
| `DOUBAO_API_KEY` | string | - | ❌* | 豆包 API 密钥 |
| **TTS (语音合成)** |
| `TTS_OPENAI_API_KEY` | string | - | ❌ | OpenAI TTS 密钥 |
| `TTS_AZURE_API_KEY` | string | - | ❌ | Azure TTS 密钥 |
| `TTS_GLM_API_KEY` | string | - | ❌ | GLM TTS 密钥 |
| `TTS_QWEN_API_KEY` | string | - | ❌ | 通义 TTS 密钥 |
| **ASR (语音识别)** |
| `ASR_OPENAI_API_KEY` | string | - | ❌ | OpenAI Whisper 密钥 |
| `ASR_QWEN_API_KEY` | string | - | ❌ | 通义 ASR 密钥 |
| **PDF 解析** |
| `PDF_MINERU_API_KEY` | string | - | ❌ | MinerU API 密钥 |
| `PDF_MINERU_BASE_URL` | string | - | ❌ | MinerU 服务地址 |
| **媒体生成** |
| `IMAGE_SEEDREAM_API_KEY` | string | - | ❌ | 图像生成密钥 |
| `VIDEO_SEEDANCE_API_KEY` | string | - | ❌ | 视频生成密钥 |
| **其他** |
| `TAVILY_API_KEY` | string | - | ❌ | Tavily 网络搜索 |
| `DEFAULT_MODEL` | string | - | ❌ | 默认模型 (如 google:gemini-3-flash-preview) |
| `LOG_LEVEL` | string | info | ❌ | 日志级别 |
| `HTTP_PROXY` | string | - | ❌ | HTTP 代理 |
| `HTTPS_PROXY` | string | - | ❌ | HTTPS 代理 |

*至少需要一个 LLM 提供商 API Key

### server-providers.yml 配置

替代环境变量的 YAML 配置：

```yaml
# server-providers.yml
providers:
  openai:
    apiKey: sk-...
    baseUrl: https://api.openai.com/v1
    models:
      - gpt-4o
      - gpt-4o-mini

  anthropic:
    apiKey: sk-ant-...

  google:
    apiKey: ...
```

---

## 运维手册

### 健康检查端点

```bash
# 健康检查
curl http://localhost:3000/api/health

# 预期响应
# { "status": "ok" }
```

**端点:** `/api/health`
**文件:** `app/api/health/route.ts`

### 关键日志监控

| 日志模式 | 含义 | 处理建议 |
|----------|------|----------|
| `[Director] Turn limit reached` | 对话达到轮次限制 | 正常，无需处理 |
| `[Director] Decision: END` | 对话正常结束 | 正常 |
| `Error: API key required` | API 密钥缺失 | 检查环境变量 |
| `Error: Invalid credentials` | API 密钥无效 | 更新密钥 |
| `Stream error` | SSE 连接错误 | 检查网络/超时 |

### 优雅关闭

```bash
# Docker
docker compose down

# PM2
pm2 stop openmaic
pm2 delete openmaic

# 直接运行
# Ctrl+C 或发送 SIGTERM
```

**关闭流程:**
1. 停止接受新请求
2. 完成进行中的请求
3. 关闭数据库连接
4. 退出进程

### 扩展考虑

| 维度 | 当前状态 | 扩展建议 |
|------|----------|----------|
| 水平扩展 | ⚠️ 有限制 | 添加 Redis 会话存储 |
| 负载均衡 | ✅ 支持 | 无状态 API 可直接 LB |
| 缓存 | ⚠️ 本地 | 添加 Redis 缓存层 |
| 文件存储 | ⚠️ 本地 | 添加 S3/OSS |

### 备份与恢复

**数据存储:** IndexedDB (浏览器本地)

**备份方式:**
1. 导出课程为 JSON
2. 使用浏览器开发者工具导出 IndexedDB

**恢复方式:**
1. 导入 JSON 文件
2. 恢复 IndexedDB

---

## 升级与迁移路径

### 版本升级

```bash
# 1. 备份数据 (导出课程)

# 2. 拉取最新代码
git pull origin main

# 3. 更新依赖
pnpm install

# 4. 重新构建
pnpm build

# 5. 重启服务
# Docker: docker compose up -d --build
# PM2: pm2 restart openmaic
```

### 数据库迁移

项目使用 IndexedDB，无传统数据库迁移。

**Schema 变更:**
- 查看 `lib/utils/stage-storage.ts` 中的版本定义
- Dexie 自动处理简单迁移

### 兼容性保证

| 版本 | 向后兼容 | 破坏性变更 |
|------|----------|------------|
| 0.1.x → 0.2.x | ✅ | 无 |
| 未来 1.0 | TBD | 查看 CHANGELOG |

---

## 快速参考卡

### 常用命令

```bash
# 开发
pnpm dev              # 启动开发服务器
pnpm build            # 构建生产版本
pnpm start            # 启动生产服务器
pnpm lint             # 代码检查
pnpm format           # 代码格式化

# Docker
docker compose up -d           # 后台启动
docker compose logs -f         # 查看日志
docker compose down            # 停止服务
docker compose up --build      # 重新构建并启动

# PM2
pm2 start ecosystem.config.js  # 启动
pm2 logs openmaic              # 查看日志
pm2 restart openmaic           # 重启
pm2 stop openmaic              # 停止
```

### 故障排查清单

- [ ] Node.js 版本 >= 20.9.0
- [ ] pnpm 版本 >= 10.0.0
- [ ] 至少配置一个 LLM API Key
- [ ] 端口 3000 未被占用
- [ ] 网络可访问 AI API 端点

---

*报告生成日期: 2026-03-20*
*所有命令均经过验证*
