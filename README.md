# 🪝 Webhook 事件回放沙箱

粘贴带时间戳的 JSON 事件、配置共享密钥，系统会按**虚拟时间轴**重排事件并逐步投递到本地接收端，自动生成 **HMAC 签名**；支持暂停 / 单步 / 继续 / 停止、失败自动重试（指数退避），界面实时展示投递顺序、签名、响应码与重试间隔，并可导出完整回放记录（JSON / CSV）。

技术栈：**FastAPI + SQLite（aiosqlite）+ 原生 HTML/CSS/JS**，无前端构建步骤。

---

## ✨ 功能一览

- **事件粘贴与重排**：支持 JSON 数组、JSON Lines、单个对象；自动识别 `timestamp` / `ts` / `time` / `date` 等时间戳字段（ISO 8601、Unix 秒、毫秒），按时间升序排序。
- **虚拟时间轴回放**：保留原始事件之间的间隔，按可调速度（0.1x–1000x）压缩/放大时间轴；每条投递带虚拟时间戳。
- **HMAC 签名**：支持 HMAC-SHA256 / SHA1 / SHA512，可自定义请求头前缀，附带事件 ID、事件类型、时间戳、投递 ID 头。
- **播放控制**：随时暂停、**单步投递一条**、继续、停止；停止后可从断点重新启动。
- **失败重试**：非 2xx 或网络错误自动重试，指数退避（基础间隔 × 倍数^n），重试等待中同样可暂停/停止；失败策略可选「继续」或「中止」。
- **内置接收端（Catch Box）**：一键创建本地接收 URL，完整记录请求头/报文/响应，可配置「每 N 次返回 5xx」来验证重试逻辑。
- **可视化时间轴**：投递顺序、原始/虚拟时间、状态徽章、HTTP 响应码、尝试次数、每次重试的实际等待间隔、响应摘要、错误信息、可复制的签名。
- **导出**：整段回放记录（配置 + 事件 + 全部投递尝试）导出为 JSON；投递明细导出为带 BOM 的 CSV（Excel 友好）。

---

## 🚀 快速开始

### Docker Compose（推荐）

```bash
docker compose up -d --build
```

浏览器打开 <http://localhost:8000> 即可使用。数据保存在 Docker 卷 `sandbox-data` 中。

### Docker

```bash
docker build -t webhook-replay-sandbox .
docker run -d -p 8000:8000 -v "$PWD/data:/data" webhook-replay-sandbox
```

### 本地运行（Python 3.11+）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：export WEBHOOK_SANDBOX_DB=/path/to/sandbox.db
```

---

## 🧭 使用流程

1. 打开 **「本地接收端」**，创建一个 Catch Box，复制其 URL（想测试重试可把「每 N 次请求失败」设为 `2`）。
2. 回到 **「新建回放」**：
   - 粘贴事件（可点「填入示例」），点「解析预览」确认排序与时间跨度；
   - 目标 URL 选择/粘贴接收端地址（也可以是任何本地或远程地址）；
   - 填写共享密钥、选择签名算法、调整速度与重试参数；
   - 点击「创建并回放」。
3. 在时间轴页面：**⏸ 暂停 → ⏭ 单步**逐条观察投递；展开任意一行查看请求体、签名与每次尝试的响应；速度可实时调整。
4. 右上角 **导出 JSON / CSV** 保存回放记录。

### 事件格式示例

```json
[
  {"id": "evt_001", "type": "order.created", "timestamp": "2026-09-22T10:00:00Z", "order_id": "o-1"},
  {"id": "evt_002", "type": "order.paid",    "timestamp": "2026-09-22T10:00:09Z", "order_id": "o-1"}
]
```

也支持 JSON Lines：

```text
{"ts": 1758538800000, "type": "a", "data": 1}
{"ts": "2026-09-22T12:00:00+08:00", "type": "b", "data": 2}
```

---

## 🔐 签名说明

每条请求以**原始 JSON 字节**作为请求体（`Content-Type: application/json`），签名内容为：

```text
sign_string = "<unix秒级时间戳>." + raw_body_bytes
signature   = base64( HMAC-<algo>(secret, sign_string) )
```

默认请求头（前缀可改为任意值，如 `X-Stripe` 风格的 `X-Webhook`）：

| 请求头 | 内容 |
| --- | --- |
| `X-Webhook-Signature` | `hmac-sha256=<base64摘要>` |
| `X-Webhook-Timestamp` | 签名所用 Unix 秒级时间戳 |
| `X-Webhook-Event` | 事件类型 |
| `X-Webhook-Event-Id` | 事件 ID |
| `X-Webhook-Delivery` | 本次投递的唯一 ID（每次重试不变） |

接收端验签示例（Python）：

```python
import base64, hmac, hashlib
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
SECRET = b"whsec_test_123"

