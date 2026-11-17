# 拣选路线改造复测

配对实验分析的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker，按段推进、可暂停恢复）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

## 分段推进与暂停恢复
- 分析任务在创建时按输入快照切成若干段（默认 8 条一段，`ANALYSIS_SEGMENT_SIZE` 可调），每段独立持久化在 `job_segments`，按 `(job_id, segment_index)` 唯一。
- worker 每次只领取一段；各负责人按最近一次领取时间轮流领取，同一负责人有多项待办时也不会挤掉其他人的执行机会，后来的短分析可以在长任务完成前先出结果。
- 暂停保留已完成段并把在跑的段退回待算；恢复从第一个未完成段继续。worker 重启时把 running 段退回 pending，已完成段和合并进度原样保留。
- 写回段需要领取令牌匹配且任务仍在推进；暂停成功后的迟到写回、并发重试同一段都会被丢弃，人数不会重复累计。
- 全部段完成后才合成均值、方差、t 值与置信区间并发布可批准结论；半成品不写入结果，也不会进入分发简报。合成与整批计算走同一条汇总路径，结果逐字节一致；逐人差值先做 `before−after` 再累计，测时很大而差值很小时保持稳定。
- 工作台可暂停、恢复，并查看分段进度与等待原因（排队、轮流执行、已暂停）；旧分析结果仍可查看。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18205；后端文档 http://localhost:19205/docs；数据库宿主端口 17705。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。`ANALYSIS_SEGMENT_SIZE` 控制每段条数，便于本机验收暂停与轮转。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/paused/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=pick-paired；FRONTEND_PORT=18205；BACKEND_PORT=19205；DB_PORT=17705；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。分段的统计等价性、暂停恢复与公平调度测试位于 backend/tests/（`python -m pytest backend/tests`）；测试与截图统一存放 ../../test-artifacts/gb-1006/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
