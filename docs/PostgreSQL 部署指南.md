# PostgreSQL 部署指南

本文档介绍如何使用 Docker Desktop 部署 PostgreSQL 数据库，用于支持会话管理模块。

## 前置条件

- 已安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- Docker Desktop 已启动并运行

## 一、部署步骤

### 1.1 确认 Docker Desktop 运行状态

打开 Docker Desktop 应用程序，确认左下角状态显示为 **Running**（绿色）。

### 1.2 启动 PostgreSQL 容器

在项目根目录下执行：

```powershell
# 进入项目目录
cd d:\code\py_project\PythonProject\test3

# 启动容器（后台运行）
docker-compose up -d
```

预期输出：
```
[+] Running 2/2
 ✔ Network test3_default        Created
 ✔ Container qa_agent_pgsql     Started
```

### 1.3 验证容器状态

```powershell
# 查看容器状态
docker ps
```

预期输出应包含：
```
CONTAINER ID   IMAGE         COMMAND                  STATUS                   PORTS                    NAMES
xxxxxxxxxxxx   postgres:15   "docker-entrypoint.s…"   Up X seconds (healthy)   0.0.0.0:5432->5432/tcp   qa_agent_pgsql
```

**关键检查点**：`STATUS` 列显示 `(healthy)` 表示 PostgreSQL 已就绪。

### 1.4 查看容器日志（可选）

```powershell
# 查看启动日志
docker logs qa_agent_pgsql
```

---

## 二、数据库连接配置

### 2.1 连接信息

| 参数 | 值 |
|------|-----|
| 主机 | localhost |
| 端口 | 5432 |
| 用户名 | user |
| 密码 | 1234 |
| 数据库名 | agent |

### 2.2 连接字符串

```
postgresql://user:1234@localhost:5432/agent
```

### 2.3 .env 配置

确认 `.env` 文件中已配置：

```env
CONVERSATION_DB_URL=postgresql://user:1234@localhost:5432/agent
CONVERSATION_MAX_CONTEXT_TURNS=5
```

---

## 三、部署后校验

### 3.1 运行冒烟测试

```powershell
python scripts/smoke_test.py
```

预期输出包含：
```
[测试] 会话管理模块
============================================================
[OK] 导入会话管理模块成功
[INFO] 测试数据库连接...
[OK] 数据库连接成功，表已创建
[INFO] 测试 ConversationManager...
[OK] ConversationManager 实例化成功
[INFO] 测试创建会话...
[OK] 创建会话成功: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
[INFO] 测试添加消息...
[OK] 添加消息成功，消息ID: x, x, x
[INFO] 测试获取上下文...
[OK] 获取上下文成功，共 3 条消息
[INFO] 测试获取完整历史...
[OK] 获取完整历史成功，共 3 条消息
[INFO] 测试关闭会话...
[OK] 关闭会话成功
```

### 3.2 使用 psql 验证（可选）

```powershell
# 进入容器内部
docker exec -it qa_agent_pgsql psql -U user -d agent

# 查看表结构
\dt

# 查看会话数据
SELECT * FROM conversations;

# 退出
\q
```

---

## 四、常用命令

### 4.1 容器管理

```powershell
# 启动容器
docker-compose up -d

# 停止容器
docker-compose down

# 重启容器
docker-compose restart

# 删除容器和数据卷（清空数据）
docker-compose down -v
```

### 4.2 数据管理

```powershell
# 进入 PostgreSQL 命令行
docker exec -it qa_agent_pgsql psql -U user -d agent

# 备份数据库
docker exec qa_agent_pgsql pg_dump -U user agent > backup.sql

# 恢复数据库
cat backup.sql | docker exec -i qa_agent_pgsql psql -U user agent
```

---

## 五、常见问题

### Q1: 端口 5432 被占用

**错误信息**：
```
Error: port is already allocated
```

**解决方案**：
1. 检查是否有其他 PostgreSQL 实例运行
2. 修改 `docker-compose.yml` 中的端口映射，如 `"5433:5432"`
3. 同步更新 `.env` 中的连接字符串

### Q2: 容器启动但状态不健康

**排查步骤**：
```powershell
# 查看日志
docker logs qa_agent_pgsql

# 重启容器
docker-compose restart
```

### Q3: 连接被拒绝

**检查清单**：
1. Docker Desktop 是否运行
2. 容器是否启动：`docker ps`
3. 防火墙是否阻止 5432 端口
4. `.env` 中的连接字符串是否正确

---

## 六、清理与重置

### 6.1 停止并删除容器

```powershell
docker-compose down
```

### 6.2 删除数据卷（清空所有数据）

```powershell
docker-compose down -v
```

### 6.3 完全清理

```powershell
# 停止并删除容器、网络、数据卷
docker-compose down -v --remove-orphans

# 删除镜像
docker rmi postgres:15
```

---

## 七、部署完成标志

- [ ] Docker Desktop 运行中
- [ ] `docker ps` 显示 `qa_agent_pgsql` 容器状态为 `(healthy)`
- [ ] `python scripts/smoke_test.py` 会话管理模块测试通过
- [ ] 输出显示 `[SUCCESS] 所有冒烟测试通过！`