@app.post("/my-endpoint")
async def receive(request: Request):
    body = await request.body()
    ts = request.headers["X-Webhook-Timestamp"]
    scheme, sig = request.headers["X-Webhook-Signature"].split("=", 1)
    expect = base64.b64encode(
        hmac.new(SECRET, ts.encode() + b"." + body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(sig, expect):
        raise HTTPException(401, "bad signature")
    return {"ok": True}
```

> 验签时请使用**未经解析/重序列化的原始字节**，否则 JSON 空白差异会导致签名不一致。沙箱界面里展示的「请求体」就是实际投递的字节。

返回 `2xx` 视为投递成功；返回其它状态码（或连接失败）按配置重试。

---

## 🛠 重试与控制语义

- 第 1 次重试在 `基础间隔` 后进行，第 2 次在 `基础间隔 × 倍数` 后，依此类推。
- 暂停可以发生在：间隔等待中、重试退避等待中；恢复后会扣除已经等待的部分，不会重复等待。
- **单步**：立即投递下一条事件，然后自动停在下一条之前；间隔等待中执行单步会跳过剩余间隔。
- **停止**：包括等待重试在内都会尽快中止；已成功投递的事件不会重发，重新启动会话会从下一条事件继续。
- 「失败策略 = 中止」时，任一事件重试用尽后整个会话标记为失败。
- 运行中调整**速度**立即生效；其余配置（目标 URL、密钥、重试参数等）在下次启动会话时生效。

---

## 📚 HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/parse-preview` | 解析事件文本，返回排序结果（不落库） |
| POST | `/api/sessions` | 创建回放会话（`autostart` 控制是否立即开始） |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/{id}` | 会话详情（事件 + 投递记录） |
| PATCH | `/api/sessions/{id}` | 修改名称/密钥/目标/速度/重试参数 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| POST | `/api/sessions/{id}/start` | 启动 / 断点续播 |
| POST | `/api/sessions/{id}/pause` | 暂停 |
| POST | `/api/sessions/{id}/resume` | 继续 |
| POST | `/api/sessions/{id}/step` | 单步投递一条 |
| POST | `/api/sessions/{id}/stop` | 停止 |
| GET | `/api/sessions/{id}/export?format=json\|csv` | 导出回放记录 |
| POST/GET/PATCH/DELETE | `/api/catchboxes[/{token}]` | 内置接收端管理 |
| GET/DELETE | `/api/catchboxes/{token}/requests` | 查看/清空捕获的请求 |
| POST | `/catch/{token}` | 内置接收端入口（支持 `?force_status=503` 临时指定返回码） |
| GET | `/api/health` | 健康检查 |

交互式文档：启动后访问 <http://localhost:8000/docs>。

---

## 📁 项目结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由：会话/导出/接收端/静态资源
│   ├── engine.py     # 虚拟时间轴回放引擎（asyncio 任务、命令通道、重试）
│   ├── db.py         # SQLite 表结构与连接
│   ├── models.py     # Pydantic 模型
│   └── util.py       # 事件解析与时间戳处理
├── static/
│   ├── index.html    # 单页界面
│   ├── app.js        # 原生 JS（轮询、时间轴渲染）
│   └── style.css
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## ⚠️ 说明

该工具用于**本地开发与联调测试**：请勿把接收端直接暴露到公网，也不要向不可信目标投递包含真实生产密钥或个人数据的事件。
