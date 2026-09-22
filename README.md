# Webhook 事件回放沙箱

一个本地化的 Webhook 回放调试工具：粘贴带时间戳的 JSON 事件并配置共享密钥，系统按**虚拟时间轴**重排后逐步投递到本地接收端，每次投递生成 **HMAC-SHA256 签名**。支持暂停、单步、失败自动/手动重试，界面实时展示投递顺序、签名、响应码与重试间隔，并可导出回放记录。

技术栈：FastAPI + SQLite + 原生 HTML/JS（无构建步骤）。

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t webhook-replay .
docker run -p 8000:8000 -v replay-data:/data webhook-replay
```

### 方式三：本地 Python

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后浏览器访问 **http://localhost:8000** 。

## 使用流程

1. **创建回放会话**：填写共享密钥、目标地址（默认指向内置接收端 `http://localhost:8000/receiver/default`）、时间流速与重试策略，粘贴事件 JSON（或点「载入示例事件」）。
2. **回放控制**：开始 / 暂停 / 继续 / 单步 / 重置。单步在暂停或空闲状态下精确投递一个事件。
3. **观察投递记录**：表格按虚拟时间轴展示投递顺序、状态、尝试次数、响应码与签名；点击行展开每次尝试的完整签名、耗时、**重试前等待间隔**与响应体。失败事件可单独「重试」。
4. **配置本地接收端**：可切换 正常 200 / 总是失败 / 随机失败 / 慢响应 四种模式，用于演练重试与退避；填入密钥后接收端会校验签名并在日志中显示 ✓/✗。
5. **导出**：一键下载回放记录（JSON 全量 / CSV 每次尝试一行）。

## 事件格式

JSON 数组，每个事件必须包含 `timestamp` / `ts` / `time` 之一（epoch 秒或 ISO 8601）。报文体取 `payload`（或 `data`）字段，缺省时使用整个对象：

```json
[
  {
    "timestamp": "2026-09-22T10:00:00Z",
    "payload": { "type": "order.paid", "id": "evt_1001", "data": { "amount": "99.00" } }
  },
  { "ts": 1758526260, "type": "user.created", "user_id": 42 }
]
```

事件无需按时间排序，系统会按 `(时间戳, 原始顺序)` 重排并赋予投递序号。

## 虚拟时间轴

相邻事件的投递间隔 = `时间戳差 ÷ 时间流速`（单次上限 3 秒真实时间）。例如两个事件相差 1 小时、流速 3600× 时，实际等待 1 秒。暂停会冻结倒计时。

## 签名算法

与主流 Webhook 方案一致，接收方可据此验签：

```
X-Webhook-Signature: sha256=<HMAC_SHA256(secret, "{X-Webhook-Timestamp}.{body}")>
X-Webhook-Timestamp: <unix 秒>
X-Webhook-Event-Id:  <事件 ID>
X-Webhook-Delivery-Attempt: <第几次尝试>
```

Python 验签示例：

```python
import hashlib, hmac

expected = hmac.new(secret.encode(), f"{ts_header}.".encode() + raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, signature_header.removeprefix("sha256="))
```

## 重试策略

投递失败（非 2xx 或网络错误）后按**指数退避**自动重试：第 n 次重试前等待 `基础间隔 × 2^(n-1)` 秒（默认 1s → 2s → 4s，最多 3 次）。自动重试耗尽后事件标记为失败，可在界面手动重试（暂停/空闲状态下）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 创建会话（事件、密钥、流速、重试策略） |
| GET | `/api/sessions` / `/api/sessions/{id}` | 会话列表 / 会话详情（含事件与尝试记录） |
| POST | `/api/sessions/{id}/start` `/pause` `/resume` `/step` `/reset` | 回放控制 |
| POST | `/api/sessions/{id}/events/{eid}/retry` | 手动重试单个失败事件 |
| GET | `/api/sessions/{id}/export?format=json\|csv` | 导出回放记录 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| POST | `/receiver/{name}` | 内置接收端（行为可配置） |
| PUT | `/api/receivers/{name}/config` | 配置接收端行为（ok/fail/flaky/slow、延迟、验签密钥） |
| GET | `/api/receivers/{name}` | 查看接收端配置与接收日志 |

## 数据持久化

SQLite 数据库默认位于容器内 `/data/replay.db`（Compose 已挂载命名卷 `replay-data`），本地运行时位于 `./data/replay.db`，可用环境变量 `DB_PATH` 覆盖。服务重启后，进行中的会话会安全落回「已暂停」状态。
